from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState
from app.core.errors import DomainError
from app.core.jobs.model import AttemptState, CheckpointRecord


TRANSCRIPT_SCHEMA = "evidence.transcript.v1"
TRANSCRIPT_PAYLOAD = b'{"record_type":"transcript_header"}\n'


class _Clock:
    def __init__(self) -> None:
        self.now_ms = 1_000_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


def _task7_api():
    cancellation = importlib.import_module("app.core.jobs.cancellation")
    external = importlib.import_module("app.core.jobs.external_operation")
    resource = importlib.import_module("app.core.jobs.resource_lease")
    return (
        cancellation.CancellationToken,
        external.ExternalOperationGuard,
        external.ExternalOutcome,
        resource.ExecutionAuthority,
    )


def _repository(tmp_path: Path, clock: _Clock) -> SqliteJobRepository:
    return SqliteJobRepository.open(tmp_path / "machine-root", clock=clock)


def _running_attempt(
    repository: SqliteJobRepository,
    *,
    owner_id: str = "workspace-a:process-a",
):
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.acquire_scheduler_lease(owner_id, ttl_seconds=30)
    pending = repository.create_attempt(job.job_id, "normalize_transcript")
    assert pending.fencing_token == 0
    running = repository.start_attempt(pending.attempt_id, authority)
    assert running.state is AttemptState.RUNNING
    assert running.fencing_token == authority.fencing_token
    return job, running, authority


def _checkpoint_record(job_id: str, attempt_id: str) -> CheckpointRecord:
    return CheckpointRecord(
        job_id=job_id,
        step_id="normalize_transcript",
        attempt_id=attempt_id,
        schema_id=TRANSCRIPT_SCHEMA,
        input_hash=sha256_digest(b"input"),
        payload=TRANSCRIPT_PAYLOAD,
        metadata_json="{}",
    )


def test_cancellation_token_reads_durable_store_and_running_attempt_settles_job(
    tmp_path: Path,
) -> None:
    CancellationToken, _, _, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, authority = _running_attempt(repository)
    token = CancellationToken(repository, job.job_id)

    assert not token.is_cancelled()
    requested = repository.cancel_job(job.job_id)

    assert requested.state is JobState.RUNNING
    assert requested.cancellation_requested is True
    with pytest.raises(DomainError, match="job_cancelled"):
        repository.create_attempt(job.job_id, "late-step")
    with pytest.raises(DomainError, match="job_cancelled"):
        token.raise_if_cancelled()

    repository.transition_attempt(
        attempt.attempt_id, AttemptState.CANCELLED, authority=authority
    )
    settled = repository.get_job(job.job_id)
    assert settled.state is JobState.CANCELLED
    assert settled.cancellation_requested is True


