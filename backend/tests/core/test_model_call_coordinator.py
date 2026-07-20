from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.models.model_result_store import ModelOperationResultStore
from app.core.application.model_call_coordinator import (
    ModelCallCoordinator,
    ModelCallExecution,
    StoredModelOperationResult,
)
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.cancellation import CancellationToken
from app.core.jobs.external_operation import ExternalOperationGuard, ExternalOutcome
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.ports.model_executor import (
    ModelExecutionBinding,
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelFinishReason,
    ModelOutputMode,
)


class _NeverCancelled:
    def raise_if_cancelled(self) -> None:
        return None


class _SequenceExecutor:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls = 0

    def complete(self, request: ModelExecutionRequest, token: object) -> ModelExecutionResult:
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]


class _FailingResultStore(ModelOperationResultStore):
    def save(
        self,
        operation_id: str,
        request_hash: str,
        result: ModelExecutionResult,
    ) -> StoredModelOperationResult:
        raise DomainError(
            "external_result_unavailable",
            ErrorCategory.CONFLICT,
            "fixture store failed",
        )


def _binding(**changes: object) -> ModelExecutionBinding:
    value = ModelExecutionBinding(
        schema_version=1,
        provider_type="openai-compatible",
        model_identity="provider/model-v1",
        credential_profile_ref="profile-local-default",
        context_window_tokens=32_000,
        max_output_tokens=4_000,
        max_concurrency=2,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )
    return replace(value, **changes)


def _request(**changes: object) -> ModelExecutionRequest:
    value = ModelExecutionRequest(
        schema_version=1,
        stage_id="knowledge-map",
        stage_version=1,
        prompt_id="extract-knowledge",
        prompt_version=3,
        system_instruction="Extract only supported knowledge.",
        user_content="Segment seg-0001 says hello.",
        output_mode=ModelOutputMode.JSON_SCHEMA,
        max_output_tokens=2_000,
        timeout_seconds=30,
        response_schema_json='{"type":"object","required":["items"]}',
        temperature=0.2,
    )
    return replace(value, **changes)


def _result(**changes: object) -> ModelExecutionResult:
    value = ModelExecutionResult(
        text='{"items":[]}',
        actual_model_identity="provider/model-v1",
        input_tokens=120,
        output_tokens=14,
        finish_reason=ModelFinishReason.STOP,
        provider_request_id="req_123",
        warnings=("normalized",),
    )
    return replace(value, **changes)


def _running_execution(
    tmp_path: Path,
) -> tuple[SqliteJobRepository, ModelCallExecution, CancellationToken]:
    repo = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: 1_000)
    job = repo.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = repo.acquire_scheduler_lease("process-one", ttl_seconds=30)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "compile-knowledge-map").attempt_id,
        authority,
    )
    execution = ModelCallExecution(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        authority=authority,
        heartbeat=lambda: repo.heartbeat_scheduler_lease(
            authority, ttl_seconds=30
        ),
    )
    return repo, execution, CancellationToken(repo, job.job_id)


def _operation_rows(repo: SqliteJobRepository) -> list[tuple[str, str]]:
    with repo._connect() as connection:
        return [
            (row["operation_id"], row["outcome"])
            for row in connection.execute(
                "SELECT operation_id, outcome FROM external_operations ORDER BY rowid"
            ).fetchall()
        ]


def test_request_hash_is_semantic_and_ignores_timeout_only_changes() -> None:
    base = ModelCallCoordinator.request_hash(_binding(), _request(), "chunk-0000")
    timeout_only = ModelCallCoordinator.request_hash(
        _binding(timeout_seconds=90),
        _request(timeout_seconds=45),
        "chunk-0000",
    )
    assert timeout_only == base

    variants = (
        (_binding(model_identity="provider/model-v2"), _request(), "chunk-0000"),
        (_binding(credential_profile_ref="profile-other"), _request(), "chunk-0000"),
        (_binding(), _request(prompt_version=4), "chunk-0000"),
        (_binding(), _request(user_content="different"), "chunk-0000"),
        (_binding(), _request(temperature=0.1), "chunk-0000"),
        (_binding(), _request(max_output_tokens=1_999), "chunk-0000"),
        (_binding(), _request(), "chunk-0001"),
    )
    assert all(
        ModelCallCoordinator.request_hash(binding, request, shard) != base
        for binding, request, shard in variants
    )


