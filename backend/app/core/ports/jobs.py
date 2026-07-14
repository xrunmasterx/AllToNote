from typing import Protocol

from app.core.domain.video import JobState
from app.core.jobs.model import Attempt, Challenge, Job


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


class AttemptStoragePort(Protocol):
    """Boundary for private attempt staging and checkpoints."""
