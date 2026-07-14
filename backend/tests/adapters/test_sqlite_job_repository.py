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


def _create_needs_input_attempt(repo: SqliteJobRepository):
    job = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="challenge-original",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    attempt = repo.create_attempt(job.job_id, "acquire")
    repo.transition_attempt(attempt.attempt_id, AttemptState.RUNNING)
    attempt = repo.transition_attempt(attempt.attempt_id, AttemptState.NEEDS_INPUT)
    return job, attempt


def _record_unknown_operation(
    repo: SqliteJobRepository,
    *,
    job_id: str,
    step_id: str,
    attempt_id: str,
    operation_id: str,
) -> None:
    with repo._transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO external_operations (
                operation_id, job_id, step_id, attempt_id, provider,
                request_hash, operation_idempotency_key, provider_request_id,
                outcome, summary_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'provider', ?, NULL, NULL,
                      'external_outcome_unknown', '{}', 'created', 'updated')
            """,
            (operation_id, job_id, step_id, attempt_id, HASH_A),
        )


def _create_failed_job_with_unknown_operations(
    repo: SqliteJobRepository,
    operation_ids: tuple[str, ...] = ("op_unknown_a", "op_unknown_b"),
):
    job = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="original-request",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    attempt = repo.create_attempt(job.job_id, "model")
    repo.transition_attempt(attempt.attempt_id, AttemptState.RUNNING)
    for operation_id in operation_ids:
        _record_unknown_operation(
            repo,
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            operation_id=operation_id,
        )
    repo.transition_attempt(attempt.attempt_id, AttemptState.FAILED)
    return repo.transition_job(job.job_id, JobState.FAILED), attempt


def test_schema_v1_persists_challenge_response_hash_and_attempt_binding(
    repo: SqliteJobRepository,
) -> None:
    with repo._connect() as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(challenges)")
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(challenges)"
        ).fetchall()

    assert {"response_hash", "response_attempt_id"} <= columns
    assert any(row[2] == "attempts" and row[3] == "response_attempt_id" for row in foreign_keys)


def test_create_job_persists_retry_lineage_and_replays_only_same_binding(
    repo: SqliteJobRepository,
) -> None:
    original = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="original",
    )

    retry = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="retry-1",
        retry_of_job_id=original.job_id,
    )
    replay = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="retry-1",
        retry_of_job_id=original.job_id,
    )

    assert retry.retry_of_job_id == original.job_id
    assert replay == retry
    with pytest.raises(DomainError, match="idempotency_conflict"):
        repo.create_job(
            request_hash=HASH_A,
            principal="agent",
            client_request_id="retry-1",
        )


@pytest.mark.parametrize(
    "terminal_state",
    (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED),
)
def test_cancel_is_stable_for_terminal_jobs(
    repo: SqliteJobRepository,
    terminal_state: JobState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id=None,
    )
    if terminal_state is JobState.CANCELLED:
        terminal = repo.transition_job(job.job_id, terminal_state)
    else:
        repo.transition_job(job.job_id, JobState.RUNNING)
        terminal = repo.transition_job(job.job_id, terminal_state)

    assert repo.cancel_job(job.job_id) == terminal
    assert repo.cancel_job(job.job_id) == terminal


@pytest.mark.parametrize(
    "state",
    (JobState.QUEUED, JobState.RUNNING, JobState.WAITING_FOR_INPUT),
)
def test_cancel_transitions_settled_nonterminal_job(
    repo: SqliteJobRepository,
    state: JobState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id=None,
    )
    if state is not JobState.QUEUED:
        repo.transition_job(job.job_id, JobState.RUNNING)
    if state is JobState.WAITING_FOR_INPUT:
        repo.transition_job(job.job_id, JobState.WAITING_FOR_INPUT)

    assert repo.cancel_job(job.job_id).state is JobState.CANCELLED


def test_cancel_rejects_unsettled_attempt_without_partial_write(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    repo.create_attempt(job.job_id, "running-step")

    with pytest.raises(DomainError, match="attempt_not_settled"):
        repo.cancel_job(job.job_id)

    assert repo.get_job(job.job_id).state is JobState.RUNNING


def test_create_challenge_atomically_waits_for_input(
    repo: SqliteJobRepository,
) -> None:
    job, attempt = _create_needs_input_attempt(repo)

    challenge = repo.create_challenge(
        job.job_id,
        attempt.attempt_id,
        '{"kind":"credential_profile"}',
    )

    assert challenge.job_id == job.job_id
    assert challenge.attempt_id == attempt.attempt_id
    assert challenge.state == "pending"
    assert repo.get_job(job.job_id).state is JobState.WAITING_FOR_INPUT


def test_respond_challenge_is_hash_idempotent_across_process_reopen(
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "machine-root"
    repo = SqliteJobRepository.open(machine_root)
    job, attempt = _create_needs_input_attempt(repo)
    challenge = repo.create_challenge(job.job_id, attempt.attempt_id, "{}")

    first_job, first_attempt = repo.respond_challenge_atomic(
        job.job_id,
        challenge.challenge_id,
        response_hash=HASH_A,
        response_json='{"credential_profile":"配置"}',
    )
    reopened = SqliteJobRepository.open(machine_root)
    replay_job, replay_attempt = reopened.respond_challenge_atomic(
        job.job_id,
        challenge.challenge_id,
        response_hash=HASH_A,
        response_json='{"credential_profile":"配置"}',
    )

    assert first_job.state is JobState.QUEUED
    assert first_attempt.step_id == attempt.step_id
    assert first_attempt.state is AttemptState.PENDING
    assert replay_job == first_job
    assert replay_attempt == first_attempt
    with reopened._connect() as connection:
        row = connection.execute(
            "SELECT * FROM challenges WHERE challenge_id = ?",
            (challenge.challenge_id,),
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
    assert row["state"] == "consumed"
    assert row["response_hash"] == HASH_A
    assert row["response_attempt_id"] == first_attempt.attempt_id
    assert attempt_count == 2


def test_respond_challenge_rejects_different_hash_without_mutation(
    repo: SqliteJobRepository,
) -> None:
    job, attempt = _create_needs_input_attempt(repo)
    challenge = repo.create_challenge(job.job_id, attempt.attempt_id, "{}")
    first_job, first_attempt = repo.respond_challenge_atomic(
        job.job_id,
        challenge.challenge_id,
        response_hash=HASH_A,
        response_json="{}",
    )

    with pytest.raises(DomainError, match="challenge_response_conflict"):
        repo.respond_challenge_atomic(
            job.job_id,
            challenge.challenge_id,
            response_hash=HASH_B,
            response_json="{}",
        )

    assert repo.get_job(job.job_id) == first_job
    with repo._connect() as connection:
        row = connection.execute(
            "SELECT response_hash, response_attempt_id FROM challenges WHERE challenge_id = ?",
            (challenge.challenge_id,),
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
    assert tuple(row) == (HASH_A, first_attempt.attempt_id)
    assert attempt_count == 2


def test_respond_challenge_rolls_back_when_attempt_insert_fails(
    repo: SqliteJobRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, attempt = _create_needs_input_attempt(repo)
    challenge = repo.create_challenge(job.job_id, attempt.attempt_id, "{}")
    monkeypatch.setattr(
        "app.adapters.jobs.sqlite_repository.new_typed_id",
        lambda prefix: attempt.attempt_id,
    )

    with pytest.raises(sqlite3.IntegrityError):
        repo.respond_challenge_atomic(
            job.job_id,
            challenge.challenge_id,
            response_hash=HASH_A,
            response_json="{}",
        )

    assert repo.get_job(job.job_id).state is JobState.WAITING_FOR_INPUT
    with repo._connect() as connection:
        row = connection.execute(
            """
            SELECT state, response_hash, response_json, response_attempt_id
            FROM challenges WHERE challenge_id = ?
            """,
            (challenge.challenge_id,),
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
    assert tuple(row) == ("pending", None, None, None)
    assert attempt_count == 1


def test_retry_atomically_creates_new_job_and_replays_after_reopen(
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "machine-root"
    repo = SqliteJobRepository.open(machine_root)
    original, _ = _create_failed_job_with_unknown_operations(repo)

    retry = repo.create_retry_job_atomic(
        original.job_id,
        expected_original_state=JobState.FAILED,
        confirmed_unknown_operation_ids=("op_unknown_a", "op_unknown_b"),
        client_request_id="retry-1",
    )
    reopened = SqliteJobRepository.open(machine_root)
    replay = reopened.create_retry_job_atomic(
        original.job_id,
        expected_original_state=JobState.FAILED,
        confirmed_unknown_operation_ids=("op_unknown_b", "op_unknown_a"),
        client_request_id="retry-1",
    )

    assert retry.job_id != original.job_id
    assert retry.request_hash == original.request_hash
    assert retry.principal == original.principal
    assert retry.retry_of_job_id == original.job_id
    assert replay == retry
    assert reopened.get_job(original.job_id) == original


@pytest.mark.parametrize(
    ("expected_state", "confirmed_ids", "client_request_id", "error_code"),
    (
        (
            JobState.CANCELLED,
            ("op_unknown_a", "op_unknown_b"),
            "retry-state",
            "retry_original_state_conflict",
        ),
        (
            JobState.FAILED,
            ("op_unknown_a",),
            "retry-missing",
            "retry_unknown_operations_unconfirmed",
        ),
        (
            JobState.FAILED,
            ("op_unknown_a", "op_unknown_b", "op_extra"),
            "retry-extra",
            "retry_unknown_operations_unconfirmed",
        ),
        (
            JobState.FAILED,
            ("op_unknown_a", "op_unknown_a", "op_unknown_b"),
            "retry-duplicate",
            "retry_unknown_operations_invalid",
        ),
        (
            JobState.FAILED,
            ("op_unknown_a", "op_unknown_b"),
            "original-request",
            "retry_client_request_id_reused",
        ),
    ),
)
def test_retry_rejects_invalid_preconditions_before_insert(
    repo: SqliteJobRepository,
    expected_state: JobState,
    confirmed_ids: tuple[str, ...],
    client_request_id: str,
    error_code: str,
) -> None:
    original, _ = _create_failed_job_with_unknown_operations(repo)

    with pytest.raises(DomainError, match=error_code):
        repo.create_retry_job_atomic(
            original.job_id,
            expected_original_state=expected_state,
            confirmed_unknown_operation_ids=confirmed_ids,
            client_request_id=client_request_id,
        )

    with repo._connect() as connection:
        retry_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE retry_of_job_id = ?",
            (original.job_id,),
        ).fetchone()[0]
    assert retry_count == 0
    assert repo.get_job(original.job_id) == original


def test_retry_rejects_nonterminal_original_before_insert(
    repo: SqliteJobRepository,
) -> None:
    original = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="original",
    )

    with pytest.raises(DomainError, match="retry_original_not_terminal"):
        repo.create_retry_job_atomic(
            original.job_id,
            expected_original_state=JobState.QUEUED,
            confirmed_unknown_operation_ids=(),
            client_request_id="retry-queued",
        )

    with repo._connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1


def test_retry_key_bound_by_normal_submit_is_a_stable_conflict(
    repo: SqliteJobRepository,
) -> None:
    original, _ = _create_failed_job_with_unknown_operations(repo, ())
    normal = repo.create_job(
        request_hash=original.request_hash,
        principal=original.principal,
        client_request_id="occupied",
    )

    with pytest.raises(DomainError, match="idempotency_conflict"):
        repo.create_retry_job_atomic(
            original.job_id,
            expected_original_state=JobState.FAILED,
            confirmed_unknown_operation_ids=(),
            client_request_id="occupied",
        )

    assert repo.get_job(normal.job_id) == normal