def test_success_is_recovered_after_repository_reopen_without_provider_call(
    tmp_path: Path,
) -> None:
    repo, execution, token = _running_execution(tmp_path)
    store_path = tmp_path / "results"
    first_executor = _SequenceExecutor(_result())
    coordinator = ModelCallCoordinator(
        operation_store=repo,
        result_store=ModelOperationResultStore(store_path),
        executor=first_executor,
    )

    first = coordinator.execute(_binding(), _request(), execution, "chunk-0000", token)
    assert first == _result()
    assert first_executor.calls == 1
    assert [outcome for _, outcome in _operation_rows(repo)] == [
        ExternalOutcome.SUCCEEDED.value
    ]

    reopened = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: 1_000)
    recovered_execution = replace(
        execution,
        heartbeat=lambda: reopened.heartbeat_scheduler_lease(
            execution.authority, ttl_seconds=30
        ),
    )
    forbidden_executor = _SequenceExecutor(
        AssertionError("successful recovery must not call the provider")
    )
    recovered = ModelCallCoordinator(
        operation_store=reopened,
        result_store=ModelOperationResultStore(store_path),
        executor=forbidden_executor,
    ).execute(
        _binding(),
        _request(),
        recovered_execution,
        "chunk-0000",
        CancellationToken(reopened, execution.job_id),
    )

    assert recovered == first
    assert forbidden_executor.calls == 0


def test_missing_success_anchor_fails_without_replaying_provider(tmp_path: Path) -> None:
    repo, execution, token = _running_execution(tmp_path)
    store_path = tmp_path / "results"
    coordinator = ModelCallCoordinator(
        operation_store=repo,
        result_store=ModelOperationResultStore(store_path),
        executor=_SequenceExecutor(_result()),
    )
    coordinator.execute(_binding(), _request(), execution, "chunk-0000", token)
    next(store_path.glob("*.model.json")).unlink()
    forbidden = _SequenceExecutor(AssertionError("must not replay a paid call"))

    with pytest.raises(DomainError, match="external_result_unavailable"):
        ModelCallCoordinator(
            operation_store=repo,
            result_store=ModelOperationResultStore(store_path),
            executor=forbidden,
        ).execute(_binding(), _request(), execution, "chunk-0000", token)
    assert forbidden.calls == 0


def test_known_retryable_failure_retries_with_a_persisted_budget(tmp_path: Path) -> None:
    repo, execution, token = _running_execution(tmp_path)
    executor = _SequenceExecutor(
        DomainError(
            "provider_busy",
            ErrorCategory.RETRYABLE_RUNTIME,
            "provider explicitly rejected the call",
        ),
        _result(),
    )
    actual = ModelCallCoordinator(
        operation_store=repo,
        result_store=ModelOperationResultStore(tmp_path / "results"),
        executor=executor,
        max_attempts=2,
    ).execute(_binding(), _request(), execution, "chunk-0000", token)

    assert actual == _result()
    assert executor.calls == 2
    assert [outcome for _, outcome in _operation_rows(repo)] == [
        ExternalOutcome.FAILED.value,
        ExternalOutcome.SUCCEEDED.value,
    ]


