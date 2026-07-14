from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import AttemptState
from app.core.jobs.state_machine import (
    LEGAL_ATTEMPT_TRANSITIONS,
    LEGAL_JOB_TRANSITIONS,
    TERMINAL_ATTEMPT_STATES,
    TERMINAL_JOB_STATES,
    transition_attempt,
    transition_job,
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
OLD_EXECUTION_CHAIN_TABLES = (
    """
    CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        state TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(job_id, step_id) REFERENCES steps(job_id, step_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE challenges (
        challenge_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        attempt_id TEXT REFERENCES attempts(attempt_id),
        state TEXT NOT NULL,
        prompt_json TEXT NOT NULL,
        response_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE external_operations (
        operation_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        provider TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        operation_idempotency_key TEXT,
        provider_request_id TEXT,
        outcome TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        relative_path TEXT NOT NULL,
        schema_id TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        output_hash TEXT NOT NULL,
        byte_length INTEGER NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)
EXTRA_SCHEMA_OBJECTS = (
    (
        "index",
        "unexpected_index",
        "CREATE INDEX unexpected_index ON jobs(state)",
    ),
    (
        "index",
        "unexpected_unique_index",
        "CREATE UNIQUE INDEX unexpected_unique_index ON jobs(job_id)",
    ),
    (
        "trigger",
        "unexpected_trigger",
        """
        CREATE TRIGGER unexpected_trigger AFTER UPDATE ON jobs
        BEGIN
            SELECT 1;
        END
        """,
    ),
    (
        "view",
        "unexpected_view",
        "CREATE VIEW unexpected_view AS SELECT job_id, state FROM jobs",
    ),
)


@pytest.fixture
def repo(tmp_path: Path) -> SqliteJobRepository:
    yield SqliteJobRepository.open(tmp_path / "machine-root")


def _database_path(tmp_path: Path, name: str) -> tuple[Path, Path]:
    machine_root = tmp_path / name
    machine_root.mkdir()
    return machine_root, machine_root / "jobs.sqlite"


def _assert_schema_invalid(machine_root: Path) -> None:
    with pytest.raises(DomainError, match="job_store_schema_invalid") as caught:
        SqliteJobRepository.open(machine_root)
    assert caught.value.code == "job_store_schema_invalid"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_current_schema_reopens_and_preserves_writer_data(tmp_path: Path) -> None:
    machine_root = tmp_path / "machine-root"
    writer = SqliteJobRepository.open(machine_root)
    job = writer.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id="reopen-current",
    )

    reopened = SqliteJobRepository.open(machine_root)

    assert reopened.get_job(job.job_id) == job


def test_version_one_empty_shell_is_rejected(tmp_path: Path) -> None:
    machine_root, database_path = _database_path(tmp_path, "empty-shell")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 1")

    _assert_schema_invalid(machine_root)


def test_version_one_missing_table_is_rejected(tmp_path: Path) -> None:
    machine_root = tmp_path / "missing-table"
    SqliteJobRepository.open(machine_root)
    with sqlite3.connect(machine_root / "jobs.sqlite") as connection:
        connection.execute("DROP TABLE source_identities")

    _assert_schema_invalid(machine_root)


def test_version_one_old_execution_chain_shape_is_rejected(tmp_path: Path) -> None:
    machine_root = tmp_path / "old-head"
    SqliteJobRepository.open(machine_root)
    with sqlite3.connect(machine_root / "jobs.sqlite") as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in (
            "challenges",
            "external_operations",
            "checkpoints",
            "attempts",
        ):
            connection.execute(f"DROP TABLE {table}")
        for statement in OLD_EXECUTION_CHAIN_TABLES:
            connection.execute(statement)

    _assert_schema_invalid(machine_root)


def test_version_one_extra_application_table_is_rejected(tmp_path: Path) -> None:
    machine_root = tmp_path / "extra-table"
    SqliteJobRepository.open(machine_root)
    with sqlite3.connect(machine_root / "jobs.sqlite") as connection:
        connection.execute("CREATE TABLE unexpected (value TEXT)")

    _assert_schema_invalid(machine_root)


@pytest.mark.parametrize(
    ("object_type", "object_name", "statement"),
    EXTRA_SCHEMA_OBJECTS,
)
def test_version_one_extra_schema_object_is_rejected_without_mutation(
    tmp_path: Path,
    object_type: str,
    object_name: str,
    statement: str,
) -> None:
    machine_root = tmp_path / object_name
    writer = SqliteJobRepository.open(machine_root)
    job = writer.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=object_name,
    )
    with sqlite3.connect(writer.database_path) as connection:
        before = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        connection.execute(statement)

    _assert_schema_invalid(machine_root)

    with sqlite3.connect(writer.database_path) as connection:
        schema_object = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, object_name),
        ).fetchone()
        after = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
    assert schema_object is not None
    assert after == before


def test_version_zero_with_application_table_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    machine_root, database_path = _database_path(tmp_path, "partial-v0")
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unexpected (value TEXT)")

    _assert_schema_invalid(machine_root)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == 0
    assert tables == {"unexpected"}


def test_corrupt_database_is_rejected_without_overwrite(tmp_path: Path) -> None:
    machine_root, database_path = _database_path(tmp_path, "corrupt")
    corrupt_bytes = b"not-a-sqlite-database\x00keep-these-bytes"
    database_path.write_bytes(corrupt_bytes)

    _assert_schema_invalid(machine_root)

    assert database_path.read_bytes() == corrupt_bytes


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


def _insert_challenge(
    connection: sqlite3.Connection,
    *,
    challenge_id: str,
    job_id: str,
    attempt_id: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO challenges (
            challenge_id, job_id, attempt_id, state, prompt_json,
            response_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'pending', '{}', NULL, 'created', 'updated')
        """,
        (challenge_id, job_id, attempt_id),
    )


def _insert_execution_record(
    connection: sqlite3.Connection,
    *,
    table: str,
    record_id: str,
    job_id: str,
    step_id: str,
    attempt_id: str,
) -> None:
    if table == "external_operations":
        connection.execute(
            """
            INSERT INTO external_operations (
                operation_id, job_id, step_id, attempt_id, provider,
                request_hash, operation_idempotency_key, provider_request_id,
                outcome, summary_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'provider', ?, NULL, NULL, 'started', '{}',
                      'created', 'updated')
            """,
            (record_id, job_id, step_id, attempt_id, HASH_A),
        )
        return
    if table == "checkpoints":
        connection.execute(
            """
            INSERT INTO checkpoints (
                checkpoint_id, job_id, step_id, attempt_id, relative_path,
                schema_id, input_hash, output_hash, byte_length, metadata_json,
                created_at
            ) VALUES (?, ?, ?, ?, 'checkpoint.bin', 'schema.v1', ?, ?, 1, '{}',
                      'created')
            """,
            (record_id, job_id, step_id, attempt_id, HASH_A, HASH_B),
        )
        return
    raise AssertionError(f"unsupported test table: {table}")


def test_execution_chain_foreign_keys_accept_valid_writer_shapes(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    attempt = repo.create_attempt(job.job_id, "resolve")

    with repo._transaction(immediate=True) as connection:
        _insert_challenge(
            connection,
            challenge_id="challenge-with-attempt",
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
        )
        _insert_challenge(
            connection,
            challenge_id="challenge-without-attempt",
            job_id=job.job_id,
            attempt_id=None,
        )
        _insert_execution_record(
            connection,
            table="external_operations",
            record_id="operation-valid",
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
        )
        _insert_execution_record(
            connection,
            table="checkpoints",
            record_id="checkpoint-valid",
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
        )


def test_challenge_foreign_key_rejects_cross_job_attempt(
    repo: SqliteJobRepository,
) -> None:
    first_job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    second_job = repo.create_job(
        request_hash=HASH_B,
        principal="local",
        client_request_id=None,
    )
    attempt = repo.create_attempt(first_job.job_id, "resolve")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with repo._transaction(immediate=True) as connection:
            _insert_challenge(
                connection,
                challenge_id="challenge-cross-job",
                job_id=second_job.job_id,
                attempt_id=attempt.attempt_id,
            )


@pytest.mark.parametrize("table", ("external_operations", "checkpoints"))
@pytest.mark.parametrize("invalid_shape", ("cross_job", "cross_step", "missing_step"))
def test_execution_record_foreign_keys_reject_invalid_ownership_shapes(
    repo: SqliteJobRepository,
    table: str,
    invalid_shape: str,
) -> None:
    first_job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    second_job = repo.create_job(
        request_hash=HASH_B,
        principal="local",
        client_request_id=None,
    )
    attempt = repo.create_attempt(first_job.job_id, "resolve")
    repo.create_attempt(first_job.job_id, "draft")
    invalid_values = {
        "cross_job": (second_job.job_id, attempt.step_id),
        "cross_step": (first_job.job_id, "draft"),
        "missing_step": (first_job.job_id, "missing"),
    }
    invalid_job_id, invalid_step_id = invalid_values[invalid_shape]

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with repo._transaction(immediate=True) as connection:
            _insert_execution_record(
                connection,
                table=table,
                record_id=f"{table}-{invalid_shape}",
                job_id=invalid_job_id,
                step_id=invalid_step_id,
                attempt_id=attempt.attempt_id,
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


@pytest.mark.parametrize(
    "terminal_state",
    (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED),
)
def test_create_attempt_rejects_terminal_job_without_inserting_rows(
    repo: SqliteJobRepository,
    terminal_state: JobState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    if terminal_state is JobState.CANCELLED:
        repo.transition_job(job.job_id, terminal_state)
    else:
        repo.transition_job(job.job_id, JobState.RUNNING)
        repo.transition_job(job.job_id, terminal_state)

    with pytest.raises(DomainError, match="job_terminal"):
        repo.create_attempt(job.job_id, "must-not-exist")

    with repo._connect() as connection:
        step_count = connection.execute(
            "SELECT COUNT(*) FROM steps WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
    assert step_count == 0
    assert attempt_count == 0


@pytest.mark.parametrize(
    "nonterminal_state",
    (JobState.QUEUED, JobState.RUNNING, JobState.WAITING_FOR_INPUT),
)
def test_create_attempt_allows_every_nonterminal_job_state(
    repo: SqliteJobRepository,
    nonterminal_state: JobState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    if nonterminal_state is not JobState.QUEUED:
        repo.transition_job(job.job_id, JobState.RUNNING)
    if nonterminal_state is JobState.WAITING_FOR_INPUT:
        repo.transition_job(job.job_id, JobState.WAITING_FOR_INPUT)

    attempt = repo.create_attempt(job.job_id, "allowed")

    assert attempt.job_id == job.job_id
    assert attempt.state is AttemptState.PENDING


@pytest.mark.parametrize(
    ("start", "end"),
    tuple((start, end) for start in JobState for end in JobState),
)
def test_job_state_machine_complete_transition_matrix(
    start: JobState,
    end: JobState,
) -> None:
    if (start, end) in LEGAL_JOB_TRANSITIONS:
        assert transition_job(start, end) is end
    elif start in TERMINAL_JOB_STATES:
        with pytest.raises(DomainError, match="job_terminal"):
            transition_job(start, end)
    else:
        with pytest.raises(DomainError, match="job_transition_invalid"):
            transition_job(start, end)


@pytest.mark.parametrize(
    ("start", "end"),
    tuple((start, end) for start in AttemptState for end in AttemptState),
)
def test_attempt_state_machine_complete_transition_matrix(
    start: AttemptState,
    end: AttemptState,
) -> None:
    if (start, end) in LEGAL_ATTEMPT_TRANSITIONS:
        assert transition_attempt(start, end) is end
    elif start in TERMINAL_ATTEMPT_STATES:
        with pytest.raises(DomainError, match="attempt_terminal"):
            transition_attempt(start, end)
    else:
        with pytest.raises(DomainError, match="attempt_transition_invalid"):
            transition_attempt(start, end)


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
