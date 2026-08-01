from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import Attempt, AttemptState, CheckpointMetadata, CheckpointRecord
from app.core.jobs.resource_lease import ExecutionAuthority, JobExecutionAuthority
from app.core.ports.jobs import (
    AttemptStoragePort,
    JobClaimRepositoryPort,
    JobExecutionRepositoryPort,
)


_T = TypeVar("_T")
_AUTHORITY_LOSS_CODES = frozenset(
    {"attempt_fenced", "job_claim_fenced", "scheduler_lease_lost"}
)


class _CheckpointRepositoryPort(
    JobExecutionRepositoryPort,
    JobClaimRepositoryPort,
    Protocol,
):
    pass


def _checkpoint_error() -> DomainError:
    return DomainError(
        "checkpoint_content_invalid",
        ErrorCategory.INTERNAL,
        "Checkpoint content is invalid",
    )


def _decode_success_marker(payload: bytes, step_id: str) -> None:
    try:
        marker = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError):
        raise _checkpoint_error() from None
    if (
        type(marker) is not dict
        or frozenset(marker) != frozenset({"state", "step"})
        or marker != {"state": "succeeded", "step": step_id}
    ):
        raise _checkpoint_error()


@dataclass(frozen=True)
class CheckpointedStepExecutionContext:
    job_id: str
    step_id: str
    attempt_id: str
    authority: ExecutionAuthority
    heartbeat: Callable[[], None]


class CheckpointedStepRunner:
    """Run one durable Job step with checkpoint reuse and lease heartbeats."""

    def __init__(
        self,
        repository: _CheckpointRepositoryPort,
        attempt_storage: AttemptStoragePort,
        *,
        checkpoint_reader: Callable[[CheckpointMetadata], bytes],
        checkpoint_schema: str,
        scheduler_lease_ttl_seconds: int,
        heartbeat_interval_seconds: float,
        additional_heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._repository = repository
        self._attempt_storage = attempt_storage
        self._checkpoint_reader = checkpoint_reader
        self._checkpoint_schema = checkpoint_schema
        self._scheduler_lease_ttl_seconds = scheduler_lease_ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._additional_heartbeat = additional_heartbeat

    def run(
        self,
        job_id: str,
        step_id: str,
        input_hash: str,
        authority: ExecutionAuthority,
        action: Callable[[CheckpointedStepExecutionContext], _T],
        *,
        encode: Callable[[_T], bytes] | None = None,
        decode: Callable[[bytes], _T] | None = None,
        reuse: bool = True,
        resumed_attempt: Attempt | None = None,
    ) -> _T:
        self._heartbeat(authority)
        existing = self._repository.latest_checkpoint(job_id, step_id)
        if reuse and existing is not None and self._attempt_storage.validate_checkpoint(
            existing,
            expected_schema_id=self._checkpoint_schema,
            expected_input_hash=input_hash,
        ):
            try:
                payload = self._checkpoint_reader(existing)
                if decode is not None:
                    value = decode(payload)
                else:
                    _decode_success_marker(payload, step_id)
                    value = None  # type: ignore[assignment]
            except (DomainError, OSError, RuntimeError, TypeError, ValueError):
                pass
            else:
                if self._is_resumed_step(resumed_attempt, job_id, step_id):
                    self._repository.transition_attempt(
                        resumed_attempt.attempt_id,
                        AttemptState.SUCCEEDED,
                        authority=authority,
                    )
                self._heartbeat(authority)
                return value
        if self._is_resumed_step(resumed_attempt, job_id, step_id):
            attempt = resumed_attempt
        elif isinstance(authority, JobExecutionAuthority):
            attempt = self._repository.create_attempt(
                job_id,
                step_id,
                authority=authority,
            )
            attempt = self._repository.start_attempt(attempt.attempt_id, authority)
        else:
            attempt = self._repository.create_attempt(job_id, step_id)
            attempt = self._repository.start_attempt(attempt.attempt_id, authority)
        try:
            self._heartbeat(authority)
            execution = CheckpointedStepExecutionContext(
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                authority=authority,
                heartbeat=lambda: self._heartbeat(authority),
            )
            value = self._run_action(action, execution)
            self._heartbeat(authority)
            payload = (
                encode(value)
                if encode is not None
                else json.dumps(
                    {"step": step_id, "state": "succeeded"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            self._attempt_storage.save_checkpoint(
                CheckpointRecord(
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt.attempt_id,
                    schema_id=self._checkpoint_schema,
                    input_hash=input_hash,
                    payload=payload,
                    metadata_json="{}",
                ),
                authority,
            )
        except DomainError as error:
            try:
                if error.code == "external_outcome_unknown":
                    self._repository.pause_for_external_outcome_atomic(
                        job_id,
                        attempt.attempt_id,
                        authority,
                    )
                else:
                    self._repository.transition_attempt(
                        attempt.attempt_id,
                        (
                            AttemptState.CANCELLED
                            if error.category is ErrorCategory.CANCELLED
                            else AttemptState.FAILED
                        ),
                        authority=authority,
                    )
            except DomainError as convergence_error:
                if convergence_error.code not in _AUTHORITY_LOSS_CODES:
                    raise
            raise
        except BaseException:
            try:
                self._repository.transition_attempt(
                    attempt.attempt_id,
                    AttemptState.FAILED,
                    authority=authority,
                )
            except DomainError as convergence_error:
                if convergence_error.code not in _AUTHORITY_LOSS_CODES:
                    raise
            raise
        self._repository.transition_attempt(
            attempt.attempt_id,
            AttemptState.SUCCEEDED,
            authority=authority,
        )
        return value

    def _run_action(
        self,
        action: Callable[[CheckpointedStepExecutionContext], _T],
        execution: CheckpointedStepExecutionContext,
    ) -> _T:
        stop = threading.Event()
        heartbeat_failures: list[BaseException] = []

        def heartbeat_until_stopped() -> None:
            try:
                while not stop.wait(self.heartbeat_interval_seconds):
                    execution.heartbeat()
            except BaseException as error:
                heartbeat_failures.append(error)
                stop.set()

        worker = threading.Thread(
            target=heartbeat_until_stopped,
            name=f"alltonote-scheduler-heartbeat-{execution.attempt_id}",
        )
        worker.start()
        try:
            value = action(execution)
        except BaseException:
            stop.set()
            worker.join()
            raise
        stop.set()
        worker.join()
        if heartbeat_failures:
            raise heartbeat_failures[0]
        return value

    @staticmethod
    def _is_resumed_step(
        attempt: Attempt | None,
        job_id: str,
        step_id: str,
    ) -> bool:
        return (
            attempt is not None
            and attempt.job_id == job_id
            and attempt.step_id == step_id
            and attempt.state is AttemptState.RUNNING
        )

    def _heartbeat(self, authority: ExecutionAuthority) -> None:
        if isinstance(authority, JobExecutionAuthority):
            self._repository.heartbeat_job_claim(
                authority,
                ttl_seconds=self._scheduler_lease_ttl_seconds,
            )
        else:
            self._repository.heartbeat_scheduler_lease(
                authority,
                ttl_seconds=self._scheduler_lease_ttl_seconds,
            )
        if self._additional_heartbeat is not None:
            self._additional_heartbeat()


__all__ = ["CheckpointedStepExecutionContext", "CheckpointedStepRunner"]
