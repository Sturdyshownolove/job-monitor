"""Job domain model (Job Model.md v2.0).

Immutable Pydantic v2 model. Construct through `Job.create(...)`, which
computes `hash` and `discovered_at` from already-normalized input and then
freezes the instance. Contains no business logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from utils import generate_job_hash

SUPPORTED_SOURCES: frozenset[str] = frozenset(
    {"LinkedIn", "Indeed", "StepStone", "RemoteRocketShip"}
)


class Job(BaseModel):
    """One normalized job posting."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str = Field(min_length=1)
    url: str
    source: str
    posted_time: str | None = None
    # Language requirement as stated on the listing card, when the source
    # exposes one (e.g. RemoteRocketship's "German Required" pill). Consumed
    # by the language filter; not persisted, never part of the hash.
    language_note: str | None = None
    discovered_at: datetime
    hash: str = Field(min_length=64, max_length=64)

    @field_validator("title", "company", "location")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        # Job Model.md: URL must begin with https://
        if not value.startswith("https://"):
            raise ValueError("url must begin with https://")
        return value

    @field_validator("source")
    @classmethod
    def _supported_source(cls, value: str) -> str:
        if value not in SUPPORTED_SOURCES:
            raise ValueError(f"source must be one of {sorted(SUPPORTED_SOURCES)}")
        return value

    @classmethod
    def create(
        cls,
        *,
        title: str,
        company: str,
        location: str,
        url: str,
        source: str,
        posted_time: str | None = None,
        language_note: str | None = None,
        discovered_at: datetime | None = None,
    ) -> "Job":
        """Factory: derives hash and discovery timestamp, returns frozen Job."""
        return cls(
            title=title,
            company=company,
            location=location,
            url=url,
            source=source,
            posted_time=posted_time,
            language_note=language_note,
            discovered_at=discovered_at if discovered_at is not None else datetime.now(UTC),
            hash=generate_job_hash(title, company, url),
        )

    # Job Model.md: two jobs are equal when their hashes match.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Job) and self.hash == other.hash

    def __hash__(self) -> int:
        return hash(self.hash)
