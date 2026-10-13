"""Read-only web viewer for the jobs database.

Zero extra dependencies: stdlib http.server + sqlite3 (read-only URI mode).
The jobs table is never written; the scraper pipeline stays the single writer.
"Applied" marks live outside the scraper DB in database/applied.json.

Run:     python webui.py  [--port 8787]        then open http://127.0.0.1:8787
Export:  python webui.py --export dist         static snapshot for Netlify;
         applied marks are stored in the browser's localStorage there.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB_PATH = Path(__file__).resolve().parent / "database" / "jobs.db"
APPLIED_PATH = Path(__file__).resolve().parent / "database" / "applied.json"

_local = threading.local()
_applied_lock = threading.Lock()


def _connection() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


# -- applied marks -------------------------------------------------------------
# Keyed by the job's content hash (stable across DB rebuilds), stored in a
# plain JSON file so the scraper's SQLite schema stays untouched.

def load_applied() -> set[str]:
    try:
        return set(json.loads(APPLIED_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def set_applied(job_hash: str, applied: bool) -> None:
    with _applied_lock:
        marks = load_applied()
        if applied:
            marks.add(job_hash)
        else:
            marks.discard(job_hash)
        APPLIED_PATH.write_text(
            json.dumps(sorted(marks), indent=0, ensure_ascii=False), encoding="utf-8"
        )


# -- posted_time parsing -------------------------------------------------------
# Sources emit relative English strings ("8 minutes ago", "Yesterday"),
# absolute dates ("July 16", "October 6, 2025") or nothing. For sorting we
# estimate an absolute timestamp anchored to date_found.

_REL_RE = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
_ABS_RE = re.compile(r"^([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?$", re.I)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_REL_UNITS = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def estimate_posted_at(posted_time: str | None, date_found: str) -> str:
    """Best-effort absolute timestamp for a relative/absolute posted_time.

    Falls back to date_found when the string is missing or unparseable, so
    every row sorts deterministically.
    """
    try:
        found = datetime.fromisoformat(date_found)
    except (ValueError, TypeError):
        found = None
    if not posted_time or found is None:
        return date_found
    text = posted_time.strip().lower()

    match = _REL_RE.search(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        return (found - amount * _REL_UNITS[unit]).isoformat(sep=" ")
    if text == "yesterday":
        return (found - timedelta(days=1)).isoformat(sep=" ")
    if text in ("today", "just now", "just posted"):
        return date_found

    match = _ABS_RE.match(text)
    if match and match.group(1) in _MONTHS:
        year = int(match.group(3)) if match.group(3) else found.year
        try:
            when = datetime(year, _MONTHS[match.group(1)], int(match.group(2)))
        except ValueError:
            return date_found
        # Year-less dates in the future must belong to the previous year.
        if not match.group(3) and when > found:
            when = when.replace(year=year - 1)
        return when.isoformat(sep=" ")
    return date_found


# -- title categorisation ------------------------------------------------------

_UX_RE = re.compile(r"\bux\b|user experience", re.I)
_UI_RE = re.compile(r"\bui\b|user interface", re.I)
_PRODUCT_RE = re.compile(r"product\s*design", re.I)
_INTERACTION_RE = re.compile(r"interaction\s*design", re.I)
_GRAPHIC_RE = re.compile(r"graphic|grafik|visual\s*design", re.I)


def categorize(title: str) -> str:
    has_ux = bool(_UX_RE.search(title))
    has_ui = bool(_UI_RE.search(title))
    if has_ux and has_ui:
        return "UX/UI"
    if _PRODUCT_RE.search(title):
        return "Product Design"
    if has_ux:
        return "UX"
    if has_ui:
        return "UI"
    if _INTERACTION_RE.search(title):
        return "Interaction"
    if _GRAPHIC_RE.search(title):
        return "Grafik/Görsel"
    return "Diğer"


# -- queries -------------------------------------------------------------------

_SORT_KEYS = {
    "posted": lambda j: j["posted_at"],
    "found": lambda j: j["date_found"],
    "title": lambda j: j["title"].lower(),
    "company": lambda j: j["company"].lower(),
    "location": lambda j: j["location"].lower(),
    "source": lambda j: j["source"].lower(),
    "category": lambda j: j["category"].lower(),
}


def query_jobs(
    source: str | None,
    q: str | None,
    day: str | None,
    category: str | None,
    applied: str | None,
    sort: str,
    direction: str,
    limit: int,
    offset: int,
) -> dict:
    where: list[str] = []
    params: list[object] = []
    if source:
        where.append("source = ?")
        params.append(source)
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if day:
        where.append("date(date_found) = ?")
        params.append(day)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    # Category, posted_at and applied are derived outside SQL, so filter,
    # sort and paginate here. Row counts are small (hundreds); cheap.
    conn = _connection()
    rows = conn.execute(
        f"""SELECT id, title, company, location, url, source, posted_time,
                   date_found, hash
            FROM jobs {where_sql}""",
        params,
    ).fetchall()
    marks = load_applied()

    jobs = []
    category_counts: dict[str, int] = {}
    for row in rows:
        job = dict(row)
        job["category"] = categorize(job["title"])
        job["posted_at"] = estimate_posted_at(job["posted_time"], job["date_found"])
        job["applied"] = job["hash"] in marks
        category_counts[job["category"]] = category_counts.get(job["category"], 0) + 1
        jobs.append(job)

    if category:
        jobs = [j for j in jobs if j["category"] == category]
    if applied == "yes":
        jobs = [j for j in jobs if j["applied"]]
    elif applied == "no":
        jobs = [j for j in jobs if not j["applied"]]

    key = _SORT_KEYS.get(sort, _SORT_KEYS["found"])
    jobs.sort(key=key, reverse=(direction != "asc"))

    total = len(jobs)
    page = jobs[offset:offset + limit]
    return {
        "total": total,
        "jobs": page,
        "categories": dict(sorted(category_counts.items(), key=lambda kv: -kv[1])),
    }


def query_stats() -> dict:
    conn = _connection()
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE date(date_found) = date('now')"
    ).fetchone()[0]
    week = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE date_found >= datetime('now', '-7 days')"
    ).fetchone()[0]
    per_source = dict(
        conn.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source ORDER BY 2 DESC").fetchall()
    )
    last = conn.execute("SELECT MAX(date_found) FROM jobs").fetchone()[0]
    return {
        "total": total,
        "today": today,
        "week": week,
        "per_source": per_source,
        "last_found": last,
        "applied": len(load_applied()),
    }


PAGE = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>İş İlanları — Job Monitor</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #262b36;
    --text: #e6e9ef; --muted: #8b93a3; --accent: #4f8cff;
    --pill: #1f2430;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 system-ui, "Segoe UI", sans-serif;
  }
  header {
    padding: 20px 24px 12px; border-bottom: 1px solid var(--border);
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px 20px;
  }
  header h1 { font-size: 18px; margin: 0; }
  header .meta { color: var(--muted); font-size: 13px; }
  .controls {
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    padding: 14px 24px 4px;
  }
  .controls.cats { padding-top: 6px; padding-bottom: 10px; }
  .lbl { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; margin-right: 2px; }
  .pill {
    background: var(--pill); border: 1px solid var(--border); color: var(--text);
    border-radius: 999px; padding: 5px 14px; cursor: pointer; font-size: 13px;
  }
  .pill.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .pill .n { color: var(--muted); margin-left: 6px; font-size: 12px; }
  .pill.active .n { color: #dbe7ff; }
  input[type=search], input[type=date], select {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; padding: 7px 12px; font-size: 13px; outline: none;
  }
  input[type=search] { min-width: 240px; }
  input:focus, select:focus { border-color: var(--accent); }
  .wrap { padding: 0 24px 40px; overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; min-width: 920px; }
  th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th {
    color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
    letter-spacing: .04em; position: sticky; top: 0; background: var(--bg);
    user-select: none; white-space: nowrap;
  }
  th[data-sort] { cursor: pointer; }
  th[data-sort]:hover { color: var(--text); }
  th .arrow { color: var(--accent); margin-left: 4px; }
  td a { color: var(--accent); text-decoration: none; font-weight: 500; }
  td a:hover { text-decoration: underline; }
  .src, .cat { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; background: var(--pill); border: 1px solid var(--border); white-space: nowrap; }
  .cat { border-color: #3a4356; }
  .muted { color: var(--muted); }
  .nowrap { white-space: nowrap; }
  tr.done td { opacity: .5; }
  tr.done td a { color: var(--muted); }
  input.apld { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
  .pager { display: flex; gap: 8px; align-items: center; padding: 16px 24px; }
  .pager button { background: var(--panel); border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 6px 14px; cursor: pointer; }
  .pager button:disabled { opacity: .4; cursor: default; }
  .empty { padding: 40px 24px; color: var(--muted); text-align: center; }
</style>
</head>
<body>
<header>
  <h1>İş İlanları</h1>
  <span class="meta" id="stats">yükleniyor…</span>
</header>

<div class="controls">
  <span class="lbl">Kaynak</span>
  <button class="pill active" data-source="">Tümü</button>
  <span id="sourcePills"></span>
  <input type="search" id="q" placeholder="Ara: başlık, şirket, konum…">
  <input type="date" id="day" title="Gün filtresi">
  <select id="appliedFilter" title="Başvuru filtresi">
    <option value="">Başvuru: tümü</option>
    <option value="yes">Başvurduklarım</option>
    <option value="no">Başvurmadıklarım</option>
  </select>
  <span class="muted" id="count"></span>
</div>

<div class="controls cats">
  <span class="lbl">Kategori</span>
  <button class="pill active" data-category="">Tümü</button>
  <span id="catPills"></span>
</div>

<div class="wrap">
  <table id="tbl">
    <thead>
      <tr>
        <th title="Başvurdum">&#10003;</th>
        <th data-sort="title">Başlık</th>
        <th data-sort="category">Kategori</th>
        <th data-sort="company">Şirket</th>
        <th data-sort="location">Konum</th>
        <th data-sort="source">Kaynak</th>
        <th data-sort="posted">İlan zamanı</th>
        <th data-sort="found" class="nowrap">Bulunma (UTC)</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" style="display:none">Kayıt yok.</div>
</div>

<div class="pager">
  <button id="prev">&larr; Önceki</button>
  <span class="muted" id="page"></span>
  <button id="next">Sonraki &rarr;</button>
</div>

<script>/*__STATIC_DATA__*/</script>
<script>
const LIMIT = 50;
const IS_STATIC = typeof window.STATIC_DATA !== "undefined";
let state = { source: "", category: "", q: "", day: "", applied: "", offset: 0, sort: "found", dir: "desc" };
let debounce;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}

function fmtDate(s) {
  if (!s) return "";
  return s.replace("T", " ").slice(0, 16);
}

/* -- applied marks: server file locally, localStorage on static hosting -- */

function localApplied() {
  try { return new Set(JSON.parse(localStorage.getItem("appliedJobs") || "[]")); }
  catch { return new Set(); }
}

function saveLocalApplied(set) {
  localStorage.setItem("appliedJobs", JSON.stringify([...set]));
}

if (IS_STATIC) {
  // Seed browser storage with marks exported from the local database.
  const seeded = localApplied();
  (window.STATIC_DATA.applied || []).forEach(h => seeded.add(h));
  saveLocalApplied(seeded);
}

async function setApplied(hash, applied) {
  if (IS_STATIC) {
    const marks = localApplied();
    applied ? marks.add(hash) : marks.delete(hash);
    saveLocalApplied(marks);
    return;
  }
  await fetch("/api/applied", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hash, applied }),
  });
}

/* -- data access: fetch locally, in-page filtering on static hosting ----- */

function staticJobs(p) {
  const marks = localApplied();
  let jobs = window.STATIC_DATA.jobs.map(j => ({ ...j, applied: marks.has(j.hash) }));
  if (p.source) jobs = jobs.filter(j => j.source === p.source);
  if (p.q) {
    const needle = p.q.toLowerCase();
    jobs = jobs.filter(j =>
      j.title.toLowerCase().includes(needle) ||
      j.company.toLowerCase().includes(needle) ||
      j.location.toLowerCase().includes(needle));
  }
  if (p.day) jobs = jobs.filter(j => j.date_found.slice(0, 10) === p.day);
  const categories = {};
  for (const j of jobs) categories[j.category] = (categories[j.category] || 0) + 1;
  if (p.category) jobs = jobs.filter(j => j.category === p.category);
  if (p.applied === "yes") jobs = jobs.filter(j => j.applied);
  if (p.applied === "no") jobs = jobs.filter(j => !j.applied);
  const keys = {
    posted: j => j.posted_at, found: j => j.date_found,
    title: j => j.title.toLowerCase(), company: j => j.company.toLowerCase(),
    location: j => j.location.toLowerCase(), source: j => j.source.toLowerCase(),
    category: j => j.category.toLowerCase(),
  };
  const key = keys[p.sort] || keys.found;
  jobs.sort((a, b) => key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0);
  if (p.dir !== "asc") jobs.reverse();
  const sorted = Object.fromEntries(Object.entries(categories).sort((a, b) => b[1] - a[1]));
  return { total: jobs.length, jobs: jobs.slice(p.offset, p.offset + LIMIT), categories: sorted };
}

async function apiJobs(p) {
  if (IS_STATIC) return staticJobs(p);
  const qs = new URLSearchParams({ limit: LIMIT, offset: p.offset, sort: p.sort, dir: p.dir });
  for (const k of ["source", "category", "q", "day", "applied"]) if (p[k]) qs.set(k, p[k]);
  return (await fetch("/api/jobs?" + qs)).json();
}

async function apiStats() {
  if (IS_STATIC) {
    const s = { ...window.STATIC_DATA.stats };
    s.applied = localApplied().size;
    return s;
  }
  return (await fetch("/api/stats")).json();
}

/* -- rendering ------------------------------------------------------------ */

async function loadStats() {
  const s = await apiStats();
  document.getElementById("stats").textContent =
    `toplam ${s.total} · bugün ${s.today} · son 7 gün ${s.week} · başvuru ${s.applied}` +
    (s.last_found ? ` · son kayıt ${fmtDate(s.last_found)} UTC` : "") +
    (IS_STATIC ? " · statik kopya" : "");
  const holder = document.getElementById("sourcePills");
  holder.innerHTML = "";
  for (const [src, n] of Object.entries(s.per_source)) {
    const b = document.createElement("button");
    b.className = "pill" + (state.source === src ? " active" : "");
    b.dataset.source = src;
    b.innerHTML = esc(src) + '<span class="n">' + n + "</span>";
    holder.appendChild(b);
  }
  if (state.source) syncPills("source");
}

function syncPills(kind) {
  document.querySelectorAll(`.pill[data-${kind}]`).forEach(p =>
    p.classList.toggle("active", p.dataset[kind] === state[kind]));
}

function renderCatPills(categories) {
  const holder = document.getElementById("catPills");
  holder.innerHTML = "";
  for (const [cat, n] of Object.entries(categories)) {
    const b = document.createElement("button");
    b.className = "pill" + (state.category === cat ? " active" : "");
    b.dataset.category = cat;
    b.innerHTML = esc(cat) + '<span class="n">' + n + "</span>";
    holder.appendChild(b);
  }
}

function renderArrows() {
  document.querySelectorAll("th[data-sort]").forEach(th => {
    const arrow = th.querySelector(".arrow");
    if (arrow) arrow.remove();
    if (th.dataset.sort === state.sort) {
      const span = document.createElement("span");
      span.className = "arrow";
      span.textContent = state.dir === "asc" ? "\\u2191" : "\\u2193";
      th.appendChild(span);
    }
  });
}

async function load() {
  const data = await apiJobs(state);
  const tbody = document.getElementById("rows");
  tbody.innerHTML = data.jobs.map(j => `
    <tr class="${j.applied ? "done" : ""}">
      <td><input type="checkbox" class="apld" data-hash="${esc(j.hash)}" ${j.applied ? "checked" : ""} title="Başvurdum"></td>
      <td><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></td>
      <td><span class="cat">${esc(j.category)}</span></td>
      <td>${esc(j.company)}</td>
      <td>${esc(j.location)}</td>
      <td><span class="src">${esc(j.source)}</span></td>
      <td class="muted nowrap" title="tahmini: ${esc(fmtDate(j.posted_at))}">${esc(j.posted_time ?? "")}</td>
      <td class="muted nowrap">${fmtDate(j.date_found)}</td>
    </tr>`).join("");
  document.getElementById("empty").style.display = data.jobs.length ? "none" : "";
  document.getElementById("tbl").style.display = data.jobs.length ? "" : "none";
  document.getElementById("count").textContent = data.total + " kayıt";
  renderCatPills(data.categories);
  renderArrows();
  const pageNo = Math.floor(state.offset / LIMIT) + 1;
  const pages = Math.max(1, Math.ceil(data.total / LIMIT));
  document.getElementById("page").textContent = pageNo + " / " + pages;
  document.getElementById("prev").disabled = state.offset === 0;
  document.getElementById("next").disabled = state.offset + LIMIT >= data.total;
}

/* -- events ---------------------------------------------------------------- */

document.getElementById("rows").addEventListener("change", async e => {
  const cb = e.target.closest("input.apld");
  if (!cb) return;
  await setApplied(cb.dataset.hash, cb.checked);
  cb.closest("tr").classList.toggle("done", cb.checked);
  loadStats();
  if (state.applied) load();  // active applied-filter: row may leave the list
});

document.addEventListener("click", e => {
  const th = e.target.closest("th[data-sort]");
  if (th) {
    const key = th.dataset.sort;
    if (state.sort === key) {
      state.dir = state.dir === "asc" ? "desc" : "asc";
    } else {
      state.sort = key;
      // Text columns start ascending; date columns start newest-first.
      state.dir = (key === "posted" || key === "found") ? "desc" : "asc";
    }
    state.offset = 0;
    load();
    return;
  }
  const pill = e.target.closest(".pill");
  if (!pill) return;
  if (pill.dataset.source !== undefined) {
    state.source = pill.dataset.source;
    syncPills("source");
  } else if (pill.dataset.category !== undefined) {
    state.category = pill.dataset.category;
    syncPills("category");
  }
  state.offset = 0;
  load();
});

document.getElementById("q").addEventListener("input", e => {
  clearTimeout(debounce);
  debounce = setTimeout(() => { state.q = e.target.value.trim(); state.offset = 0; load(); }, 250);
});

document.getElementById("day").addEventListener("change", e => {
  state.day = e.target.value; state.offset = 0; load();
});

document.getElementById("appliedFilter").addEventListener("change", e => {
  state.applied = e.target.value; state.offset = 0; load();
});

document.getElementById("prev").onclick = () => { state.offset = Math.max(0, state.offset - LIMIT); load(); };
document.getElementById("next").onclick = () => { state.offset += LIMIT; load(); };

loadStats();
load();
if (!IS_STATIC) setInterval(() => { loadStats(); load(); }, 60000);
</script>
</body>
</html>
"""


