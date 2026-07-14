from typing import Protocol

from app.core.domain.video import JobState
from app.core.jobs.model import (
    Attempt,
    Challenge,
    CheckpointMetadata,
    CheckpointRecord,
    Job,
    JobEvent,
)
from app.core.jobs.resource_lease import ExecutionAuthority


class JobRepositoryPort(Protocol):
    """Boundary for durable job state and execution records."""

    def create_job(
        self,
        *,
        request_hash: str,
        principal: str,
        client_request_id: str | None,
        retry_of_job_id: str | None = None,
    ) -> Job: ...

    def get_job_details(
        self, job_id: str
    ) -> tuple[Job, Attempt | None, Challenge | None]: ...

    def cancel_job(self, job_id: str) -> Job: ...

    def respond_challenge_atomic(
        self,
        job_id: str,
        challenge_id: str,
        *,
        response_hash: str,
        response_json: str,
    ) -> tuple[Job, Attempt]: ...

    def create_retry_job_atomic(
        self,
        original_job_id: str,
        *,
        expected_original_state: JobState,
        confirmed_unknown_operation_ids: tuple[str, ...],
        client_request_id: str,
    ) -> Job: ...


class AttemptMetadataRepositoryPort(Protocol):
    """Durable metadata boundary for checkpoints and Job events."""

    def record_checkpoint(
        self,
        metadata: CheckpointMetadata,
        authority: ExecutionAuthority,
    ) -> CheckpointMetadata: ...

    def latest_checkpoint(
        self, job_id: str, step_id: str
    ) -> CheckpointMetadata | None: ...

    def append_event(
        self, job_id: str, event_type: str, payload_json: str
    ) -> JobEvent: ...

    def list_events(
        self, job_id: str, after_sequence: int = 0
    ) -> tuple[JobEvent, ...]: ...


class AttemptStoragePort(Protocol):
    """Boundary for private attempt staging and checkpoints."""

    def save_checkpoint(
        self, record: CheckpointRecord, authority: ExecutionAuthority
    ) -> CheckpointMetadata: ...

    def validate_checkpoint(
        self,
        metadata: CheckpointMetadata,
        *,
        expected_schema_id: str,
        expected_input_hash: str,
    ) -> bool: ...

    def append_event(
        self, job_id: str, event_type: str, payload_json: str
    ) -> JobEvent: ...

    def reconcile_event_projection(self, job_id: str) -> tuple[JobEvent, ...]: ...
