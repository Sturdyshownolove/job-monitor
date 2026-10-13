"""Normalizer (Normalizer.md).

Transforms raw scraper output into clean, consistent values before the
Filtering Pipeline. Deterministic, idempotent, stateless: normalizing twice
always produces the same output as normalizing once.
"""

from __future__ import annotations

import html
import re
import unicodedata
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class WorkMode(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    PRINCIPAL = "principal"
    STAFF = "staff"
    DIRECTOR = "director"
    HEAD = "head"


# Locations resolving to Germany (Filtering Pipeline.md + Indeed Extraction Rules.md)
_GERMANY_MARKERS: frozenset[str] = frozenset(
    {
        "germany",
        "deutschland",
        "berlin",
        "munich",
        "münchen",
        "hamburg",
        "frankfurt",
        "cologne",
        "köln",
        "stuttgart",
        "düsseldorf",
        "duesseldorf",
        "leipzig",
        "bonn",
    }
)

# Parenthetical gender/marketing tags, e.g. (m/f/d), (all genders), [gn]
_GENDER_TAG_RE = re.compile(
    r"\s*[(\[][^)\]]*(?:\bm/w\b|\bw/m\b|\bm/f\b|\bf/m\b|\ball genders\b|\bgn\b|\bd/w/m\b)"
    r"[^)\]]*[)\]]",
    re.IGNORECASE,
)

# Parenthetical work-mode tags, e.g. (Remote), (Hybrid Germany)
_WORK_MODE_TAG_RE = re.compile(
    r"\s*\(\s*(?:remote|hybrid|on-?site)(?:\s+germany)?\s*\)", re.IGNORECASE
)

# Title suffix separators: " | ...", " - ...", " – ..." (spaced only, so
# "UX/UI" and hyphenated words survive)
_TITLE_SUFFIX_RE = re.compile(r"\s+[|–—-]\s+.*$")

_WHITESPACE_RE = re.compile(r"\s+")

_TRACKING_PARAMS: frozenset[str] = frozenset(
    {"trk", "trackingid", "ref", "refid", "gclid", "fbclid", "mkt_tok"}
)

_SENIORITY_ORDER: tuple[tuple[Seniority, str], ...] = (
    (Seniority.INTERN, r"\bintern(?:ship)?\b"),
    (Seniority.JUNIOR, r"\bjunior\b"),
    (Seniority.PRINCIPAL, r"\bprincipal\b"),
    (Seniority.STAFF, r"\bstaff\b"),
    (Seniority.DIRECTOR, r"\bdirector\b"),
    (Seniority.HEAD, r"\bhead\b"),
    (Seniority.LEAD, r"\blead\b"),
    (Seniority.SENIOR, r"\bsenior\b"),
    (Seniority.MID, r"\bmid(?:-level)?\b"),
)


class Normalizer:
    """Stateless normalization component. Receives no mutable dependencies."""

    def normalize_text(self, value: str | None) -> str | None:
        """Base cleanup: entities, unicode NFC, invisible chars, whitespace.

        Empty results become None (Filtering Pipeline.md).
        """
        if value is None:
            return None
        text = html.unescape(value)
        text = unicodedata.normalize("NFC", text)
        text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf"} or ch in "\n\t")
        text = _WHITESPACE_RE.sub(" ", text).strip()
        return text or None

    def normalize_title(self, value: str | None) -> str | None:
        """Title cleanup maximizing duplicate detection (Normalizer.md).

        "Senior Product Designer (m/f/d)" / "| Berlin" / "(Hybrid)" all
        collapse to "Senior Product Designer".
        """
        text = self.normalize_text(value)
        if text is None:
            return None
        text = _GENDER_TAG_RE.sub("", text)
        text = _WORK_MODE_TAG_RE.sub("", text)
        text = _TITLE_SUFFIX_RE.sub("", text)
        text = _WHITESPACE_RE.sub(" ", text).strip(" -|")
        return text or None

    def normalize_company(self, value: str | None) -> str | None:
        """Company cleanup. Preserves legal suffixes (GmbH, AG, SE, Ltd., Inc)."""
        text = self.normalize_text(value)
        if text is None:
            return None
        # Trailing separators only — a final period may belong to "Ltd."
        text = text.rstrip(",;:|").strip()
        return text or None

    def normalize_location(self, value: str | None) -> str | None:
        """Location cleanup: "Berlin, Germany" → "Berlin", "Berlin / Remote" → "Berlin"."""
        text = self.normalize_text(value)
        if text is None:
            return None
        text = re.split(r"\s+/\s+", text)[0]
        text = re.sub(r",\s*(?:germany|deutschland)\s*$", "", text, flags=re.IGNORECASE)
        return text.strip() or None

    def detect_country(self, location: str | None) -> str | None:
        """Return "Germany" when the location resolves to Germany, else None."""
        if location is None:
            return None
        lowered = location.lower()
        if any(marker in lowered for marker in _GERMANY_MARKERS):
            return "Germany"
        return None

    def is_germany_eligible(self, location: str | None) -> bool:
        """Germany itself, or a remote region that includes Germany
        (Europe / EU / EMEA / worldwide). Used by the pipeline country stage
        so remote-first sources aren't rejected for lacking a city name."""
        if self.detect_country(location) == "Germany":
            return True
        if location is None:
            return False
        lowered = location.lower()
        return any(
            marker in lowered
            for marker in ("europe", "european union", "emea", "worldwide", "anywhere")
        )

    def detect_work_mode(self, value: str | None) -> WorkMode:
        if value is None:
            return WorkMode.UNKNOWN
        lowered = value.lower()
        if "hybrid" in lowered:
            return WorkMode.HYBRID
        if "remote" in lowered:
            return WorkMode.REMOTE
        if "on-site" in lowered or "onsite" in lowered or "vor ort" in lowered:
            return WorkMode.ONSITE
        return WorkMode.UNKNOWN

    def detect_seniority(self, title: str | None) -> Seniority | None:
        if title is None:
            return None
        lowered = title.lower()
        for seniority, pattern in _SENIORITY_ORDER:
            if re.search(pattern, lowered):
                return seniority
        return None

    def normalize_url(self, value: str | None) -> str | None:
        """Canonical URL: drop fragment, tracking/UTM params, trailing slash."""
        text = self.normalize_text(value)
        if text is None:
            return None
        parts = urlsplit(text)
        if not parts.scheme or not parts.netloc:
            return text
        query = [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
        ]
        path = parts.path.rstrip("/") if parts.path != "/" else ""
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
        )