@pytest.mark.parametrize(
    ("code", "category"),
    (
        ("authentication_failed", ErrorCategory.POLICY_DENIED),
        ("request_invalid", ErrorCategory.INVALID_REQUEST),
        ("response_contract_invalid", ErrorCategory.RECIPE_FAILED),
    ),
)
def test_non_retryable_error_is_durable_and_never_replayed(
    tmp_path: Path,
    code: str,
    category: ErrorCategory,
) -> None:
    repo, execution, token = _running_execution(tmp_path)
    executor = _SequenceExecutor(DomainError(code, category, "sanitized failure"))
    coordinator = ModelCallCoordinator(
        operation_store=repo,
        result_store=ModelOperationResultStore(tmp_path / "results"),
        executor=executor,
    )

    with pytest.raises(DomainError) as first:
        coordinator.execute(_binding(), _request(), execution, "chunk-0000", token)
    assert first.value.code == code
    assert executor.calls == 1

    forbidden = _SequenceExecutor(AssertionError("terminal error must not replay"))
    with pytest.raises(DomainError) as recovered:
        ModelCallCoordinator(
            operation_store=repo,
            result_store=ModelOperationResultStore(tmp_path / "results"),
            executor=forbidden,
        ).execute(_binding(), _request(), execution, "chunk-0000", token)
    assert recovered.value.code == code
    assert recovered.value.category is category
    assert forbidden.calls == 0


