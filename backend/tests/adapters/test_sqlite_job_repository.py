from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.video import JobState
from app.core.errors import DomainError
from app.core.jobs.model import AttemptState
from app.core.jobs.state_machine import (
    LEGAL_ATTEMPT_TRANSITIONS,
    transition_attempt,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
EXPECTED_TABLES = {
    "attempts",
    "challenges",
    "checkpoints",
    "events",
    "external_operations",
    "jobs",
    "leases",
    "source_identities",
    "steps",
}


@pytest.fixture
def repo(tmp_path: Path) -> SqliteJobRepository:
    yield SqliteJobRepository.open(tmp_path / "machine-root")


def test_open_creates_version_one_database_with_exact_schema_and_pragmas(
    repo: SqliteJobRepository,
) -> None:
    assert repo.database_path == repo.machine_root / "jobs.sqlite"
    assert repo.database_path.is_file()

    with repo._connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert tables == EXPECTED_TABLES
    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert busy_timeout == 5_000
    assert user_version == 1
    assert foreign_key_violations == []


def test_json_columns_are_utf8_text_and_schema_has_no_secret_columns(
    repo: SqliteJobRepository,
) -> None:
    with repo._connect() as connection:
        columns = {
            table: connection.execute(f"PRAGMA table_info({table})").fetchall()
            for table in EXPECTED_TABLES
        }

    json_columns = [
        column
        for table_columns in columns.values()
        for column in table_columns
        if column[1].endswith("_json")
    ]
    all_column_names = {
        column[1]
        for table_columns in columns.values()
        for column in table_columns
    }

    assert json_columns
    assert {column[2] for column in json_columns} == {"TEXT"}
    assert not any(
        forbidden in column_name.casefold()
        for column_name in all_column_names
        for forbidden in ("secret", "api_key", "cookie", "authorization")
    )


def test_idempotency_replay_returns_existing_job(repo: SqliteJobRepository) -> None:
    first = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="req-1",
    )
    replay = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="req-1",
    )

    assert replay == first
    assert repo.get_job(first.job_id) == first


def test_idempotency_key_cannot_bind_different_request(
    repo: SqliteJobRepository,
) -> None:
    repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="req-1",
    )

    with pytest.raises(DomainError, match="idempotency_conflict"):
        repo.create_job(
            request_hash=HASH_B,
            principal="agent",
            client_request_id="req-1",
        )


def test_null_client_request_id_does_not_deduplicate(repo: SqliteJobRepository) -> None:
    first = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id=None,
    )
    second = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id=None,
    )

    assert first.job_id != second.job_id


def test_same_client_request_id_is_scoped_by_principal(
    repo: SqliteJobRepository,
) -> None:
    first = repo.create_job(
        request_hash=HASH_A,
        principal="agent-a",
        client_request_id="req-1",
    )
    second = repo.create_job(
        request_hash=HASH_B,
        principal="agent-b",
        client_request_id="req-1",
    )

    assert first.job_id != second.job_id


LEGAL_JOB_TRANSITIONS = (
    (JobState.QUEUED, JobState.RUNNING),
    (JobState.QUEUED, JobState.CANCELLED),
    (JobState.RUNNING, JobState.WAITING_FOR_INPUT),
    (JobState.RUNNING, JobState.SUCCEEDED),
    (JobState.RUNNING, JobState.FAILED),
    (JobState.RUNNING, JobState.CANCELLED),
    (JobState.WAITING_FOR_INPUT, JobState.QUEUED),
    (JobState.WAITING_FOR_INPUT, JobState.FAILED),
    (JobState.WAITING_FOR_INPUT, JobState.CANCELLED),
)


@pytest.mark.parametrize(("start", "end"), LEGAL_JOB_TRANSITIONS)
def test_job_state_machine_accepts_only_frozen_edges(
    repo: SqliteJobRepository,
    start: JobState,
    end: JobState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    if start is JobState.RUNNING:
        repo.transition_job(job.job_id, JobState.RUNNING)
    elif start is JobState.WAITING_FOR_INPUT:
        repo.transition_job(job.job_id, JobState.RUNNING)
        repo.transition_job(job.job_id, JobState.WAITING_FOR_INPUT)

    assert repo.transition_job(job.job_id, end).state is end


@pytest.mark.parametrize(
    "end",
    (JobState.WAITING_FOR_INPUT, JobState.FAILED, JobState.SUCCEEDED),
)
def test_queued_job_rejects_edges_not_in_frozen_graph(
    repo: SqliteJobRepository,
    end: JobState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )

    with pytest.raises(DomainError, match="job_transition_invalid"):
        repo.transition_job(job.job_id, end)


def test_terminal_job_cannot_return_to_running(repo: SqliteJobRepository) -> None:
    job = repo.create_job(
        request_hash="sha256:" + "0" * 64,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    repo.transition_job(job.job_id, JobState.FAILED)

    with pytest.raises(DomainError, match="job_terminal"):
        repo.transition_job(job.job_id, JobState.RUNNING)


@pytest.mark.parametrize(("start", "end"), LEGAL_ATTEMPT_TRANSITIONS)
def test_attempt_state_machine_accepts_only_legal_edges(
    start: AttemptState,
    end: AttemptState,
) -> None:
    assert transition_attempt(start, end) is end


def test_terminal_attempt_cannot_return_to_running(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    attempt = repo.create_attempt(job.job_id, "resolve")
    repo.transition_attempt(attempt.attempt_id, AttemptState.RUNNING)
    repo.transition_attempt(attempt.attempt_id, AttemptState.FAILED)

    with pytest.raises(DomainError, match="attempt_terminal"):
        repo.transition_attempt(attempt.attempt_id, AttemptState.RUNNING)


@pytest.mark.parametrize("attempt_state", (AttemptState.PENDING, AttemptState.RUNNING))
def test_job_cannot_be_terminal_while_an_attempt_is_unsettled(
    repo: SqliteJobRepository,
    attempt_state: AttemptState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    attempt = repo.create_attempt(job.job_id, "resolve")
    if attempt_state is AttemptState.RUNNING:
        repo.transition_attempt(attempt.attempt_id, AttemptState.RUNNING)

    with pytest.raises(DomainError, match="attempt_not_settled"):
        repo.transition_job(job.job_id, JobState.FAILED)


def test_job_can_be_terminal_after_all_attempts_are_settled(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    attempt = repo.create_attempt(job.job_id, "resolve")
    repo.transition_attempt(attempt.attempt_id, AttemptState.RUNNING)
    repo.transition_attempt(attempt.attempt_id, AttemptState.SUCCEEDED)

    assert repo.transition_job(job.job_id, JobState.SUCCEEDED).state is JobState.SUCCEEDED


def test_commit_guard_rejects_non_running_job(repo: SqliteJobRepository) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )

    with pytest.raises(DomainError, match="commit_guard_not_running"):
        with repo.commit_guard(job.job_id):
            pass


def test_commit_guard_rolls_back_when_guarded_work_fails(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)

    with pytest.raises(RuntimeError, match="portable commit failed"):
        with repo.commit_guard(job.job_id) as transaction:
            assert isinstance(transaction, sqlite3.Connection)
            transaction.execute(
                "UPDATE jobs SET state = ? WHERE job_id = ?",
                (JobState.FAILED.value, job.job_id),
            )
            raise RuntimeError("portable commit failed")

    assert repo.get_job(job.job_id).state is JobState.RUNNING
