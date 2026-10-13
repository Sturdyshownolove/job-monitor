"""Hash generator tests (Database.md / SHA256 checklist)."""

from __future__ import annotations

import pytest

from utils import generate_job_hash


def test_same_input_same_hash() -> None:
    a = generate_job_hash("Product Designer", "Miro", "https://x.com/1")
    b = generate_job_hash("Product Designer", "Miro", "https://x.com/1")
    assert a == b


def test_different_url_different_hash() -> None:
    a = generate_job_hash("Product Designer", "Miro", "https://x.com/1")
    b = generate_job_hash("Product Designer", "Miro", "https://x.com/2")
    assert a != b


def test_different_company_different_hash() -> None:
    a = generate_job_hash("Product Designer", "Miro", "https://x.com/1")
    b = generate_job_hash("Product Designer", "Figma", "https://x.com/1")
    assert a != b


def test_sha256_hex_format() -> None:
    value = generate_job_hash("t", "c", "https://x.com")
    assert len(value) == 64
    int(value, 16)  # valid hex


@pytest.mark.parametrize("title,company,url", [("", "c", "u"), ("t", " ", "u"), ("t", "c", "")])
def test_empty_values_rejected(title: str, company: str, url: str) -> None:
    with pytest.raises(ValueError):
        generate_job_hash(title, company, url)