# -- static export (Netlify) ----------------------------------------------------

def export_static(out_dir: Path) -> Path:
    """Write a self-contained index.html snapshot of the whole database."""
    data = query_jobs(
        source=None, q=None, day=None, category=None, applied=None,
        sort="found", direction="desc", limit=1_000_000, offset=0,
    )
    payload = {
        "jobs": data["jobs"],
        "stats": query_stats(),
        "applied": sorted(load_applied()),
    }
    html = PAGE.replace(
        "/*__STATIC_DATA__*/",
        "window.STATIC_DATA = " + json.dumps(payload, ensure_ascii=False) + ";",
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")
    return out_file


# -- HTTP ------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/jobs":
            qs = parse_qs(parsed.query)
            try:
                limit = min(max(int(qs.get("limit", ["50"])[0]), 1), 200)
                offset = max(int(qs.get("offset", ["0"])[0]), 0)
            except ValueError:
                self._send_json(400, {"error": "limit/offset must be integers"})
                return
            sort = qs.get("sort", ["found"])[0]
            direction = qs.get("dir", ["desc"])[0]
            data = query_jobs(
                source=qs.get("source", [None])[0],
                q=qs.get("q", [None])[0],
                day=qs.get("day", [None])[0],
                category=qs.get("category", [None])[0],
                applied=qs.get("applied", [None])[0],
                sort=sort if sort in _SORT_KEYS else "found",
                direction="asc" if direction == "asc" else "desc",
                limit=limit,
                offset=offset,
            )
            self._send_json(200, data)
            return
        if parsed.path == "/api/stats":
            self._send_json(200, query_stats())
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        if parsed.path != "/api/applied":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            job_hash = str(body["hash"])
            applied = bool(body["applied"])
        except (ValueError, KeyError):
            self._send_json(400, {"error": "expected JSON {hash, applied}"})
            return
        set_applied(job_hash, applied)
        self._send_json(200, {"ok": True})

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # keep the console quiet


def main() -> None:
    parser = argparse.ArgumentParser(description="Jobs DB web viewer (read-only)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--export", metavar="DIR", default=None,
                        help="write a static snapshot (index.html) and exit")
    args = parser.parse_args()
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    if args.export:
        out = export_static(Path(args.export))
        print(f"Static snapshot written: {out}")
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Job viewer: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