def test_cancel_cancels_pending_attempts_and_finishes_without_running_attempt(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    pending = repository.create_attempt(job.job_id, "resolve")

    cancelled = repository.cancel_job(job.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancellation_requested is True
    with pytest.raises(DomainError, match="attempt_terminal"):
        repository.start_attempt(
            pending.attempt_id,
            repository.acquire_scheduler_lease(
                "workspace-a:process-a", ttl_seconds=30
            ),
        )


def test_generic_attempt_transition_cannot_bypass_fenced_start(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, _Clock())
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    pending = repository.create_attempt(job.job_id, "resolve")

    with pytest.raises(DomainError, match="attempt_start_required"):
        repository.transition_attempt(pending.attempt_id, AttemptState.RUNNING)

    started = repository.start_attempt(
        pending.attempt_id,
        repository.acquire_scheduler_lease(
            "workspace-a:process-a", ttl_seconds=30
        ),
    )
    assert started.state is AttemptState.RUNNING
    assert started.fencing_token > 0


def test_expired_scheduler_owner_cannot_restart_or_checkpoint_old_attempt(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, old_attempt, old_authority = _running_attempt(repository)
    clock.advance(31_000)
    new_authority = repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=30
    )
    assert new_authority.fencing_token == old_authority.fencing_token + 1

    with pytest.raises(DomainError, match="attempt_fenced"):
        repository.start_attempt(old_attempt.attempt_id, new_authority)

    storage = FileAttemptStorage(
        tmp_path / "attempt-storage",
        repository,
        validators={TRANSCRIPT_SCHEMA: lambda payload: payload == TRANSCRIPT_PAYLOAD},
    )
    with pytest.raises(DomainError, match="attempt_fenced"):
        storage.save_checkpoint(
            _checkpoint_record(job.job_id, old_attempt.attempt_id), old_authority
        )
    assert repository.latest_checkpoint(job.job_id, old_attempt.step_id) is None


def test_scheduler_heartbeat_and_release_are_fencing_cas_operations(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    first = repository.acquire_scheduler_lease(
        "workspace-a:process-a", ttl_seconds=10
    )
    assert repository.heartbeat_scheduler_lease(
        first, ttl_seconds=20
    ) == first
    clock.advance(20_001)
    second = repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=10
    )

    assert second.fencing_token == first.fencing_token + 1
    assert repository.release_scheduler_lease(first) is False
    with pytest.raises(DomainError, match="scheduler_lease_lost"):
        repository.heartbeat_scheduler_lease(first, ttl_seconds=10)
    assert repository.release_scheduler_lease(second) is True


def test_active_authority_can_checkpoint_only_its_running_attempt(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, authority = _running_attempt(repository)
    storage = FileAttemptStorage(
        tmp_path / "attempt-storage",
        repository,
        validators={TRANSCRIPT_SCHEMA: lambda payload: payload == TRANSCRIPT_PAYLOAD},
    )

    metadata = storage.save_checkpoint(
        _checkpoint_record(job.job_id, attempt.attempt_id), authority
    )

    assert repository.latest_checkpoint(job.job_id, attempt.step_id) == metadata


def test_cancel_wins_before_commit_guard_and_late_output_cannot_commit(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, authority = _running_attempt(repository)
    repository.cancel_job(job.job_id)

    with pytest.raises(DomainError, match="commit_guard_cancelled"):
        with repository.commit_guard(job.job_id, attempt.attempt_id, authority):
            raise AssertionError("guarded rename must not run")

    repository.transition_attempt(
        attempt.attempt_id, AttemptState.CANCELLED, authority=authority
    )
    assert repository.get_job(job.job_id).state is JobState.CANCELLED


def test_commit_guard_wins_before_cancel_and_success_remains_terminal(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, authority = _running_attempt(repository)
    renamed = tmp_path / "published"

    with repository.commit_guard(job.job_id, attempt.attempt_id, authority):
        renamed.mkdir()

    committed = repository.get_job(job.job_id)
    assert renamed.is_dir()
    assert committed.state is JobState.SUCCEEDED
    assert committed.cancellation_requested is False
    assert repository.cancel_job(job.job_id) == committed


def test_commit_guard_rejects_success_while_fenced_old_attempt_is_running(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, _, _ = _running_attempt(repository)
    clock.advance(31_000)
    authority = repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=30
    )
    pending = repository.create_attempt(job.job_id, "replacement")
    replacement = repository.start_attempt(pending.attempt_id, authority)

    with pytest.raises(DomainError, match="attempt_not_settled"):
        with repository.commit_guard(
            job.job_id, replacement.attempt_id, authority
        ):
            pass

    assert repository.get_job(job.job_id).state is JobState.RUNNING


@pytest.mark.parametrize("other_state", (AttemptState.PENDING, AttemptState.RUNNING))
def test_commit_guard_rejects_other_unsettled_attempt_before_marker_runs(
    tmp_path: Path,
    other_state: AttemptState,
) -> None:
    repository = _repository(tmp_path, _Clock())
    job, attempt, authority = _running_attempt(repository)
    other = repository.create_attempt(job.job_id, "other-step")
    if other_state is AttemptState.RUNNING:
        other = repository.start_attempt(other.attempt_id, authority)
    marker: list[str] = []

    with pytest.raises(DomainError, match="attempt_not_settled"):
        with repository.commit_guard(job.job_id, attempt.attempt_id, authority):
            marker.append("published")

    assert marker == []
    assert repository.get_job(job.job_id).state is JobState.RUNNING
    with repository._connect() as connection:
        states = {
            row["attempt_id"]: AttemptState(row["state"])
            for row in connection.execute(
                "SELECT attempt_id, state FROM attempts WHERE job_id = ?",
                (job.job_id,),
            )
        }
    assert states[attempt.attempt_id] is AttemptState.RUNNING
    assert states[other.attempt_id] is other_state


def test_started_external_operation_becomes_unknown_and_cannot_restart(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, ExternalOutcome, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, authority = _running_attempt(repository)
    guard = ExternalOperationGuard(repository, authority)
    prepared = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"paid request"),
        summary_json="{}",
    )

    started = guard.start(prepared.operation_id)
    assert started.outcome is ExternalOutcome.STARTED
    clock.advance(31_000)
    new_authority = repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=30
    )
    reconciler = ExternalOperationGuard(repository, new_authority)
    reconciled = reconciler.reconcile_after_process_loss(job.job_id)
    assert len(reconciled) == 1
    assert reconciled[0].operation_id == started.operation_id
    assert reconciled[0].outcome is ExternalOutcome.UNKNOWN
    assert guard.get(prepared.operation_id).outcome is ExternalOutcome.UNKNOWN
    with pytest.raises(DomainError, match="external_outcome_unknown"):
        reconciler.start(prepared.operation_id)


def test_external_operation_binding_freezes_existing_outcome_semantics(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, ExternalOutcome, _ = _task7_api()
    repository = _repository(tmp_path, _Clock())
    job, attempt, authority = _running_attempt(repository)
    guard = ExternalOperationGuard(repository, authority)

    prepared = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"idempotent"),
        summary_json='{"call":1}',
    )
    replay = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"idempotent"),
        summary_json='{"call":2}',
    )
    assert replay == prepared

    started = guard.start(prepared.operation_id)
    with pytest.raises(DomainError, match="external_operation_in_progress"):
        guard.prepare(
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            provider="paid-provider",
            request_hash=sha256_digest(b"idempotent"),
            summary_json="{}",
        )
    succeeded = guard.succeed(
        started.operation_id,
        provider_request_id="provider-1",
        summary_json='{"status":"ok"}',
    )
    assert succeeded.outcome is ExternalOutcome.SUCCEEDED
    assert guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"idempotent"),
        summary_json="{}",
    ) == succeeded

    failed = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"retryable"),
        summary_json="{}",
    )
    guard.start(failed.operation_id)
    guard.fail(failed.operation_id, summary_json='{"status":"failed"}')
    retry = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"retryable"),
        summary_json="{}",
    )
    assert retry.operation_id != failed.operation_id
    assert retry.outcome is ExternalOutcome.PREPARED


