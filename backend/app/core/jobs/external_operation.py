from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.jobs.resource_lease import ExecutionAuthority


class ExternalOutcome(StrEnum):
    PREPARED = "external_outcome_prepared"
    STARTED = "external_outcome_started"
    SUCCEEDED = "external_outcome_succeeded"
    FAILED = "external_outcome_failed"
    UNKNOWN = "external_outcome_unknown"


@dataclass(frozen=True)
class ExternalOperation:
    operation_id: str
    job_id: str
    step_id: str
    attempt_id: str
    provider: str
    request_hash: str
    operation_idempotency_key: str | None
    provider_request_id: str | None
    outcome: ExternalOutcome
    summary_json: str
    created_at: str
    updated_at: str


class _ExternalOperationStore(Protocol):
    def prepare_external_operation(
        self,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        provider: str,
        request_hash: str,
        operation_idempotency_key: str | None,
        summary_json: str,
    ) -> ExternalOperation: ...

    def start_external_operation(
        self, operation_id: str, authority: ExecutionAuthority
    ) -> ExternalOperation: ...

    def finish_external_operation(
        self,
        operation_id: str,
        outcome: ExternalOutcome,
        *,
        provider_request_id: str | None,
        summary_json: str,
    ) -> ExternalOperation: ...

    def get_external_operation(self, operation_id: str) -> ExternalOperation: ...

    def reconcile_external_operations_after_process_loss(
        self,
        job_id: str,
        authority: ExecutionAuthority,
    ) -> tuple[ExternalOperation, ...]: ...


class ExternalOperationGuard:
    def __init__(
        self,
        store: _ExternalOperationStore,
        authority: ExecutionAuthority,
    ) -> None:
        self._store = store
        self._authority = authority

    def prepare(
        self,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        provider: str,
        request_hash: str,
        summary_json: str,
        operation_idempotency_key: str | None = None,
    ) -> ExternalOperation:
        return self._store.prepare_external_operation(
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            provider=provider,
            request_hash=request_hash,
            operation_idempotency_key=operation_idempotency_key,
            summary_json=summary_json,
        )

    def start(self, operation_id: str) -> ExternalOperation:
        return self._store.start_external_operation(
            operation_id, self._authority
        )

    def succeed(
        self,
        operation_id: str,
        *,
        provider_request_id: str | None,
        summary_json: str,
    ) -> ExternalOperation:
        return self._store.finish_external_operation(
            operation_id,
            ExternalOutcome.SUCCEEDED,
            provider_request_id=provider_request_id,
            summary_json=summary_json,
        )

    def fail(
        self, operation_id: str, *, summary_json: str
    ) -> ExternalOperation:
        return self._store.finish_external_operation(
            operation_id,
            ExternalOutcome.FAILED,
            provider_request_id=None,
            summary_json=summary_json,
        )

    def get(self, operation_id: str) -> ExternalOperation:
        return self._store.get_external_operation(operation_id)

    def reconcile_after_process_loss(
        self, job_id: str
    ) -> tuple[ExternalOperation, ...]:
        return self._store.reconcile_external_operations_after_process_loss(
            job_id, self._authority
        )


__all__ = ["ExternalOperation", "ExternalOperationGuard", "ExternalOutcome"]
