from __future__ import annotations

from dataclasses import replace

import pytest

from app.core.application.job_execution_router import JobExecutionRouter
from app.core.domain.video import JobSnapshot, JobState
from app.core.errors import DomainError
from app.core.jobs.model import JobExecutionBinding


_BINDING = JobExecutionBinding(
    recipe_id="alltonote.document-note",
    recipe_version=1,
    executor_id="alltonote.document",
    executor_version=1,
    pack_id="document-basic",
    pack_version="docling-2.117.0",
)


class _BindingReader:
    def __init__(self, binding: JobExecutionBinding) -> None:
        self.binding = binding
        self.lookups: list[str] = []

    def get_job_execution_binding(self, job_id: str) -> JobExecutionBinding:
        self.lookups.append(job_id)
        return self.binding


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _snapshot(self, operation: str, job_id: str) -> JobSnapshot:
        self.calls.append((operation, job_id))
        return JobSnapshot(job_id, JobState.QUEUED, False, None, None, None, None, None)

    def wait_job(self, job_id: str) -> JobSnapshot:
        return self._snapshot("wait", job_id)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._snapshot("get", job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._snapshot("cancel", job_id)


def test_router_dispatches_every_operation_by_exact_persisted_binding() -> None:
    reader = _BindingReader(_BINDING)
    executor = _Executor()
    router = JobExecutionRouter(reader, ((_BINDING, executor),))

    assert router.get_job("job_1").job_id == "job_1"
    assert router.wait_job("job_1").job_id == "job_1"
    assert router.cancel_job("job_1").job_id == "job_1"

    assert reader.lookups == ["job_1", "job_1", "job_1"]
    assert executor.calls == [
        ("get", "job_1"),
        ("wait", "job_1"),
        ("cancel", "job_1"),
    ]


def test_router_fails_closed_when_exact_pack_version_is_missing() -> None:
    reader = _BindingReader(replace(_BINDING, pack_version="docling-2.118.0"))
    router = JobExecutionRouter(reader, ((_BINDING, _Executor()),))

    with pytest.raises(DomainError, match="job_executor_unavailable"):
        router.wait_job("job_1")


def test_router_rejects_duplicate_exact_registration() -> None:
    executor = _Executor()
    with pytest.raises(DomainError, match="job_executor_registration_invalid"):
        JobExecutionRouter(
            _BindingReader(_BINDING),
            ((_BINDING, executor), (_BINDING, executor)),
        )
