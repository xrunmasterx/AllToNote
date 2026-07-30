from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionBinding, JobSnapshot


class JobExecutionBindingReader(Protocol):
    def get_job_execution_binding(self, job_id: str) -> JobExecutionBinding: ...


class JobExecutor(Protocol):
    def wait_job(self, job_id: str) -> JobSnapshot: ...

    def get_job(self, job_id: str) -> JobSnapshot: ...

    def cancel_job(self, job_id: str) -> JobSnapshot: ...


class JobExecutionRouter:
    """Resolve the exact persisted executor without resubmitting a Job."""

    def __init__(
        self,
        binding_reader: JobExecutionBindingReader,
        executors: Iterable[tuple[JobExecutionBinding, JobExecutor]],
    ) -> None:
        resolved: dict[JobExecutionBinding, JobExecutor] = {}
        for binding, executor in executors:
            if not isinstance(binding, JobExecutionBinding) or binding in resolved:
                raise DomainError(
                    "job_executor_registration_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Job executor registration is invalid",
                )
            resolved[binding] = executor
        self._binding_reader = binding_reader
        self._executors = resolved

    def wait_job(self, job_id: str) -> JobSnapshot:
        return self._resolve(job_id).wait_job(job_id)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._resolve(job_id).get_job(job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._resolve(job_id).cancel_job(job_id)

    def _resolve(self, job_id: str) -> JobExecutor:
        binding = self._binding_reader.get_job_execution_binding(job_id)
        executor = self._executors.get(binding)
        if executor is None:
            raise DomainError(
                "job_executor_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The exact persisted Job executor is unavailable",
            )
        return executor


__all__ = ["JobExecutionRouter", "JobExecutor"]