@pytest.mark.parametrize(
    "failure",
    (
        DomainError(
            "external_outcome_unknown",
            ErrorCategory.CONFLICT,
            "provider timeout after send",
        ),
        RuntimeError("unmapped provider failure"),
    ),
)
def test_unknown_outcome_is_never_automatically_replayed(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    repo, execution, token = _running_execution(tmp_path)
    executor = _SequenceExecutor(failure)
    coordinator = ModelCallCoordinator(
        operation_store=repo,
        result_store=ModelOperationResultStore(tmp_path / "results"),
        executor=executor,
    )

    with pytest.raises(DomainError, match="external_outcome_unknown"):
        coordinator.execute(_binding(), _request(), execution, "chunk-0000", token)
    assert executor.calls == 1
    assert _operation_rows(repo)[0][1] == ExternalOutcome.UNKNOWN.value

    with pytest.raises(DomainError, match="external_outcome_unknown"):
        coordinator.execute(_binding(), _request(), execution, "chunk-0000", token)
    assert executor.calls == 1


def test_result_store_failure_becomes_unknown_and_cannot_replay(tmp_path: Path) -> None:
    repo, execution, token = _running_execution(tmp_path)
    executor = _SequenceExecutor(_result())
    coordinator = ModelCallCoordinator(
        operation_store=repo,
        result_store=_FailingResultStore(tmp_path / "results"),
        executor=executor,
    )

    with pytest.raises(DomainError, match="external_result_unavailable"):
        coordinator.execute(_binding(), _request(), execution, "chunk-0000", token)
    assert executor.calls == 1
    assert _operation_rows(repo)[0][1] == ExternalOutcome.UNKNOWN.value
    with pytest.raises(DomainError, match="external_outcome_unknown"):
        coordinator.execute(_binding(), _request(), execution, "chunk-0000", token)
    assert executor.calls == 1


def test_cancellation_after_provider_return_rejects_result_write(tmp_path: Path) -> None:
    repo, execution, token = _running_execution(tmp_path)

    class _CancellingExecutor:
        calls = 0

        def complete(
            self, request: ModelExecutionRequest, call_token: object
        ) -> ModelExecutionResult:
            self.calls += 1
            repo.cancel_job(execution.job_id)
            return _result()

    executor = _CancellingExecutor()
    with pytest.raises(DomainError, match="job_cancelled"):
        ModelCallCoordinator(
            operation_store=repo,
            result_store=ModelOperationResultStore(tmp_path / "results"),
            executor=executor,
        ).execute(_binding(), _request(), execution, "chunk-0000", token)

    assert executor.calls == 1
    assert _operation_rows(repo)[0][1] == ExternalOutcome.UNKNOWN.value
    assert not list((tmp_path / "results").glob("*.model.json"))


def test_cancellation_race_cannot_commit_late_success(tmp_path: Path) -> None:
    repo, execution, token = _running_execution(tmp_path)

    class _CancelAfterSaveStore(ModelOperationResultStore):
        def save(
            self,
            operation_id: str,
            request_hash: str,
            result: ModelExecutionResult,
        ) -> StoredModelOperationResult:
            stored = super().save(operation_id, request_hash, result)
            repo.cancel_job(execution.job_id)
            return stored

    with pytest.raises(DomainError, match="job_cancelled"):
        ModelCallCoordinator(
            operation_store=repo,
            result_store=_CancelAfterSaveStore(tmp_path / "results"),
            executor=_SequenceExecutor(_result()),
        ).execute(_binding(), _request(), execution, "chunk-0000", token)

    assert _operation_rows(repo)[0][1] == ExternalOutcome.STARTED.value


def test_post_call_fencing_rejects_late_result_write(tmp_path: Path) -> None:
    repo, execution, token = _running_execution(tmp_path)
    heartbeat_count = 0

    def fenced_after_provider() -> None:
        nonlocal heartbeat_count
        heartbeat_count += 1
        if heartbeat_count >= 3:
            raise DomainError(
                "attempt_fenced",
                ErrorCategory.CONFLICT,
                "fixture lost execution authority",
            )

    fenced_execution = replace(execution, heartbeat=fenced_after_provider)
    with pytest.raises(DomainError, match="attempt_fenced"):
        ModelCallCoordinator(
            operation_store=repo,
            result_store=ModelOperationResultStore(tmp_path / "results"),
            executor=_SequenceExecutor(_result()),
        ).execute(_binding(), _request(), fenced_execution, "chunk-0000", token)

    assert _operation_rows(repo)[0][1] == ExternalOutcome.STARTED.value
    assert not list((tmp_path / "results").glob("*.model.json"))


def test_stale_guard_cannot_finish_or_mark_unknown_after_takeover(tmp_path: Path) -> None:
    now_ms = 1_000
    repo = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: now_ms)
    job = repo.create_job(
        request_hash="sha256:" + "b" * 64,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    old_authority = repo.acquire_scheduler_lease("old-process", ttl_seconds=1)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "compile").attempt_id,
        old_authority,
    )
    old_guard = ExternalOperationGuard(repo, old_authority)
    operation = old_guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="fixture/provider",
        request_hash="c" * 64,
        summary_json="{}",
    )
    old_guard.start(operation.operation_id)

    now_ms = 2_001
    new_authority = repo.acquire_scheduler_lease("new-process", ttl_seconds=1)
    repo.take_over_running_attempt(job.job_id, attempt.attempt_id, new_authority)

    for action in (
        lambda: old_guard.succeed(
            operation.operation_id,
            provider_request_id=None,
            summary_json="{}",
        ),
        lambda: old_guard.unknown(operation.operation_id, summary_json="{}"),
    ):
        with pytest.raises(DomainError, match="attempt_fenced"):
            action()
        assert (
            repo.get_external_operation(operation.operation_id).outcome
            is ExternalOutcome.STARTED
        )


def test_model_result_store_is_idempotent_and_detects_tampering(tmp_path: Path) -> None:
    store = ModelOperationResultStore(tmp_path / "results")
    operation_id = "op_00000000-0000-0000-0000-000000000001"
    request_hash = "sha256:" + "d" * 64
    stored = store.save(operation_id, request_hash, _result())
    assert store.save(operation_id, request_hash, _result()) == stored

    summary = ModelCallCoordinator._success_summary(
        "chunk-0000", request_hash, stored
    )
    assert store.load(operation_id, request_hash, summary) == _result()
    (tmp_path / "results" / stored.relative_path).write_bytes(b"tampered")
    with pytest.raises(DomainError, match="external_result_unavailable"):
        store.load(operation_id, request_hash, summary)