def test_prepared_binding_rebinds_same_operation_to_current_fenced_attempt(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, ExternalOutcome, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, old_attempt, old_authority = _running_attempt(repository)
    request_hash = sha256_digest(b"prepared before takeover")
    prepared = ExternalOperationGuard(repository, old_authority).prepare(
        job_id=job.job_id,
        step_id=old_attempt.step_id,
        attempt_id=old_attempt.attempt_id,
        provider="paid-provider",
        request_hash=request_hash,
        summary_json="{}",
    )
    with repository._connect() as connection:
        before_count = connection.execute(
            "SELECT COUNT(*) FROM external_operations"
        ).fetchone()[0]

    clock.advance(31_000)
    authority = repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=30
    )
    current_attempt = repository.create_attempt(job.job_id, old_attempt.step_id)
    current_attempt = repository.start_attempt(current_attempt.attempt_id, authority)
    guard = ExternalOperationGuard(repository, authority)
    replay = guard.prepare(
        job_id=job.job_id,
        step_id=current_attempt.step_id,
        attempt_id=current_attempt.attempt_id,
        provider="paid-provider",
        request_hash=request_hash,
        summary_json='{"replayed":true}',
    )

    assert replay.operation_id == prepared.operation_id
    assert replay.attempt_id == current_attempt.attempt_id
    assert replay.summary_json == prepared.summary_json
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_operations"
        ).fetchone()[0] == before_count
    assert guard.start(replay.operation_id).outcome is ExternalOutcome.STARTED


