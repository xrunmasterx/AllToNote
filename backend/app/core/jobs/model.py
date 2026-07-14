from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.domain.video import JobState


class AttemptState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    NEEDS_INPUT = "needs_input"
    SKIPPED = "skipped"


class ChallengeState(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"


@dataclass(frozen=True)
class Job:
    job_id: str
    request_hash: str
    principal: str
    client_request_id: str | None
    state: JobState
    retry_of_job_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    job_id: str
    step_id: str
    state: AttemptState
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Challenge:
    challenge_id: str
    job_id: str
    attempt_id: str
    state: ChallengeState
    prompt_json: str
    response_json: str | None
    response_hash: str | None
    response_attempt_id: str | None
    created_at: str
    updated_at: str


__all__ = [
    "Attempt",
    "AttemptState",
    "Challenge",
    "ChallengeState",
    "Job",
    "JobState",
]
