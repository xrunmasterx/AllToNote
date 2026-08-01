from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application.checkpoint_runner import CheckpointedStepRunner
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import ExternalOperationGuard
from app.core.jobs.model import AttemptState


_CHECKPOINT_SCHEMA = "test-step.v1"
_INPUT_HASH = sha256_digest(b"input")


class _Clock:
    def __init__(self) -> None:
        self.now_ms = 1_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


def _fixture(tmp_path: Path):
    clock = _Clock()
    repository = SqliteJobRepository.open(tmp_path / "machine", clock=clock)
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.claim_job(
        job.job_id,
        "process-a",
        ttl_seconds=30,
    ).authority
    storage = FileAttemptStorage(
        tmp_path / "attempts",
        repository,
        validators={_CHECKPOINT_SCHEMA: lambda payload: bool(payload)},
    )
    runner = CheckpointedStepRunner(
        repository,
        storage,
        checkpoint_reader=lambda metadata: (
            storage.root / metadata.relative_path
        ).read_bytes(),
        checkpoint_schema=_CHECKPOINT_SCHEMA,
        scheduler_lease_ttl_seconds=30,
        heartbeat_interval_seconds=60.0,
    )
    return clock, repository, job, authority, runner


def _attempt_state(repository, attempt_id: str) -> AttemptState:
    with repository._connect() as connection:
        row = connection.execute(
            "SELECT state FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    assert row is not None
    return AttemptState(row["state"])


def _stage_states(
    repository: SqliteJobRepository,
    job_id: str,
    stage: str,
) -> list[str]:
    return [
        payload["state"]
        for event in repository.list_events(job_id)
        if event.event_type == "stage.changed.v1"
        and (payload := json.loads(event.payload_json))["stage"] == stage
    ]


def test_valid_checkpoint_is_reused_and_resumed_attempt_converges(
    tmp_path: Path,
) -> None:
    _, repository, job, authority, runner = _fixture(tmp_path)
    calls = 0

    def action(_execution):
        nonlocal calls
        calls += 1
        return "durable result"

    first = runner.run(
        job.job_id,
        "knowledge-map",
        _INPUT_HASH,
        authority,
        action,
        encode=lambda value: value.encode("utf-8"),
        decode=lambda payload: payload.decode("utf-8"),
    )
    resumed = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            "knowledge-map",
            authority=authority,
        ).attempt_id,
        authority,
    )
    second = runner.run(
        job.job_id,
        "knowledge-map",
        _INPUT_HASH,
        authority,
        action,
        encode=lambda value: value.encode("utf-8"),
        decode=lambda payload: payload.decode("utf-8"),
        resumed_attempt=resumed,
    )

    assert first == second == "durable result"
    assert calls == 1
    assert _attempt_state(repository, resumed.attempt_id) is AttemptState.SUCCEEDED
    assert _stage_states(repository, job.job_id, "knowledge-map") == [
        "pending",
        "running",
        "succeeded",
        "pending",
        "running",
        "succeeded",
    ]


def test_fenced_runner_cannot_publish_checkpoint(tmp_path: Path) -> None:
    clock, repository, job, authority, runner = _fixture(tmp_path)

    def fence_before_publish(_execution):
        clock.advance(31_000)
        repository.claim_job(job.job_id, "process-b", ttl_seconds=30)
        return "late result"

    with pytest.raises(DomainError, match="job_claim_fenced"):
        runner.run(
            job.job_id,
            "knowledge-map",
            _INPUT_HASH,
            authority,
            fence_before_publish,
            encode=lambda value: value.encode("utf-8"),
            decode=lambda payload: payload.decode("utf-8"),
        )

    assert repository.latest_checkpoint(job.job_id, "knowledge-map") is None


def test_external_outcome_unknown_pauses_job_atomically(tmp_path: Path) -> None:
    _, repository, job, authority, runner = _fixture(tmp_path)
    attempt_ids: list[str] = []

    def lose_external_outcome(execution):
        attempt_ids.append(execution.attempt_id)
        guard = ExternalOperationGuard(repository, execution.authority)
        operation = guard.prepare(
            job_id=execution.job_id,
            step_id=execution.step_id,
            attempt_id=execution.attempt_id,
            provider="paid-provider",
            request_hash=sha256_digest(b"paid request"),
            summary_json="{}",
        )
        guard.start(operation.operation_id)
        guard.unknown(operation.operation_id, summary_json="{}")
        raise DomainError(
            "external_outcome_unknown",
            ErrorCategory.RETRYABLE_RUNTIME,
            "Provider outcome is unknown",
        )

    with pytest.raises(DomainError, match="external_outcome_unknown"):
        runner.run(
            job.job_id,
            "knowledge-map",
            _INPUT_HASH,
            authority,
            lose_external_outcome,
        )

    paused_job, attempt, challenge = repository.get_job_details(job.job_id)
    assert paused_job.state is JobState.WAITING_FOR_INPUT
    assert attempt is None
    assert _attempt_state(repository, attempt_ids[0]) is AttemptState.NEEDS_INPUT
    assert challenge is not None
    assert repository.latest_checkpoint(job.job_id, "knowledge-map") is None
    assert _stage_states(repository, job.job_id, "knowledge-map") == [
        "pending",
        "running",
        "needs_input",
    ]
    assert repository.list_events(job.job_id)[-1].payload_json == (
        '{"state":"waiting_for_input"}'
    )


def test_cancelled_action_settles_attempt_as_cancelled(tmp_path: Path) -> None:
    _, repository, job, authority, runner = _fixture(tmp_path)
    attempt_ids: list[str] = []

    def cancel(execution):
        attempt_ids.append(execution.attempt_id)
        repository.cancel_job(job.job_id)
        raise DomainError(
            "job_cancelled",
            ErrorCategory.CANCELLED,
            "Job cancellation was requested",
        )

    with pytest.raises(DomainError, match="job_cancelled"):
        runner.run(
            job.job_id,
            "parse-document",
            _INPUT_HASH,
            authority,
            cancel,
        )

    assert _attempt_state(repository, attempt_ids[0]) is AttemptState.CANCELLED
    assert repository.get_job_details(job.job_id)[0].state is JobState.CANCELLED
    assert repository.latest_checkpoint(job.job_id, "parse-document") is None
    assert _stage_states(repository, job.job_id, "parse-document") == [
        "pending",
        "running",
        "cancelled",
    ]
    assert repository.list_events(job.job_id)[-1].payload_json == (
        '{"state":"cancelled"}'
    )