@pytest.mark.parametrize("existing_outcome", (None, "prepared", "failed"))
def test_prepare_rejects_stale_authority_before_reuse_or_insert(
    tmp_path: Path,
    existing_outcome: str | None,
) -> None:
    _, ExternalOperationGuard, _, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, old_authority = _running_attempt(repository)
    old_guard = ExternalOperationGuard(repository, old_authority)
    request_hash = sha256_digest(b"stale prepare")
    if existing_outcome is not None:
        operation = old_guard.prepare(
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            provider="paid-provider",
            request_hash=request_hash,
            summary_json="{}",
        )
        if existing_outcome == "failed":
            old_guard.start(operation.operation_id)
            old_guard.fail(operation.operation_id, summary_json="{}")
    with repository._connect() as connection:
        before = connection.execute(
            "SELECT operation_id, attempt_id, outcome FROM external_operations"
        ).fetchall()

    clock.advance(31_000)
    repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=30
    )
    with pytest.raises(DomainError, match="attempt_fenced"):
        old_guard.prepare(
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            provider="paid-provider",
            request_hash=request_hash,
            summary_json="{}",
        )

    with repository._connect() as connection:
        after = connection.execute(
            "SELECT operation_id, attempt_id, outcome FROM external_operations"
        ).fetchall()
    assert tuple(map(tuple, after)) == tuple(map(tuple, before))


def test_prepare_validates_cancellation_and_attempt_binding_before_insert(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, _, _ = _task7_api()
    repository = _repository(tmp_path, _Clock())
    job, attempt, authority = _running_attempt(repository)
    guard = ExternalOperationGuard(repository, authority)
    repository.cancel_job(job.job_id)

    with pytest.raises(DomainError, match="job_cancelled"):
        guard.prepare(
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            provider="provider",
            request_hash=sha256_digest(b"cancelled"),
            summary_json="{}",
        )
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_operations"
        ).fetchone()[0] == 0


def test_prepare_rejects_attempt_from_another_job_before_insert(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, _, _ = _task7_api()
    repository = _repository(tmp_path, _Clock())
    job, attempt, authority = _running_attempt(repository)
    _, other_attempt, _ = _running_attempt(repository)

    with pytest.raises(DomainError, match="attempt_fenced"):
        ExternalOperationGuard(repository, authority).prepare(
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=other_attempt.attempt_id,
            provider="provider",
            request_hash=sha256_digest(b"wrong attempt"),
            summary_json="{}",
        )
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_operations"
        ).fetchone()[0] == 0


def test_prepare_rejects_attempt_from_another_step_before_insert(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, _, _ = _task7_api()
    repository = _repository(tmp_path, _Clock())
    job, attempt, authority = _running_attempt(repository)
    other = repository.create_attempt(job.job_id, "other-step")
    other = repository.start_attempt(other.attempt_id, authority)

    with pytest.raises(DomainError, match="attempt_fenced"):
        ExternalOperationGuard(repository, authority).prepare(
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=other.attempt_id,
            provider="provider",
            request_hash=sha256_digest(b"wrong step"),
            summary_json="{}",
        )
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_operations"
        ).fetchone()[0] == 0


