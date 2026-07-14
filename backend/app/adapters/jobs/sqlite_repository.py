from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.domain.ids import new_typed_id, utc_now_millis
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import (
    Attempt,
    AttemptState,
    Challenge,
    ChallengeState,
    CheckpointMetadata,
    Job,
    JobEvent,
)
from app.core.jobs.external_operation import ExternalOperation, ExternalOutcome
from app.core.jobs.resource_lease import ExecutionAuthority, validate_lease_ttl
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
        cancellation_requested INTEGER NOT NULL
            CHECK (cancellation_requested IN (0, 1)),
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
        response_hash TEXT,
        response_attempt_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(job_id, attempt_id) REFERENCES attempts(job_id, attempt_id),
        FOREIGN KEY(job_id, response_attempt_id)
            REFERENCES attempts(job_id, attempt_id)
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
        lease_name TEXT PRIMARY KEY CHECK (lease_name = 'scheduler'),
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
    def __init__(
        self, machine_root: Path, *, clock: Callable[[], int]
    ) -> None:
        self.machine_root = machine_root
        self.database_path = machine_root / "jobs.sqlite"
        self._clock = clock

    @classmethod
    def open(
        cls,
        machine_root: Path,
        *,
        clock: Callable[[], int] | None = None,
    ) -> SqliteJobRepository:
        resolved_root = Path(machine_root).resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        repository = cls(
            resolved_root,
            clock=clock or (lambda: time.time_ns() // 1_000_000),
        )
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
        retry_of_job_id: str | None = None,
    ) -> Job:
        with self._transaction(immediate=True) as connection:
            return self._create_job(
                connection,
                request_hash=request_hash,
                principal=principal,
                client_request_id=client_request_id,
                retry_of_job_id=retry_of_job_id,
            )

    def get_job(self, job_id: str) -> Job:
        with self._transaction(immediate=False) as connection:
            return self._get_job(connection, job_id)

    def get_job_details(
        self, job_id: str
    ) -> tuple[Job, Attempt | None, Challenge | None]:
        with self._transaction(immediate=False) as connection:
            job = self._get_job(connection, job_id)
            attempt_row = connection.execute(
                """
                SELECT * FROM attempts
                WHERE job_id = ? AND state IN (?, ?)
                ORDER BY created_at DESC, attempt_id DESC
                LIMIT 1
                """,
                (
                    job_id,
                    AttemptState.PENDING.value,
                    AttemptState.RUNNING.value,
                ),
            ).fetchone()
            challenge_row = connection.execute(
                """
                SELECT * FROM challenges
                WHERE job_id = ? AND state = ?
                ORDER BY created_at DESC, challenge_id DESC
                LIMIT 1
                """,
                (job_id, ChallengeState.PENDING.value),
            ).fetchone()
            attempt = (
                self._attempt_from_row(attempt_row)
                if attempt_row is not None
                else None
            )
            challenge = (
                self._challenge_from_row(challenge_row)
                if challenge_row is not None
                else None
            )
            return job, attempt, challenge

    def create_attempt(self, job_id: str, step_id: str) -> Attempt:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if job.state in TERMINAL_JOB_STATES:
                raise DomainError(
                    "job_terminal",
                    ErrorCategory.CONFLICT,
                    "Terminal Job cannot create an Attempt",
                )
            if job.cancellation_requested:
                raise DomainError(
                    "job_cancelled",
                    ErrorCategory.CANCELLED,
                    "Job cancellation was requested",
                )
            return self._create_attempt(connection, job_id, step_id)

    def acquire_scheduler_lease(
        self, owner_id: str, *, ttl_seconds: int
    ) -> ExecutionAuthority:
        validate_lease_ttl(ttl_seconds)
        self._validate_owner_id(owner_id)
        now_ms = self._clock()
        expires_at = str(now_ms + ttl_seconds * 1_000)
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE lease_name = 'scheduler'"
            ).fetchone()
            if row is None:
                fencing_token = 1
                connection.execute(
                    """
                    INSERT INTO leases (
                        lease_name, job_id, owner, fencing_token,
                        expires_at, heartbeat_at
                    ) VALUES ('scheduler', NULL, ?, ?, ?, ?)
                    """,
                    (owner_id, fencing_token, expires_at, str(now_ms)),
                )
            elif int(row["expires_at"]) > now_ms:
                if row["owner"] != owner_id:
                    raise DomainError(
                        "scheduler_busy",
                        ErrorCategory.CONFLICT,
                        "Workspace scheduler is held by another process instance",
                    )
                fencing_token = row["fencing_token"]
                connection.execute(
                    """
                    UPDATE leases SET expires_at = ?, heartbeat_at = ?
                    WHERE lease_name = 'scheduler'
                    """,
                    (expires_at, str(now_ms)),
                )
            else:
                fencing_token = row["fencing_token"] + 1
                connection.execute(
                    """
                    UPDATE leases
                    SET job_id = NULL, owner = ?, fencing_token = ?,
                        expires_at = ?, heartbeat_at = ?
                    WHERE lease_name = 'scheduler'
                    """,
                    (
                        owner_id,
                        fencing_token,
                        expires_at,
                        str(now_ms),
                    ),
                )
            return ExecutionAuthority(owner_id, fencing_token)

    def heartbeat_scheduler_lease(
        self, authority: ExecutionAuthority, *, ttl_seconds: int
    ) -> ExecutionAuthority:
        validate_lease_ttl(ttl_seconds)
        now_ms = self._clock()
        with self._transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE leases SET expires_at = ?, heartbeat_at = ?
                WHERE lease_name = 'scheduler' AND owner = ?
                  AND fencing_token = ? AND CAST(expires_at AS INTEGER) > ?
                """,
                (
                    str(now_ms + ttl_seconds * 1_000),
                    str(now_ms),
                    authority.owner_id,
                    authority.fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                raise DomainError(
                    "scheduler_lease_lost",
                    ErrorCategory.CONFLICT,
                    "Workspace scheduler lease is expired or fenced",
                )
        return authority

    def release_scheduler_lease(
        self, authority: ExecutionAuthority
    ) -> bool:
        with self._transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE leases SET expires_at = '0'
                WHERE lease_name = 'scheduler' AND owner = ?
                  AND fencing_token = ?
                """,
                (authority.owner_id, authority.fencing_token),
            )
            return updated.rowcount == 1

    def start_attempt(
        self, attempt_id: str, authority: ExecutionAuthority
    ) -> Attempt:
        with self._transaction(immediate=True) as connection:
            attempt = self._get_attempt(connection, attempt_id)
            if attempt.state is AttemptState.RUNNING:
                raise DomainError(
                    "attempt_fenced",
                    ErrorCategory.CONFLICT,
                    "Running Attempt cannot be restarted by another owner",
                )
            next_state = transition_attempt(attempt.state, AttemptState.RUNNING)
            job = self._get_job(connection, attempt.job_id)
            if job.state is not JobState.RUNNING:
                raise DomainError(
                    "attempt_job_not_running",
                    ErrorCategory.CONFLICT,
                    "Attempt start requires a running Job",
                )
            if job.cancellation_requested:
                raise DomainError(
                    "job_cancelled",
                    ErrorCategory.CANCELLED,
                    "Job cancellation was requested",
                )
            self._assert_scheduler_authority(connection, authority)
            connection.execute(
                """
                UPDATE attempts
                SET state = ?, fencing_token = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    next_state.value,
                    authority.fencing_token,
                    utc_now_millis(),
                    attempt_id,
                ),
            )
            return self._get_attempt(connection, attempt_id)

    def cancel_job(self, job_id: str) -> Job:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if job.state in TERMINAL_JOB_STATES:
                return job
            now = utc_now_millis()
            connection.execute(
                """
                UPDATE jobs SET cancellation_requested = 1, updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            connection.execute(
                """
                UPDATE attempts SET state = ?, updated_at = ?
                WHERE job_id = ? AND state = ?
                """,
                (
                    AttemptState.CANCELLED.value,
                    now,
                    job_id,
                    AttemptState.PENDING.value,
                ),
            )
            self._settle_cancelled_job_if_idle(connection, job_id, now)
            return self._get_job(connection, job_id)

    def is_cancellation_requested(self, job_id: str) -> bool:
        with self._transaction(immediate=False) as connection:
            return self._get_job(connection, job_id).cancellation_requested

    def create_challenge(
        self,
        job_id: str,
        attempt_id: str,
        prompt_json: str,
    ) -> Challenge:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if job.state is not JobState.RUNNING:
                raise DomainError(
                    "challenge_job_not_running",
                    ErrorCategory.CONFLICT,
                    "Challenge creation requires a running Job",
                )
            attempt = self._get_attempt(connection, attempt_id)
            if attempt.job_id != job_id or attempt.state is not AttemptState.NEEDS_INPUT:
                raise DomainError(
                    "challenge_attempt_invalid",
                    ErrorCategory.CONFLICT,
                    "Challenge requires a needs-input Attempt from the same Job",
                )
            pending = connection.execute(
                """
                SELECT 1 FROM challenges
                WHERE job_id = ? AND state = ?
                LIMIT 1
                """,
                (job_id, ChallengeState.PENDING.value),
            ).fetchone()
            if pending is not None:
                raise DomainError(
                    "challenge_pending_exists",
                    ErrorCategory.CONFLICT,
                    "Job already has a pending Challenge",
                )
            challenge_id = new_typed_id("chl")
            now = utc_now_millis()
            connection.execute(
                """
                INSERT INTO challenges (
                    challenge_id, job_id, attempt_id, state, prompt_json,
                    response_json, response_hash, response_attempt_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    challenge_id,
                    job_id,
                    attempt_id,
                    ChallengeState.PENDING.value,
                    prompt_json,
                    now,
                    now,
                ),
            )
            next_state = transition_job(job.state, JobState.WAITING_FOR_INPUT)
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (next_state.value, now, job_id),
            )
            return self._get_challenge(connection, challenge_id)

    def respond_challenge_atomic(
        self,
        job_id: str,
        challenge_id: str,
        *,
        response_hash: str,
        response_json: str,
    ) -> tuple[Job, Attempt]:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            challenge = self._get_challenge(connection, challenge_id)
            if challenge.job_id != job_id:
                raise DomainError(
                    "challenge_job_mismatch",
                    ErrorCategory.CONFLICT,
                    "Challenge does not belong to the Job",
                )
            if challenge.state is ChallengeState.CONSUMED:
                if challenge.response_hash != response_hash:
                    raise DomainError(
                        "challenge_response_conflict",
                        ErrorCategory.CONFLICT,
                        "Challenge was consumed with another response",
                    )
                if challenge.response_attempt_id is None:
                    raise DomainError(
                        "challenge_response_invalid",
                        ErrorCategory.INTERNAL,
                        "Consumed Challenge has no response Attempt",
                    )
                return job, self._get_attempt(
                    connection, challenge.response_attempt_id
                )
            if job.state is not JobState.WAITING_FOR_INPUT:
                raise DomainError(
                    "challenge_job_not_waiting",
                    ErrorCategory.CONFLICT,
                    "Challenge response requires a waiting Job",
                )
            source_attempt = self._get_attempt(connection, challenge.attempt_id)
            response_attempt = self._create_attempt(
                connection, job_id, source_attempt.step_id
            )
            now = utc_now_millis()
            connection.execute(
                """
                UPDATE challenges
                SET state = ?, response_json = ?, response_hash = ?,
                    response_attempt_id = ?, updated_at = ?
                WHERE challenge_id = ?
                """,
                (
                    ChallengeState.CONSUMED.value,
                    response_json,
                    response_hash,
                    response_attempt.attempt_id,
                    now,
                    challenge_id,
                ),
            )
            next_state = transition_job(job.state, JobState.QUEUED)
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (next_state.value, now, job_id),
            )
            return self._get_job(connection, job_id), response_attempt

    def create_retry_job_atomic(
        self,
        original_job_id: str,
        *,
        expected_original_state: JobState,
        confirmed_unknown_operation_ids: tuple[str, ...],
        client_request_id: str,
    ) -> Job:
        with self._transaction(immediate=True) as connection:
            original = self._get_job(connection, original_job_id)
            if original.state not in TERMINAL_JOB_STATES:
                raise DomainError(
                    "retry_original_not_terminal",
                    ErrorCategory.CONFLICT,
                    "Retry requires a terminal original Job",
                )
            if original.state is not expected_original_state:
                raise DomainError(
                    "retry_original_state_conflict",
                    ErrorCategory.CONFLICT,
                    "Original Job state does not match retry expectation",
                )
            if original.client_request_id == client_request_id:
                raise DomainError(
                    "retry_client_request_id_reused",
                    ErrorCategory.CONFLICT,
                    "Retry requires a new client request ID",
                )
            confirmed = tuple(confirmed_unknown_operation_ids)
            if len(confirmed) != len(set(confirmed)):
                raise DomainError(
                    "retry_unknown_operations_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Unknown operation confirmations must be unique",
                )
            unknown_rows = connection.execute(
                """
                SELECT operation_id FROM external_operations
                WHERE job_id = ? AND outcome = ?
                """,
                (original_job_id, "external_outcome_unknown"),
            ).fetchall()
            unknown_ids = {row["operation_id"] for row in unknown_rows}
            if set(confirmed) != unknown_ids:
                raise DomainError(
                    "retry_unknown_operations_unconfirmed",
                    ErrorCategory.CONFLICT,
                    "Retry must confirm exactly every unknown operation",
                )
            return self._create_job(
                connection,
                request_hash=original.request_hash,
                principal=original.principal,
                client_request_id=client_request_id,
                retry_of_job_id=original.job_id,
            )

    def record_checkpoint(
        self,
        metadata: CheckpointMetadata,
        authority: ExecutionAuthority,
    ) -> CheckpointMetadata:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, metadata.job_id)
            if job.state is not JobState.RUNNING:
                raise DomainError(
                    "checkpoint_job_not_running",
                    ErrorCategory.CONFLICT,
                    "Checkpoint requires a running Job",
                )
            if job.cancellation_requested:
                raise DomainError(
                    "job_cancelled",
                    ErrorCategory.CANCELLED,
                    "Job cancellation was requested",
                )
            attempt = self._assert_execution_authority(
                connection,
                metadata.job_id,
                metadata.attempt_id,
                authority,
            )
            if attempt.step_id != metadata.step_id:
                raise DomainError(
                    "attempt_fenced",
                    ErrorCategory.CONFLICT,
                    "Checkpoint step does not belong to the running Attempt",
                )
            connection.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, job_id, step_id, attempt_id, relative_path,
                    schema_id, input_hash, output_hash, byte_length,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata.checkpoint_id,
                    metadata.job_id,
                    metadata.step_id,
                    metadata.attempt_id,
                    metadata.relative_path,
                    metadata.schema_id,
                    metadata.input_hash,
                    metadata.output_hash,
                    metadata.byte_length,
                    metadata.metadata_json,
                    metadata.created_at,
                ),
            )
            return self._checkpoint_from_row(
                connection.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                    (metadata.checkpoint_id,),
                ).fetchone()
            )

    def latest_checkpoint(
        self, job_id: str, step_id: str
    ) -> CheckpointMetadata | None:
        with self._transaction(immediate=False) as connection:
            row = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE job_id = ? AND step_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (job_id, step_id),
            ).fetchone()
            return self._checkpoint_from_row(row) if row is not None else None

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
        authority: ExecutionAuthority,
    ) -> ExternalOperation:
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT operation_id, outcome FROM external_operations
                WHERE job_id = ? AND step_id = ? AND provider = ?
                  AND request_hash = ?
                ORDER BY rowid DESC
                """,
                (job_id, step_id, provider, request_hash),
            ).fetchall()
            by_outcome = {
                outcome: tuple(
                    row["operation_id"]
                    for row in rows
                    if row["outcome"] == outcome.value
                )
                for outcome in ExternalOutcome
            }
            if by_outcome[ExternalOutcome.UNKNOWN]:
                raise DomainError(
                    "external_outcome_unknown",
                    ErrorCategory.CONFLICT,
                    "Unknown external outcome requires explicit confirmation",
                )
            if by_outcome[ExternalOutcome.STARTED]:
                raise DomainError(
                    "external_operation_in_progress",
                    ErrorCategory.CONFLICT,
                    "An external operation for this request is already in progress",
                )
            succeeded = by_outcome[ExternalOutcome.SUCCEEDED]
            if succeeded:
                return self._get_external_operation(connection, succeeded[0])
            job = self._get_job(connection, job_id)
            if job.cancellation_requested:
                raise DomainError(
                    "job_cancelled",
                    ErrorCategory.CANCELLED,
                    "Job cancellation was requested",
                )
            if job.state is not JobState.RUNNING:
                raise DomainError(
                    "external_operation_job_not_running",
                    ErrorCategory.CONFLICT,
                    "External operation requires a running Job",
                )
            attempt = self._assert_execution_authority(
                connection, job_id, attempt_id, authority
            )
            if attempt.step_id != step_id:
                raise DomainError(
                    "attempt_fenced",
                    ErrorCategory.CONFLICT,
                    "External operation step is not owned by the Attempt",
                )
            prepared = by_outcome[ExternalOutcome.PREPARED]
            if prepared:
                connection.execute(
                    """
                    UPDATE external_operations SET attempt_id = ?
                    WHERE operation_id = ? AND outcome = ?
                    """,
                    (
                        attempt_id,
                        prepared[0],
                        ExternalOutcome.PREPARED.value,
                    ),
                )
                return self._get_external_operation(connection, prepared[0])
            operation_id = new_typed_id("op")
            now = utc_now_millis()
            connection.execute(
                """
                INSERT INTO external_operations (
                    operation_id, job_id, step_id, attempt_id, provider,
                    request_hash, operation_idempotency_key,
                    provider_request_id, outcome, summary_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    job_id,
                    step_id,
                    attempt_id,
                    provider,
                    request_hash,
                    operation_idempotency_key,
                    ExternalOutcome.PREPARED.value,
                    summary_json,
                    now,
                    now,
                ),
            )
            return self._get_external_operation(connection, operation_id)

    def start_external_operation(
        self, operation_id: str, authority: ExecutionAuthority
    ) -> ExternalOperation:
        with self._transaction(immediate=True) as connection:
            operation = self._get_external_operation(connection, operation_id)
            unknown = connection.execute(
                """
                SELECT 1 FROM external_operations
                WHERE job_id = ? AND step_id = ? AND provider = ?
                  AND request_hash = ? AND outcome = ?
                LIMIT 1
                """,
                (
                    operation.job_id,
                    operation.step_id,
                    operation.provider,
                    operation.request_hash,
                    ExternalOutcome.UNKNOWN.value,
                ),
            ).fetchone()
            if unknown is not None:
                raise DomainError(
                    "external_outcome_unknown",
                    ErrorCategory.CONFLICT,
                    "Unknown external outcome requires explicit confirmation",
                )
            if operation.outcome is not ExternalOutcome.PREPARED:
                raise DomainError(
                    "external_operation_not_prepared",
                    ErrorCategory.CONFLICT,
                    "Only a prepared external operation can start",
                )
            job = self._get_job(connection, operation.job_id)
            if job.cancellation_requested:
                raise DomainError(
                    "job_cancelled",
                    ErrorCategory.CANCELLED,
                    "Job cancellation was requested",
                )
            attempt = self._assert_execution_authority(
                connection,
                operation.job_id,
                operation.attempt_id,
                authority,
            )
            if attempt.step_id != operation.step_id:
                raise DomainError(
                    "attempt_fenced",
                    ErrorCategory.CONFLICT,
                    "External operation step is not owned by the Attempt",
                )
            connection.execute(
                """
                UPDATE external_operations SET outcome = ?, updated_at = ?
                WHERE operation_id = ? AND outcome = ?
                """,
                (
                    ExternalOutcome.STARTED.value,
                    utc_now_millis(),
                    operation_id,
                    ExternalOutcome.PREPARED.value,
                ),
            )
            return self._get_external_operation(connection, operation_id)

    def finish_external_operation(
        self,
        operation_id: str,
        outcome: ExternalOutcome,
        *,
        provider_request_id: str | None,
        summary_json: str,
    ) -> ExternalOperation:
        if outcome not in (ExternalOutcome.SUCCEEDED, ExternalOutcome.FAILED):
            raise DomainError(
                "external_operation_outcome_invalid",
                ErrorCategory.INVALID_REQUEST,
                "External operation can finish only as succeeded or failed",
            )
        with self._transaction(immediate=True) as connection:
            current = self._get_external_operation(connection, operation_id)
            if current.outcome is not ExternalOutcome.STARTED:
                raise DomainError(
                    "external_operation_not_started",
                    ErrorCategory.CONFLICT,
                    "Only a started external operation can finish",
                )
            connection.execute(
                """
                UPDATE external_operations
                SET outcome = ?, provider_request_id = ?, summary_json = ?,
                    updated_at = ?
                WHERE operation_id = ? AND outcome = ?
                """,
                (
                    outcome.value,
                    provider_request_id,
                    summary_json,
                    utc_now_millis(),
                    operation_id,
                    ExternalOutcome.STARTED.value,
                ),
            )
            return self._get_external_operation(connection, operation_id)

    def get_external_operation(self, operation_id: str) -> ExternalOperation:
        with self._transaction(immediate=False) as connection:
            return self._get_external_operation(connection, operation_id)

    def reconcile_external_operations_after_process_loss(
        self,
        job_id: str,
        authority: ExecutionAuthority,
    ) -> tuple[ExternalOperation, ...]:
        with self._transaction(immediate=True) as connection:
            self._assert_scheduler_authority(connection, authority)
            rows = connection.execute(
                """
                SELECT operations.operation_id
                FROM external_operations AS operations
                JOIN attempts ON attempts.attempt_id = operations.attempt_id
                WHERE operations.job_id = ? AND operations.outcome = ?
                  AND attempts.fencing_token < ?
                ORDER BY operations.operation_id
                """,
                (
                    job_id,
                    ExternalOutcome.STARTED.value,
                    authority.fencing_token,
                ),
            ).fetchall()
            operation_ids = tuple(row["operation_id"] for row in rows)
            if operation_ids:
                connection.executemany(
                    """
                    UPDATE external_operations SET outcome = ?, updated_at = ?
                    WHERE operation_id = ? AND outcome = ?
                    """,
                    (
                        (
                            ExternalOutcome.UNKNOWN.value,
                            utc_now_millis(),
                            operation_id,
                            ExternalOutcome.STARTED.value,
                        )
                        for operation_id in operation_ids
                    ),
                )
            return tuple(
                self._get_external_operation(connection, operation_id)
                for operation_id in operation_ids
            )

    def append_event(
        self, job_id: str, event_type: str, payload_json: str
    ) -> JobEvent:
        with self._transaction(immediate=True) as connection:
            self._get_job(connection, job_id)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            event_id = new_typed_id("evt")
            created_at = utc_now_millis()
            connection.execute(
                """
                INSERT INTO events (
                    event_id, job_id, sequence, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, job_id, sequence, event_type, payload_json, created_at),
            )
            return JobEvent(
                event_id=event_id,
                job_id=job_id,
                sequence=sequence,
                event_type=event_type,
                payload_json=payload_json,
                created_at=created_at,
            )

    def list_events(
        self, job_id: str, after_sequence: int = 0
    ) -> tuple[JobEvent, ...]:
        with self._transaction(immediate=False) as connection:
            self._get_job(connection, job_id)
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence
                """,
                (job_id, after_sequence),
            ).fetchall()
            return tuple(self._event_from_row(row) for row in rows)

    def transition_job(self, job_id: str, state: JobState) -> Job:
        with self._transaction(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if state in (JobState.SUCCEEDED, JobState.CANCELLED):
                raise DomainError(
                    "job_terminal_transition_guarded",
                    ErrorCategory.CONFLICT,
                    "Job success and cancellation require their guarded paths",
                )
            next_state = transition_job(job.state, state)
            if next_state in TERMINAL_JOB_STATES:
                self._ensure_attempts_settled(connection, job_id)
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (next_state.value, utc_now_millis(), job_id),
            )
            return self._get_job(connection, job_id)

    def transition_attempt(
        self,
        attempt_id: str,
        state: AttemptState,
        *,
        authority: ExecutionAuthority | None = None,
    ) -> Attempt:
        with self._transaction(immediate=True) as connection:
            if state is AttemptState.RUNNING:
                raise DomainError(
                    "attempt_start_required",
                    ErrorCategory.CONFLICT,
                    "Attempt must enter running through the fenced start operation",
                )
            attempt = self._get_attempt(connection, attempt_id)
            if attempt.state is AttemptState.RUNNING:
                if authority is None:
                    raise DomainError(
                        "execution_authority_required",
                        ErrorCategory.CONFLICT,
                        "Running Attempt transition requires execution authority",
                    )
                self._assert_execution_authority(
                    connection, attempt.job_id, attempt.attempt_id, authority
                )
            next_state = transition_attempt(attempt.state, state)
            now = utc_now_millis()
            connection.execute(
                "UPDATE attempts SET state = ?, updated_at = ? WHERE attempt_id = ?",
                (next_state.value, now, attempt_id),
            )
            self._settle_cancelled_job_if_idle(connection, attempt.job_id, now)
            return self._get_attempt(connection, attempt_id)

    @contextmanager
    def commit_guard(
        self,
        job_id: str,
        attempt_id: str,
        authority: ExecutionAuthority,
    ) -> Iterator[sqlite3.Connection]:
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
            if job.cancellation_requested:
                raise DomainError(
                    "commit_guard_cancelled",
                    ErrorCategory.CANCELLED,
                    "Cancellation won before portable commit",
                )
            self._assert_execution_authority(
                connection, job_id, attempt_id, authority
            )
            self._ensure_no_other_unsettled_attempts(
                connection, job_id, attempt_id
            )
            yield connection
            now = utc_now_millis()
            attempt_updated = connection.execute(
                """
                UPDATE attempts SET state = ?, updated_at = ?
                WHERE attempt_id = ? AND state = ? AND fencing_token = ?
                """,
                (
                    AttemptState.SUCCEEDED.value,
                    now,
                    attempt_id,
                    AttemptState.RUNNING.value,
                    authority.fencing_token,
                ),
            )
            if attempt_updated.rowcount != 1:
                raise DomainError(
                    "commit_guard_conflict",
                    ErrorCategory.CONFLICT,
                    "Attempt authority changed before success",
                )
            self._ensure_attempts_settled(connection, job_id)
            updated = connection.execute(
                """
                UPDATE jobs SET state = ?, updated_at = ?
                WHERE job_id = ? AND state = ? AND cancellation_requested = 0
                """,
                (
                    JobState.SUCCEEDED.value,
                    now,
                    job_id,
                    JobState.RUNNING.value,
                ),
            )
            if updated.rowcount != 1:
                raise DomainError(
                    "commit_guard_conflict",
                    ErrorCategory.CONFLICT,
                    "Portable commit authority changed before success",
                )
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

    def _create_job(
        self,
        connection: sqlite3.Connection,
        *,
        request_hash: str,
        principal: str,
        client_request_id: str | None,
        retry_of_job_id: str | None,
    ) -> Job:
        if client_request_id is not None:
            existing = connection.execute(
                """
                SELECT * FROM jobs
                WHERE principal = ? AND client_request_id = ?
                """,
                (principal, client_request_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_hash"] != request_hash
                    or existing["retry_of_job_id"] != retry_of_job_id
                ):
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
                cancellation_requested, retry_of_job_id, result_json,
                error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, NULL, NULL, ?, ?)
            """,
            (
                job_id,
                request_hash,
                principal,
                client_request_id,
                JobState.QUEUED.value,
                retry_of_job_id,
                now,
                now,
            ),
        )
        return self._get_job(connection, job_id)

    def _create_attempt(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        step_id: str,
    ) -> Attempt:
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
            ) VALUES (?, ?, ?, ?, 0, ?, ?)
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

    def _assert_scheduler_authority(
        self,
        connection: sqlite3.Connection,
        authority: ExecutionAuthority,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM leases
            WHERE lease_name = 'scheduler' AND owner = ?
              AND fencing_token = ? AND CAST(expires_at AS INTEGER) > ?
            """,
            (
                authority.owner_id,
                authority.fencing_token,
                self._clock(),
            ),
        ).fetchone()
        if row is None:
            raise DomainError(
                "attempt_fenced",
                ErrorCategory.CONFLICT,
                "Execution authority is expired or fenced",
            )

    def _assert_execution_authority(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        attempt_id: str,
        authority: ExecutionAuthority,
    ) -> Attempt:
        attempt = self._get_attempt(connection, attempt_id)
        if (
            attempt.job_id != job_id
            or attempt.state is not AttemptState.RUNNING
            or attempt.fencing_token < 1
            or attempt.fencing_token != authority.fencing_token
        ):
            raise DomainError(
                "attempt_fenced",
                ErrorCategory.CONFLICT,
                "Attempt is not owned by the current execution authority",
            )
        self._assert_scheduler_authority(connection, authority)
        return attempt

    @staticmethod
    def _validate_owner_id(owner_id: str) -> None:
        if (
            type(owner_id) is not str
            or not owner_id.strip()
            or owner_id.isdecimal()
        ):
            raise DomainError(
                "execution_authority_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Scheduler owner must identify a process instance, not only a PID",
            )

    @staticmethod
    def _settle_cancelled_job_if_idle(
        connection: sqlite3.Connection,
        job_id: str,
        now: str,
    ) -> None:
        job_row = connection.execute(
            "SELECT state, cancellation_requested FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if job_row is None or not job_row["cancellation_requested"]:
            return
        running = connection.execute(
            """
            SELECT 1 FROM attempts
            WHERE job_id = ? AND state = ? LIMIT 1
            """,
            (job_id, AttemptState.RUNNING.value),
        ).fetchone()
        if running is not None:
            return
        current = JobState(job_row["state"])
        if current in TERMINAL_JOB_STATES:
            return
        next_state = transition_job(current, JobState.CANCELLED)
        connection.execute(
            "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
            (next_state.value, now, job_id),
        )

    @staticmethod
    def _ensure_no_other_unsettled_attempts(
        connection: sqlite3.Connection,
        job_id: str,
        attempt_id: str,
    ) -> None:
        unsettled = connection.execute(
            """
            SELECT 1 FROM attempts
            WHERE job_id = ? AND attempt_id != ? AND state IN (?, ?)
            LIMIT 1
            """,
            (
                job_id,
                attempt_id,
                AttemptState.PENDING.value,
                AttemptState.RUNNING.value,
            ),
        ).fetchone()
        if unsettled is not None:
            raise DomainError(
                "attempt_not_settled",
                ErrorCategory.CONFLICT,
                "All other Attempts must settle before portable commit",
            )

    @staticmethod
    def _ensure_attempts_settled(
        connection: sqlite3.Connection, job_id: str
    ) -> None:
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

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            request_hash=row["request_hash"],
            principal=row["principal"],
            client_request_id=row["client_request_id"],
            state=JobState(row["state"]),
            cancellation_requested=bool(row["cancellation_requested"]),
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
        return self._attempt_from_row(row)

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> Attempt:
        return Attempt(
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            step_id=row["step_id"],
            state=AttemptState(row["state"]),
            fencing_token=row["fencing_token"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _get_challenge(
        self, connection: sqlite3.Connection, challenge_id: str
    ) -> Challenge:
        row = connection.execute(
            "SELECT * FROM challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "challenge_not_found",
                ErrorCategory.INVALID_REQUEST,
                "Challenge does not exist",
            )
        return self._challenge_from_row(row)

    def _get_external_operation(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> ExternalOperation:
        row = connection.execute(
            "SELECT * FROM external_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "external_operation_not_found",
                ErrorCategory.INVALID_REQUEST,
                "External operation does not exist",
            )
        return self._external_operation_from_row(row)

    @staticmethod
    def _challenge_from_row(row: sqlite3.Row) -> Challenge:
        return Challenge(
            challenge_id=row["challenge_id"],
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            state=ChallengeState(row["state"]),
            prompt_json=row["prompt_json"],
            response_json=row["response_json"],
            response_hash=row["response_hash"],
            response_attempt_id=row["response_attempt_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _external_operation_from_row(row: sqlite3.Row) -> ExternalOperation:
        return ExternalOperation(
            operation_id=row["operation_id"],
            job_id=row["job_id"],
            step_id=row["step_id"],
            attempt_id=row["attempt_id"],
            provider=row["provider"],
            request_hash=row["request_hash"],
            operation_idempotency_key=row["operation_idempotency_key"],
            provider_request_id=row["provider_request_id"],
            outcome=ExternalOutcome(row["outcome"]),
            summary_json=row["summary_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> CheckpointMetadata:
        return CheckpointMetadata(
            checkpoint_id=row["checkpoint_id"],
            job_id=row["job_id"],
            step_id=row["step_id"],
            attempt_id=row["attempt_id"],
            relative_path=row["relative_path"],
            schema_id=row["schema_id"],
            input_hash=row["input_hash"],
            output_hash=row["output_hash"],
            byte_length=row["byte_length"],
            metadata_json=row["metadata_json"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            event_id=row["event_id"],
            job_id=row["job_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            payload_json=row["payload_json"],
            created_at=row["created_at"],
        )
