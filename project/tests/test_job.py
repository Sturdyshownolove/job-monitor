"""Job model tests (Job Model.md unit-test checklist)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from models import Job


def make_job(**overrides: object) -> Job:
    defaults: dict[str, object] = {
        "title": "Senior Product Designer",
        "company": "Miro",
        "location": "Berlin",
        "url": "https://example.com/jobs/1",
        "source": "LinkedIn",
        "posted_time": "2 hours ago",
    }
    defaults.update(overrides)
    return Job.create(**defaults)  # type: ignore[arg-type]


class TestFactory:
    def test_hash_computed(self) -> None:
        job = make_job()
        assert len(job.hash) == 64

    def test_hash_deterministic(self) -> None:
        assert make_job().hash == make_job().hash

    def test_discovered_at_utc(self) -> None:
        job = make_job()
        assert job.discovered_at.tzinfo is not None
        assert job.discovered_at.utcoffset().total_seconds() == 0

    def test_explicit_discovered_at(self) -> None:
        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        assert make_job(discovered_at=stamp).discovered_at == stamp

    def test_posted_time_optional(self) -> None:
        assert make_job(posted_time=None).posted_time is None


class TestEquality:
    def test_equal_when_hash_matches(self) -> None:
        a = make_job(posted_time="1 hour ago")
        b = make_job(posted_time="3 days ago")  # hash fields identical
        assert a == b
        assert hash(a) == hash(b)

    def test_not_equal_when_url_differs(self) -> None:
        assert make_job() != make_job(url="https://example.com/jobs/2")

    def test_not_equal_to_other_types(self) -> None:
        assert make_job() != "job"


class TestValidation:
    def test_empty_title_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            make_job(title="   ")

    def test_empty_company_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            make_job(company="")

    def test_http_url_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            make_job(url="http://example.com/jobs/1")

    def test_unsupported_source_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            make_job(source="Glassdoor")

    @pytest.mark.parametrize("source", ["LinkedIn", "Indeed", "StepStone", "RemoteRocketShip"])
    def test_supported_sources(self, source: str) -> None:
        assert make_job(source=source).source == source


class TestImmutability:
    def test_frozen(self) -> None:
        job = make_job()
        with pytest.raises(ValidationError):
            job.title = "Other"  # type: ignore[misc]


class TestSerialization:
    def test_dict_roundtrip(self) -> None:
        job = make_job()
        data = job.model_dump()
        assert data["title"] == "Senior Product Designer"
        restored = Job(**data)
        assert restored == job

    def test_json(self) -> None:
        payload = make_job().model_dump_json()
        assert "Senior Product Designer" in payload
