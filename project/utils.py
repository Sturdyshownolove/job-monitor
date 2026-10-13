"""Reusable helpers with no business logic (Project Structure.md)."""

from __future__ import annotations

import hashlib


def generate_job_hash(title: str, company: str, url: str) -> str:
    """SHA256 fingerprint from title, company and canonical URL (ADR-009).

    Deterministic: identical input always yields the identical hash.
    Raises ValueError for empty components — incomplete jobs must never
    receive a fingerprint.
    """
    parts = (title, company, url)
    if any(not part or not part.strip() for part in parts):
        raise ValueError("hash requires non-empty title, company and url")
    payload = "|".join(part.strip() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
