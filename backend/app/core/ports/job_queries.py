from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.domain.video import JobState, VideoProduceResult
from app.core.errors import ErrorDetail
from app.core.jobs.model import Attempt, Challenge, Job, JobEvent


@dataclass(frozen=True)
class JobQueryRecord:
    job: Job
    active_attempt: Attempt | None
    pending_challenge: Challenge | None
    result: VideoProduceResult | None
    error: ErrorDetail | None
    unknown_operation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "unknown_operation_ids",
            tuple(self.unknown_operation_ids),
        )


@dataclass(frozen=True)
class JobListRecord:
    job: Job
    result: VideoProduceResult | None
    error: ErrorDetail | None


class JobQueryRepositoryPort(Protocol):
    """Read-only, principal-scoped projection boundary for public Job queries."""

    def query_job(self, job_id: str, *, principal: str) -> JobQueryRecord: ...

    def query_jobs(
        self,
        *,
        principal: str,
        states: tuple[JobState, ...],
        before_created_at: str | None,
        before_job_id: str | None,
        limit: int,
    ) -> tuple[JobListRecord, ...]: ...

    def query_job_events(
        self,
        job_id: str,
        *,
        principal: str,
        after_sequence: int,
        limit: int,
    ) -> tuple[JobEvent, ...]: ...


__all__ = [
    "JobListRecord",
    "JobQueryRecord",
    "JobQueryRepositoryPort",
]
