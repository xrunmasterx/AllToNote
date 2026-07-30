from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.errors import ErrorDetail


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
class JobExecutionBinding:
    recipe_id: str
    recipe_version: int
    executor_id: str
    executor_version: int
    pack_id: str
    pack_version: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (
                self.recipe_id,
                self.executor_id,
                self.pack_id,
                self.pack_version,
            )
        ) or any(
            type(value) is not int or value < 1
            for value in (self.recipe_version, self.executor_version)
        ):
            raise ValueError("Job execution binding is invalid")


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
class JobSnapshot:
    job_id: str
    state: JobState
    cancellation_requested: bool
    active_attempt_id: str | None
    challenge_id: str | None
    retry_of_job_id: str | None
    result: object | None
    error: ErrorDetail | None


@dataclass(frozen=True)
class RetryJobRequest:
    retry_request_schema_version: int
    client_request_id: str
    expected_original_job_state: JobState
    confirmed_unknown_operation_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "confirmed_unknown_operation_ids",
            tuple(self.confirmed_unknown_operation_ids),
        )


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
    "JobExecutionBinding",
    "JobSnapshot",
    "JobEvent",
    "JobState",
    "RetryJobRequest",
]