def test_unknown_binding_blocks_prepare_and_defensive_start_but_not_new_job(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, ExternalOutcome, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, old_authority = _running_attempt(repository)
    old_guard = ExternalOperationGuard(repository, old_authority)
    request_hash = sha256_digest(b"uncertain paid request")
    operation = old_guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=request_hash,
        summary_json="{}",
    )
    old_guard.start(operation.operation_id)
    clock.advance(31_000)
    authority = repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=30
    )
    guard = ExternalOperationGuard(repository, authority)
    assert guard.reconcile_after_process_loss(job.job_id)[0].outcome is ExternalOutcome.UNKNOWN

    with repository._connect() as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM external_operations"
        ).fetchone()[0]
    with pytest.raises(DomainError, match="external_outcome_unknown"):
        guard.prepare(
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            provider="paid-provider",
            request_hash=request_hash,
            summary_json="{}",
        )
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_operations"
        ).fetchone()[0] == before

    bypass_id = "op_prepared_bypass"
    with repository._transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO external_operations (
                operation_id, job_id, step_id, attempt_id, provider,
                request_hash, operation_idempotency_key, provider_request_id,
                outcome, summary_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, '{}', ?, ?)
            """,
            (
                bypass_id,
                job.job_id,
                attempt.step_id,
                attempt.attempt_id,
                "paid-provider",
                request_hash,
                ExternalOutcome.PREPARED.value,
                "2026-01-01T00:00:00.000Z",
                "2026-01-01T00:00:00.000Z",
            ),
        )
    with pytest.raises(DomainError, match="external_outcome_unknown"):
        guard.start(bypass_id)

    new_job = repository.create_job(
        request_hash=sha256_digest(b"new job"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(new_job.job_id, JobState.RUNNING)
    new_attempt = repository.create_attempt(new_job.job_id, attempt.step_id)
    new_attempt = repository.start_attempt(new_attempt.attempt_id, authority)
    allowed = guard.prepare(
        job_id=new_job.job_id,
        step_id=new_attempt.step_id,
        attempt_id=new_attempt.attempt_id,
        provider="paid-provider",
        request_hash=request_hash,
        summary_json="{}",
    )
    assert allowed.outcome is ExternalOutcome.PREPARED


def test_reconcile_requires_current_authority_and_only_marks_older_tokens_unknown(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, ExternalOutcome, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, old_attempt, old_authority = _running_attempt(repository)
    old_guard = ExternalOperationGuard(repository, old_authority)
    old_operation = old_guard.prepare(
        job_id=job.job_id,
        step_id=old_attempt.step_id,
        attempt_id=old_attempt.attempt_id,
        provider="provider",
        request_hash=sha256_digest(b"old"),
        summary_json="{}",
    )
    old_guard.start(old_operation.operation_id)

    clock.advance(31_000)
    authority = repository.acquire_scheduler_lease(
        "workspace-a:process-b", ttl_seconds=30
    )
    current_attempt = repository.create_attempt(job.job_id, "current-step")
    current_attempt = repository.start_attempt(current_attempt.attempt_id, authority)
    current_guard = ExternalOperationGuard(repository, authority)
    current_operation = current_guard.prepare(
        job_id=job.job_id,
        step_id=current_attempt.step_id,
        attempt_id=current_attempt.attempt_id,
        provider="provider",
        request_hash=sha256_digest(b"current"),
        summary_json="{}",
    )
    current_guard.start(current_operation.operation_id)

    with pytest.raises(DomainError, match="attempt_fenced"):
        old_guard.reconcile_after_process_loss(job.job_id)
    assert old_guard.get(old_operation.operation_id).outcome is ExternalOutcome.STARTED
    assert current_guard.get(current_operation.operation_id).outcome is ExternalOutcome.STARTED

    reconciled = current_guard.reconcile_after_process_loss(job.job_id)
    assert tuple(operation.operation_id for operation in reconciled) == (
        old_operation.operation_id,
    )
    assert current_guard.get(old_operation.operation_id).outcome is ExternalOutcome.UNKNOWN
    assert current_guard.get(current_operation.operation_id).outcome is ExternalOutcome.STARTED


def test_external_operation_start_checks_cancellation_and_fencing(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, _, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, authority = _running_attempt(repository)
    guard = ExternalOperationGuard(repository, authority)
    prepared = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"paid request"),
        summary_json="{}",
    )
    repository.cancel_job(job.job_id)

    with pytest.raises(DomainError, match="job_cancelled"):
        guard.start(prepared.operation_id)


def test_external_operation_succeeded_and_failed_outcomes_are_terminal(
    tmp_path: Path,
) -> None:
    _, ExternalOperationGuard, ExternalOutcome, _ = _task7_api()
    clock = _Clock()
    repository = _repository(tmp_path, clock)
    job, attempt, authority = _running_attempt(repository)
    guard = ExternalOperationGuard(repository, authority)

    succeeded = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"success"),
        summary_json="{}",
    )
    guard.start(succeeded.operation_id)
    succeeded = guard.succeed(
        succeeded.operation_id,
        provider_request_id="provider-request-1",
        summary_json='{"status":"ok"}',
    )
    assert succeeded.outcome is ExternalOutcome.SUCCEEDED

    failed = guard.prepare(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="paid-provider",
        request_hash=sha256_digest(b"failure"),
        summary_json="{}",
    )
    guard.start(failed.operation_id)
    failed = guard.fail(failed.operation_id, summary_json='{"status":"failed"}')
    assert failed.outcome is ExternalOutcome.FAILED
    assert guard.reconcile_after_process_loss(job.job_id) == ()
