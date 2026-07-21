from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    cancellation_requested: bool
    retry_of_job_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    job_id: str
    step_id: str
    state: AttemptState
    fencing_token: int
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


@dataclass(frozen=True)
class CheckpointRecord:
    job_id: str
    step_id: str
    attempt_id: str
    schema_id: str
    input_hash: str
    payload: bytes
    metadata_json: str

    def __post_init__(self) -> None:
        if type(self.payload) is not bytes:
            raise TypeError("Checkpoint payload must be immutable bytes")


@dataclass(frozen=True)
class CheckpointMetadata:
    checkpoint_id: str
    job_id: str
    step_id: str
    attempt_id: str
    relative_path: str
    schema_id: str
    input_hash: str
    output_hash: str
    byte_length: int
    metadata_json: str
    created_at: str


@dataclass(frozen=True)
class JobEvent:
    event_id: str
    job_id: str
    sequence: int
    event_type: str
    payload_json: str
    created_at: str


__all__ = [
    "Attempt",
    "AttemptState",
    "Challenge",
    "ChallengeState",
    "CheckpointMetadata",
    "CheckpointRecord",
    "Job",
    "JobEvent",
    "JobState",
]
