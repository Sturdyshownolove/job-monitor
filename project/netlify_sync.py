"""Netlify auto-deploy watcher.

Watches the jobs database (and applied.json) for changes; on every change it
rebuilds the static snapshot via webui.export_static and uploads it straight
to Netlify's file-digest deploy API — no git, no GitHub, no Netlify CLI.

Standalone tool: never imports the scraper stack, opens the DB read-only.

Setup (.env):
    NETLIFY_AUTH_TOKEN=...   # app.netlify.com > User settings > Applications
                             # > Personal access tokens > New access token
    NETLIFY_SITE_ID=...      # Site configuration > Site information > Site ID
                             # (or create one: python netlify_sync.py --create-site my-jobs)

Run:
    python netlify_sync.py                  # watch + deploy on change (60s poll)
    python netlify_sync.py --once           # export + deploy once, then exit
    python netlify_sync.py --dry-run        # build the snapshot, skip the upload
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from webui import APPLIED_PATH, DB_PATH, export_static

API = "https://api.netlify.com/api/v1"
EXPORT_DIR = Path(__file__).resolve().parent / "dist"

Fingerprint = tuple[int, int, float]


def _log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def fingerprint() -> Fingerprint:
    """Cheap change signal: row count + max id + applied.json mtime."""
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        count, max_id = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM jobs"
        ).fetchone()
    finally:
        conn.close()
    applied_mtime = APPLIED_PATH.stat().st_mtime if APPLIED_PATH.exists() else 0.0
    return (count, max_id, applied_mtime)


def build_html() -> bytes:
    index = export_static(EXPORT_DIR)
    return index.read_bytes()


def deploy(token: str, site_id: str, html_bytes: bytes) -> str:
    """File-digest deploy: send a manifest, then upload the file Netlify asks for.

    More explicit than the ZIP endpoint and immune to zip-parsing quirks —
    Netlify picks the MIME type from the file extension in the manifest.
    """
    auth = {"Authorization": f"Bearer {token}"}
    sha = hashlib.sha1(html_bytes).hexdigest()
    response = httpx.post(
        f"{API}/sites/{site_id}/deploys",
        headers=auth,
        json={"files": {"/index.html": sha}},
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    deploy_id = data["id"]

    if sha in (data.get("required") or []):
        upload = httpx.put(
            f"{API}/deploys/{deploy_id}/files/index.html",
            headers={**auth, "Content-Type": "application/octet-stream"},
            content=html_bytes,
            timeout=120,
        )
        upload.raise_for_status()

    # Wait until Netlify finishes processing so failures surface here.
    for _ in range(30):
        status = httpx.get(f"{API}/deploys/{deploy_id}", headers=auth, timeout=60)
        status.raise_for_status()
        info = status.json()
        if info.get("state") == "ready":
            return info.get("ssl_url") or info.get("url") or "(ready)"
        if info.get("state") == "error":
            raise RuntimeError(f"Netlify deploy failed: {info.get('error_message')}")
        time.sleep(1)
    return data.get("ssl_url") or data.get("url") or "(processing)"


def create_site(token: str, name: str) -> None:
    response = httpx.post(
        f"{API}/sites",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": name},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    print(f"Site created: {data.get('ssl_url') or data.get('url')}")
    print(f"Site ID:      {data['id']}")
    print("Add to .env:  NETLIFY_SITE_ID=" + data["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-deploy jobs viewer to Netlify")
    parser.add_argument("--interval", type=int, default=60,
                        help="poll interval in seconds (default 60)")
    parser.add_argument("--once", action="store_true",
                        help="export + deploy once, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the ZIP but skip the upload")
    parser.add_argument("--create-site", metavar="NAME", default=None,
                        help="create a new Netlify site and print its ID")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    token = os.environ.get("NETLIFY_AUTH_TOKEN", "").strip()
    site_id = os.environ.get("NETLIFY_SITE_ID", "").strip()

    if not token and not args.dry_run:
        raise SystemExit("NETLIFY_AUTH_TOKEN missing — add it to .env")
    if args.create_site:
        create_site(token, args.create_site)
        return
    if not site_id and not args.dry_run:
        raise SystemExit(
            "NETLIFY_SITE_ID missing — add it to .env "
            "(or create a site: python netlify_sync.py --create-site my-jobs)"
        )
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    def sync(reason: str) -> None:
        html_bytes = build_html()
        if args.dry_run:
            _log(f"dry-run: HTML ready ({len(html_bytes) / 1024:.0f} KB), upload skipped ({reason})")
            return
        url = deploy(token, site_id, html_bytes)
        _log(f"deployed ({reason}): {url}")

    last = fingerprint()
    if args.once:
        sync("manual")
        return
    # First start: publish current state so the site is never stale.
    sync("initial")

    _log(f"watching for changes every {args.interval}s (Ctrl+C to stop)")
    while True:
        time.sleep(args.interval)
        try:
            current = fingerprint()
            if current != last:
                sync(f"change detected {last} -> {current}")
                last = current
        except httpx.HTTPError as exc:
            # Transient API/network failure: keep watching, retry next tick.
            _log(f"deploy failed, will retry: {exc}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _log(f"unexpected error, watcher continues: {exc}")


if __name__ == "__main__":
    main()
