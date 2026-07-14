from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.domain.ids import new_typed_id, utc_now_millis
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import Attempt, AttemptState, Job
from app.core.jobs.state_machine import (
    TERMINAL_JOB_STATES,
    transition_attempt,
    transition_job,
)


_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5_000
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        principal TEXT NOT NULL,
        client_request_id TEXT,
        state TEXT NOT NULL,
        retry_of_job_id TEXT REFERENCES jobs(job_id),
        result_json TEXT,
        error_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(principal, client_request_id)
    )
    """,
    """
    CREATE TABLE steps (
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        step_name TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        PRIMARY KEY(job_id, step_id)
    )
    """,
    """
    CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        state TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(job_id, attempt_id),
        UNIQUE(job_id, step_id, attempt_id),
        FOREIGN KEY(job_id, step_id) REFERENCES steps(job_id, step_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE events (
        event_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(job_id, sequence)
    )
    """,
    """
    CREATE TABLE challenges (
        challenge_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        attempt_id TEXT,
        state TEXT NOT NULL,
        prompt_json TEXT NOT NULL,
        response_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(job_id, attempt_id) REFERENCES attempts(job_id, attempt_id)
    )
    """,
    """
    CREATE TABLE external_operations (
        operation_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        operation_idempotency_key TEXT,
        provider_request_id TEXT,
        outcome TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(job_id, step_id, attempt_id)
            REFERENCES attempts(job_id, step_id, attempt_id)
    )
    """,
    """
    CREATE TABLE checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        schema_id TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        output_hash TEXT NOT NULL,
        byte_length INTEGER NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(job_id, step_id, attempt_id)
            REFERENCES attempts(job_id, step_id, attempt_id)
    )
    """,
    """
    CREATE TABLE leases (
        lease_name TEXT PRIMARY KEY,
        job_id TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE source_identities (
        connector_id TEXT NOT NULL,
        canonical_identity TEXT NOT NULL,
        source_id TEXT NOT NULL,
        owning_bundle_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(connector_id, canonical_identity)
    )
    """,
)


def _normalize_schema_sql(sql: str) -> str:
    return " ".join(sql.split())


def _application_schema(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
          AND name NOT GLOB 'sqlite_*'
        """
    ).fetchall()
    return {(row[0], row[1]): _normalize_schema_sql(row[2]) for row in rows}


def _expected_schema() -> dict[tuple[str, str], str]:
    with sqlite3.connect(":memory:") as connection:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        return _application_schema(connection)


def _raise_schema_invalid(error: BaseException | None = None) -> None:
    raise DomainError(
        "job_store_schema_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "JobStore schema does not match version 1",
    ) from error


class SqliteJobRepository:
    def __init__(self, machine_root: Path) -> None:
        self.machine_root = machine_root
        self.database_path = machine_root / "jobs.sqlite"

    @classmethod
    def open(cls, machine_root: Path) -> SqliteJobRepository:
        resolved_root = Path(machine_root).resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        repository = cls(resolved_root)
        try:
            repository._initialize_schema()
        except DomainError:
            raise
        except sqlite3.DatabaseError as error:
            _raise_schema_invalid(error)
        return repository

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        with self._transaction(immediate=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise DomainError(
                    "job_store_schema_unsupported",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "JobStore schema version is not supported",
                )
            actual_schema = _application_schema(connection)
            if version == 0:
                if actual_schema:
                    _raise_schema_invalid()
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                actual_schema = _application_schema(connection)
            if actual_schema != _expected_schema():
                _raise_schema_invalid()

    def create_job(
        self,
        *,
        request_hash: str,
        principal: str,
        client_request_id: str | None,
    ) -> Job:
        with self._transaction(immediate=True) as connection:
            if client_request_id is not None:
                existing = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE principal = ? AND client_request_id = ?
                    """,
                    (principal, client_request_id),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise DomainError(
                            "idempotency_conflict",
                            ErrorCategory.CONFLICT,
                            "Idempotency key is already bound to another request",
                        )
                    return self._job_from_row(existing)

            now = utc_now_millis()
            job_id = new_typed_id("job")
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, request_hash, principal, client_request_id, state,
                    retry_of_job_id, result_json, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    job_id,
                    request_hash,
                    principal,
                    client_request_id,
                    JobState.QUEUED.value,
                    None,
                    now,
                    now,
                ),
            )
            return self._get_job(connection, job_id)

    def get_job(self, job_id: str) -> Job:
        with self._transaction(immediate=False) as connection:
            return self._get_job(connection, job_id)

    def create_attempt(self, job_id: str, step_id: str) -> Attempt:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if job.state in TERMINAL_JOB_STATES:
                raise DomainError(
                    "job_terminal",
                    ErrorCategory.CONFLICT,
                    "Terminal Job cannot create an Attempt",
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO steps (job_id, step_id, step_name, ordinal)
                VALUES (?, ?, ?, 0)
                """,
                (job_id, step_id, step_id),
            )
            now = utc_now_millis()
            attempt_id = new_typed_id("att")
            connection.execute(
                """
                INSERT INTO attempts (
                    attempt_id, job_id, step_id, state, fencing_token,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    step_id,
                    AttemptState.PENDING.value,
                    now,
                    now,
                ),
            )
            return self._get_attempt(connection, attempt_id)

    def transition_job(self, job_id: str, state: JobState) -> Job:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            next_state = transition_job(job.state, state)
            if next_state in TERMINAL_JOB_STATES:
                unsettled = connection.execute(
                    """
                    SELECT 1 FROM attempts
                    WHERE job_id = ? AND state IN (?, ?)
                    LIMIT 1
                    """,
                    (
                        job_id,
                        AttemptState.PENDING.value,
                        AttemptState.RUNNING.value,
                    ),
                ).fetchone()
                if unsettled is not None:
                    raise DomainError(
                        "attempt_not_settled",
                        ErrorCategory.CONFLICT,
                        "All Attempts must be terminal before the Job",
                    )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (next_state.value, utc_now_millis(), job_id),
            )
            return self._get_job(connection, job_id)

    def transition_attempt(
        self, attempt_id: str, state: AttemptState
    ) -> Attempt:
        with self._transaction(immediate=True) as connection:
            attempt = self._get_attempt(connection, attempt_id)
            next_state = transition_attempt(attempt.state, state)
            connection.execute(
                "UPDATE attempts SET state = ?, updated_at = ? WHERE attempt_id = ?",
                (next_state.value, utc_now_millis(), attempt_id),
            )
            return self._get_attempt(connection, attempt_id)

    @contextmanager
    def commit_guard(self, job_id: str) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = self._get_job(connection, job_id)
            if job.state is not JobState.RUNNING:
                raise DomainError(
                    "commit_guard_not_running",
                    ErrorCategory.CONFLICT,
                    "Commit guard requires a running Job",
                )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _get_job(self, connection: sqlite3.Connection, job_id: str) -> Job:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "job_not_found",
                ErrorCategory.INVALID_REQUEST,
                "Job does not exist",
            )
        return self._job_from_row(row)

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            request_hash=row["request_hash"],
            principal=row["principal"],
            client_request_id=row["client_request_id"],
            state=JobState(row["state"]),
            retry_of_job_id=row["retry_of_job_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _get_attempt(
        self, connection: sqlite3.Connection, attempt_id: str
    ) -> Attempt:
        row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "attempt_not_found",
                ErrorCategory.INVALID_REQUEST,
                "Attempt does not exist",
            )
        return Attempt(
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            step_id=row["step_id"],
            state=AttemptState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
