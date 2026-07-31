from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.jobs import sqlite_repository as repository_module
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.ids import new_typed_id, sha256_digest
from app.core.domain.video import (
    JobState,
    QualityOverall,
    VideoDocumentKind,
    VideoProducedDocument,
)
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.jobs.cancellation import CancellationToken
from app.core.jobs.model import AttemptState, JobExecutionBinding
from app.core.jobs.state_machine import (
    LEGAL_ATTEMPT_TRANSITIONS,
    LEGAL_JOB_TRANSITIONS,
    TERMINAL_ATTEMPT_STATES,
    TERMINAL_JOB_STATES,
    transition_attempt,
    transition_job,
)
from app.core.ports.jobs import (
    PortableCommitReceipt,
    SourceIdentityBinding,
    VideoResultPlan,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
RUN_ID = new_typed_id("run", now_ms=1, randomness=b"\x01" * 10)
BUNDLE_ID = new_typed_id("bnd", now_ms=1, randomness=b"\x02" * 10)
NEXT_BUNDLE_ID = new_typed_id("bnd", now_ms=1, randomness=b"\x03" * 10)
SOURCE_ID = new_typed_id("src", now_ms=1, randomness=b"\x04" * 10)
OTHER_SOURCE_ID = new_typed_id("src", now_ms=1, randomness=b"\x05" * 10)
REVISION_ID = new_typed_id("rev", now_ms=1, randomness=b"\x06" * 10)
PRIMARY_DRAFT_ID = new_typed_id("art", now_ms=1, randomness=b"\x07" * 10)
TRANSCRIPT_ID = new_typed_id("art", now_ms=1, randomness=b"\x08" * 10)
EVIDENCE_SET_ID = new_typed_id("art", now_ms=1, randomness=b"\x09" * 10)
QUALITY_REPORT_ID = new_typed_id("art", now_ms=1, randomness=b"\x0a" * 10)
DISPLAY_ASSET_ID = new_typed_id("art", now_ms=1, randomness=b"\x0b" * 10)
FAITHFUL_DRAFT_ID = new_typed_id("art", now_ms=1, randomness=b"\x0c" * 10)
FAITHFUL_QUALITY_ID = new_typed_id("art", now_ms=1, randomness=b"\x0d" * 10)
EXPECTED_TABLES = {
    "attempts",
    "challenges",
    "checkpoints",
    "events",
    "external_operations",
    "jobs",
    "job_execution_bindings",
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


def _video_commit_inputs(
    job_id: str,
    *,
    source_id: str = SOURCE_ID,
    bundle_id: str = BUNDLE_ID,
) -> tuple[VideoResultPlan, SourceIdentityBinding, PortableCommitReceipt]:
    plan = VideoResultPlan(
        job_id=job_id,
        run_id=RUN_ID,
        bundle_id=bundle_id,
        manifest_sha256=HASH_A,
        source_id=source_id,
        source_revision_id=REVISION_ID,
        primary_draft_artifact_id=PRIMARY_DRAFT_ID,
        transcript_artifact_id=TRANSCRIPT_ID,
        evidence_set_artifact_id=EVIDENCE_SET_ID,
        quality_report_artifact_id=QUALITY_REPORT_ID,
        display_asset_ids=(),
        quality_overall=QualityOverall.PASS,
        publish_eligible=True,
        usage={"input_tokens": 3, "output_tokens": 2},
        warnings=(),
    )
    binding = SourceIdentityBinding(
        connector_id="fixture",
        canonical_identity="fixture://course",
        source_id=source_id,
        owning_bundle_id=bundle_id,
        manifest_sha256=HASH_A,
    )
    receipt = PortableCommitReceipt(
        bundle_id=bundle_id,
        manifest_sha256=HASH_A,
        commit_sha256=HASH_B,
        workspace_relative_bundle_path=f"raw/personal/bundles/{bundle_id}",
        idempotent=False,
    )
    return plan, binding, receipt


@pytest.fixture
def repo(tmp_path: Path) -> SqliteJobRepository:
    yield SqliteJobRepository.open(tmp_path / "machine-root")


def test_initial_job_event_is_atomic_with_job_creation(
    repo: SqliteJobRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_json = "{}"
    with monkeypatch.context() as patch:
        patch.setattr(
            repo,
            "_insert_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.IntegrityError("injected event failure")
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
            repo.create_job(
                request_hash=sha256_digest(request_json),
                request_json=request_json,
                principal="local",
                client_request_id="atomic-initial-event",
                initial_events=(("configuration.snapshot.v1", "{}"),),
            )

    created = repo.create_job(
        request_hash=sha256_digest(request_json),
        request_json=request_json,
        principal="local",
        client_request_id="atomic-initial-event",
        initial_events=(("configuration.snapshot.v1", "{}"),),
    )
    assert [event.event_type for event in repo.list_events(created.job_id)] == [
        "configuration.snapshot.v1"
    ]


def test_idempotent_job_replay_rejects_changed_initial_snapshot(
    repo: SqliteJobRepository,
) -> None:
    request_json = "{}"
    created = repo.create_job(
        request_hash=sha256_digest(request_json),
        request_json=request_json,
        principal="local",
        client_request_id="snapshot-idempotency",
        initial_events=(("configuration.snapshot.v1", '{"digest":"first"}'),),
    )

    with pytest.raises(DomainError, match="idempotency_conflict"):
        repo.create_job(
            request_hash=sha256_digest(request_json),
            request_json=request_json,
            principal="local",
            client_request_id="snapshot-idempotency",
            initial_events=(
                ("configuration.snapshot.v1", '{"digest":"second"}'),
            ),
        )

    assert [event.payload_json for event in repo.list_events(created.job_id)] == [
        '{"digest":"first"}'
    ]


def test_initial_job_event_rejects_sensitive_fields_before_creation(
    repo: SqliteJobRepository,
) -> None:
    request_json = "{}"

    with pytest.raises(DomainError, match="job_event_invalid"):
        repo.create_job(
            request_hash=sha256_digest(request_json),
            request_json=request_json,
            principal="local",
            client_request_id="sensitive-initial-event",
            initial_events=(
                ("configuration.snapshot.v1", '{"api_key":"secret-canary"}'),
            ),
        )


def _authority(repo: SqliteJobRepository):
    return repo.acquire_scheduler_lease(
        "test-workspace:test-process", ttl_seconds=300
    )


def _terminalize_job(
    repo: SqliteJobRepository,
    job_id: str,
    terminal_state: JobState,
):
    if terminal_state is JobState.CANCELLED:
        return repo.cancel_job(job_id)
    repo.transition_job(job_id, JobState.RUNNING)
    if terminal_state is JobState.FAILED:
        return repo.transition_job(job_id, terminal_state)
    authority = _authority(repo)
    attempt = repo.create_attempt(job_id, "terminal-setup")
    attempt = repo.start_attempt(attempt.attempt_id, authority)
    with repo.commit_guard(job_id, attempt.attempt_id, authority):
        pass
    return repo.get_job(job_id)


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


def test_open_creates_version_two_database_with_exact_schema_and_pragmas(
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
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert tables == EXPECTED_TABLES
    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert synchronous == 2
    assert busy_timeout == 5_000
    assert user_version == 2
    assert foreign_key_violations == []


def test_job_store_fails_closed_when_wal_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResult:
        def fetchone(self):
            return ("delete",)

    class FakeConnection:
        row_factory = None

        def execute(self, statement: str):
            if statement in {"PRAGMA foreign_keys = ON"} or statement.startswith(
                "PRAGMA busy_timeout"
            ):
                return FakeResult()
            if statement == "PRAGMA journal_mode = WAL":
                return FakeResult()
            pytest.fail(f"unexpected statement after rejected WAL mode: {statement}")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        repository_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    with pytest.raises(DomainError) as raised:
        SqliteJobRepository.open(tmp_path / "wal-unavailable")

    assert raised.value.code == "job_store_wal_unavailable"
    assert raised.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_busy_begin_immediate_is_stable_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "_BUSY_TIMEOUT_MS", 25)
    repo = SqliteJobRepository.open(tmp_path / "busy-store")
    holder = sqlite3.connect(repo.database_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DomainError) as raised:
            repo.create_job(
                request_hash=HASH_A,
                principal="local",
                client_request_id=None,
            )
    finally:
        holder.rollback()
        holder.close()

    assert raised.value.code == "job_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert dict(raised.value.details) == {
        "sqlite_result": "busy",
        "busy_timeout_ms": 25,
    }
    assert "database" not in raised.value.message.casefold()


def test_busy_connect_is_stable_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = sqlite3.OperationalError("private SQLite connect detail")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    def fail_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise error

    monkeypatch.setattr(repository_module.sqlite3, "connect", fail_connect)
    repo = SqliteJobRepository(tmp_path / "connect-busy", clock=lambda: 1)

    with pytest.raises(DomainError) as raised:
        repo._connect()

    assert raised.value.code == "job_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert "private" not in str(raised.value)


@pytest.mark.parametrize(
    ("primary_result", "result_name"),
    (
        (sqlite3.SQLITE_BUSY, "busy"),
        (sqlite3.SQLITE_LOCKED, "locked"),
    ),
)
def test_extended_lock_result_uses_primary_result_code(
    primary_result: int,
    result_name: str,
) -> None:
    error = sqlite3.OperationalError("private SQLite detail")
    error.sqlite_errorcode = primary_result | (7 << 8)

    with pytest.raises(DomainError) as raised:
        repository_module._raise_if_job_store_busy(error)

    assert raised.value.code == "job_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert raised.value.details["sqlite_result"] == result_name
    assert "private" not in raised.value.message


def test_open_contention_is_not_reported_as_schema_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = sqlite3.OperationalError("database is locked at a private path")
    error.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    monkeypatch.setattr(
        SqliteJobRepository,
        "_connect",
        lambda _self: (_ for _ in ()).throw(error),
    )

    with pytest.raises(DomainError) as raised:
        SqliteJobRepository.open(tmp_path / "open-busy")

    assert raised.value.code == "job_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert "private path" not in str(raised.value)


def test_authorize_attempt_storage_maps_busy_statement(
    repo: SqliteJobRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = sqlite3.OperationalError("private SQLite statement detail")
    error.sqlite_errorcode = sqlite3.SQLITE_LOCKED

    def fail_authority(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(repo, "_assert_execution_authority", fail_authority)

    with pytest.raises(DomainError) as raised:
        repo.authorize_attempt_storage("job", "attempt", _authority(repo))

    assert raised.value.code == "job_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert raised.value.details["sqlite_result"] == "locked"
    assert "private" not in str(raised.value)


def test_version_one_database_migrates_execution_binding_and_reopens(
    tmp_path: Path,
) -> None:
    machine_root, database_path = _database_path(tmp_path, "migrate-v1")
    request_json = json.dumps(
        {
            "recipe_id": "alltonote.video-course-note",
            "recipe_version": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(database_path) as connection:
        for statement in repository_module._SCHEMA_STATEMENTS_V1:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, request_hash, request_json, principal,
                client_request_id, state, cancellation_requested,
                retry_of_job_id, result_json, error_json, created_at, updated_at
            ) VALUES ('job_legacy', ?, ?, 'local', 'legacy', 'queued', 0,
                      NULL, NULL, NULL, '1', '1')
            """,
            (sha256_digest(request_json), request_json),
        )
        connection.execute("PRAGMA user_version = 1")

    migrated = SqliteJobRepository.open(machine_root)

    assert migrated.get_job_execution_binding("job_legacy") == JobExecutionBinding(
        recipe_id="alltonote.video-course-note",
        recipe_version=2,
        executor_id="alltonote.video",
        executor_version=1,
        pack_id="media-basic",
        pack_version="legacy-v1",
    )
    with migrated._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    reopened = SqliteJobRepository.open(machine_root)
    assert reopened.get_job_execution_binding("job_legacy") == (
        migrated.get_job_execution_binding("job_legacy")
    )


def test_version_one_migration_failure_rolls_back_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_root, database_path = _database_path(tmp_path, "migrate-v1-rollback")
    request_json = '{"recipe_id":"alltonote.video-course-note","recipe_version":2}'
    with sqlite3.connect(database_path) as connection:
        for statement in repository_module._SCHEMA_STATEMENTS_V1:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, request_hash, request_json, principal,
                client_request_id, state, cancellation_requested,
                retry_of_job_id, result_json, error_json, created_at, updated_at
            ) VALUES ('job_legacy_rollback', ?, ?, 'local', 'legacy-rollback',
                      'queued', 0, NULL, NULL, NULL, '1', '1')
            """,
            (sha256_digest(request_json), request_json),
        )
        connection.execute("PRAGMA user_version = 1")

    def fail_after_schema_change(
        _self: SqliteJobRepository,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(repository_module._EXECUTION_BINDING_SCHEMA)
        raise RuntimeError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            SqliteJobRepository,
            "_migrate_v1_to_v2",
            fail_after_schema_change,
        )
        with pytest.raises(RuntimeError, match="injected migration failure"):
            SqliteJobRepository.open(machine_root)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'job_execution_bindings'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT request_json FROM jobs WHERE job_id = 'job_legacy_rollback'"
        ).fetchone()[0] == request_json

    migrated = SqliteJobRepository.open(machine_root)
    assert migrated.get_job_execution_binding("job_legacy_rollback") == (
        JobExecutionBinding(
            recipe_id="alltonote.video-course-note",
            recipe_version=2,
            executor_id="alltonote.video",
            executor_version=1,
            pack_id="media-basic",
            pack_version="legacy-v1",
        )
    )


def test_new_job_persists_exact_execution_binding(repo: SqliteJobRepository) -> None:
    binding = JobExecutionBinding(
        recipe_id="alltonote.document-note",
        recipe_version=1,
        executor_id="alltonote.document",
        executor_version=1,
        pack_id="document-basic",
        pack_version="docling-2.117.0",
    )

    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id="document-binding",
        execution_binding=binding,
    )

    assert repo.get_job_execution_binding(job.job_id) == binding


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
    assert "cancellation_requested" in all_column_names


def test_schema_rejects_non_boolean_cancellation_and_non_scheduler_lease(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with repo._transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE jobs SET cancellation_requested = 2 WHERE job_id = ?",
                (job.job_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with repo._transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO leases (
                    lease_name, job_id, owner, fencing_token,
                    expires_at, heartbeat_at
                ) VALUES ('not-scheduler', NULL, 'owner', 1, '1', '0')
                """
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
    _terminalize_job(repo, job.job_id, terminal_state)

    with repo._connect() as connection:
        before_step_count = connection.execute(
            "SELECT COUNT(*) FROM steps WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
        before_attempt_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]

    with pytest.raises(DomainError, match="job_terminal"):
        repo.create_attempt(job.job_id, "must-not-exist")

    with repo._connect() as connection:
        step_count = connection.execute(
            "SELECT COUNT(*) FROM steps WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
    assert step_count == before_step_count
    assert attempt_count == before_attempt_count


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
        repo.start_attempt(attempt.attempt_id, _authority(repo))

    with pytest.raises(DomainError, match="attempt_not_settled"):
        repo.transition_job(job.job_id, JobState.FAILED)


def test_commit_guard_can_succeed_job_after_all_attempts_are_settled(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    attempt = repo.create_attempt(job.job_id, "resolve")
    authority = _authority(repo)
    attempt = repo.start_attempt(attempt.attempt_id, authority)

    with repo.commit_guard(job.job_id, attempt.attempt_id, authority):
        pass

    assert repo.get_job(job.job_id).state is JobState.SUCCEEDED


def test_commit_guard_maps_busy_before_portable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "_BUSY_TIMEOUT_MS", 25)
    repo = SqliteJobRepository.open(tmp_path / "guard-busy")
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "commit").attempt_id,
        authority,
    )
    holder = sqlite3.connect(repo.database_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    portable_commit_calls = 0
    try:
        with pytest.raises(DomainError) as raised:
            with repo.commit_guard(job.job_id, attempt.attempt_id, authority):
                portable_commit_calls += 1
    finally:
        holder.rollback()
        holder.close()

    assert raised.value.code == "job_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert portable_commit_calls == 0
    assert repo.get_job(job.job_id).state is JobState.RUNNING


def test_commit_guard_maps_busy_commit_without_replaying_callback(
    repo: SqliteJobRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "commit").attempt_id,
        authority,
    )
    original_connect = repo._connect
    first_connection = True

    class BusyCommitConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, *args: object, **kwargs: object) -> sqlite3.Cursor:
            return self._connection.execute(*args, **kwargs)

        def commit(self) -> None:
            error = sqlite3.OperationalError("private SQLite commit detail")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

        def rollback(self) -> None:
            self._connection.rollback()

        def close(self) -> None:
            self._connection.close()

    def connect_with_busy_first_commit() -> sqlite3.Connection:
        nonlocal first_connection
        connection = original_connect()
        if first_connection:
            first_connection = False
            return BusyCommitConnection(connection)  # type: ignore[return-value]
        return connection

    monkeypatch.setattr(repo, "_connect", connect_with_busy_first_commit)
    portable_commit_calls = 0

    with pytest.raises(DomainError) as raised:
        with repo.commit_guard(job.job_id, attempt.attempt_id, authority):
            portable_commit_calls += 1

    assert raised.value.code == "job_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert portable_commit_calls == 1
    assert repo.get_job(job.job_id).state is JobState.RUNNING


@pytest.mark.parametrize("terminal_state", (JobState.SUCCEEDED, JobState.CANCELLED))
def test_generic_job_transition_cannot_enter_guarded_terminal_states(
    repo: SqliteJobRepository,
    terminal_state: JobState,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    if terminal_state is JobState.SUCCEEDED:
        repo.transition_job(job.job_id, JobState.RUNNING)

    with pytest.raises(DomainError, match="job_terminal_transition_guarded"):
        repo.transition_job(job.job_id, terminal_state)

    expected = JobState.RUNNING if terminal_state is JobState.SUCCEEDED else JobState.QUEUED
    assert repo.get_job(job.job_id).state is expected


def test_commit_guard_rejects_non_running_job(repo: SqliteJobRepository) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    attempt = repo.create_attempt(job.job_id, "resolve")
    authority = _authority(repo)

    with pytest.raises(DomainError, match="commit_guard_not_running"):
        with repo.commit_guard(job.job_id, attempt.attempt_id, authority):
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
    authority = _authority(repo)
    pending = repo.create_attempt(job.job_id, "commit")
    attempt = repo.start_attempt(pending.attempt_id, authority)

    with pytest.raises(RuntimeError, match="portable commit failed"):
        with repo.commit_guard(
            job.job_id, attempt.attempt_id, authority
        ) as transaction:
            assert isinstance(transaction, sqlite3.Connection)
            transaction.execute(
                "UPDATE jobs SET state = ? WHERE job_id = ?",
                (JobState.FAILED.value, job.job_id),
            )
            raise RuntimeError("portable commit failed")

    assert repo.get_job(job.job_id).state is JobState.RUNNING


def test_commit_video_result_atomic_persists_result_identity_and_success(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.create_attempt(job.job_id, "commit")
    attempt = repo.start_attempt(attempt.attempt_id, authority)
    plan, binding, receipt = _video_commit_inputs(job.job_id)

    returned = repo.commit_video_result_atomic(
        job.job_id,
        attempt.attempt_id,
        authority,
        result_plan=plan,
        source_identity=binding,
        commit=lambda: receipt,
    )

    assert returned.result.job_id == plan.job_id
    assert returned.result.commit_sha256 == receipt.commit_sha256
    assert returned.source_identity == binding
    assert repo.get_job(job.job_id).state is JobState.SUCCEEDED
    assert repo.get_job_result(job.job_id) == returned.result
    with repo._connect() as connection:
        identity = connection.execute(
            "SELECT * FROM source_identities WHERE connector_id = 'fixture'"
        ).fetchone()
    assert identity is not None
    assert identity["source_id"] == binding.source_id
    assert identity["owning_bundle_id"] == plan.bundle_id


def test_commit_video_result_round_trips_v2_documents(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "commit").attempt_id,
        authority,
    )
    plan, binding, receipt = _video_commit_inputs(job.job_id)
    documents = (
        VideoProducedDocument(
            VideoDocumentKind.KNOWLEDGE_NOTE,
            PRIMARY_DRAFT_ID,
            QUALITY_REPORT_ID,
            QualityOverall.PASS,
            True,
        ),
        VideoProducedDocument(
            VideoDocumentKind.FAITHFUL_EDITION,
            FAITHFUL_DRAFT_ID,
            FAITHFUL_QUALITY_ID,
            QualityOverall.PASS_WITH_WARNINGS,
            True,
        ),
    )

    returned = repo.commit_video_result_atomic(
        job.job_id,
        attempt.attempt_id,
        authority,
        result_plan=replace(plan, documents=documents),
        source_identity=binding,
        commit=lambda: receipt,
    )

    assert returned.result.documents == documents
    assert repo.get_job_result(job.job_id).documents == documents


def test_commit_video_result_atomic_rolls_back_when_callback_fails(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.create_attempt(job.job_id, "commit")
    attempt = repo.start_attempt(attempt.attempt_id, authority)

    plan, binding, _ = _video_commit_inputs(job.job_id)

    def fail_commit() -> PortableCommitReceipt:
        raise RuntimeError("portable commit failed")

    with pytest.raises(RuntimeError, match="portable commit failed"):
        repo.commit_video_result_atomic(
            job.job_id,
            attempt.attempt_id,
            authority,
            result_plan=plan,
            source_identity=binding,
            commit=fail_commit,
        )

    assert repo.get_job(job.job_id).state is JobState.RUNNING
    assert repo.get_job_result(job.job_id) is None


def test_commit_video_result_atomic_rolls_back_on_source_identity_conflict(
    repo: SqliteJobRepository,
) -> None:
    first = repo.create_job(
        request_hash=HASH_A,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(first.job_id, JobState.RUNNING)
    authority = _authority(repo)
    first_attempt = repo.create_attempt(first.job_id, "commit")
    first_attempt = repo.start_attempt(first_attempt.attempt_id, authority)
    first_plan, first_binding, first_receipt = _video_commit_inputs(first.job_id)
    repo.commit_video_result_atomic(
        first.job_id,
        first_attempt.attempt_id,
        authority,
        result_plan=first_plan,
        source_identity=first_binding,
        commit=lambda: first_receipt,
    )

    second = repo.create_job(
        request_hash=HASH_B,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(second.job_id, JobState.RUNNING)
    second_attempt = repo.create_attempt(second.job_id, "commit")
    second_attempt = repo.start_attempt(second_attempt.attempt_id, authority)

    plan, binding, receipt = _video_commit_inputs(
        second.job_id,
        source_id=OTHER_SOURCE_ID,
        bundle_id=NEXT_BUNDLE_ID,
    )
    callback_calls = 0

    def commit() -> PortableCommitReceipt:
        nonlocal callback_calls
        callback_calls += 1
        return receipt

    with pytest.raises(DomainError, match="source_identity_conflict"):
        repo.commit_video_result_atomic(
            second.job_id,
            second_attempt.attempt_id,
            authority,
            result_plan=plan,
            source_identity=binding,
            commit=commit,
        )

    assert callback_calls == 0
    assert repo.get_job(second.job_id).state is JobState.RUNNING
    assert repo.get_job_result(second.job_id) is None


def test_commit_video_result_atomic_advances_same_source_binding(
    repo: SqliteJobRepository,
) -> None:
    first = repo.create_job(
        request_hash=HASH_A, principal="local", client_request_id=None
    )
    repo.transition_job(first.job_id, JobState.RUNNING)
    authority = _authority(repo)
    first_attempt = repo.start_attempt(
        repo.create_attempt(first.job_id, "commit").attempt_id, authority
    )
    plan, binding, receipt = _video_commit_inputs(first.job_id)
    repo.commit_video_result_atomic(
        first.job_id,
        first_attempt.attempt_id,
        authority,
        result_plan=plan,
        source_identity=binding,
        commit=lambda: receipt,
    )

    second = repo.create_job(
        request_hash=HASH_B, principal="local", client_request_id=None
    )
    repo.transition_job(second.job_id, JobState.RUNNING)
    second_attempt = repo.start_attempt(
        repo.create_attempt(second.job_id, "commit").attempt_id, authority
    )
    next_plan, next_binding, next_receipt = _video_commit_inputs(
        second.job_id, bundle_id=NEXT_BUNDLE_ID
    )

    repo.commit_video_result_atomic(
        second.job_id,
        second_attempt.attempt_id,
        authority,
        result_plan=next_plan,
        source_identity=next_binding,
        commit=lambda: next_receipt,
    )

    assert repo.get_job(second.job_id).state is JobState.SUCCEEDED
    assert repo.get_job_result(second.job_id) is not None


@pytest.mark.parametrize("failure", ("mismatch", "noncanonical"))
def test_commit_video_result_atomic_rejects_plan_before_callback(
    repo: SqliteJobRepository,
    failure: str,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A, principal="local", client_request_id=None
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "commit").attempt_id, authority
    )
    plan, binding, receipt = _video_commit_inputs(job.job_id)
    if failure == "mismatch":
        binding = SourceIdentityBinding(
            connector_id=binding.connector_id,
            canonical_identity=binding.canonical_identity,
            source_id="src_mismatch",
            owning_bundle_id=binding.owning_bundle_id,
            manifest_sha256=binding.manifest_sha256,
        )
    else:
        plan = VideoResultPlan(
            **{
                **plan.__dict__,
                "usage": {"input_tokens": object()},
            }
        )
    callback_calls = 0

    def commit() -> PortableCommitReceipt:
        nonlocal callback_calls
        callback_calls += 1
        return receipt

    with pytest.raises(DomainError, match="job_result_plan_invalid"):
        repo.commit_video_result_atomic(
            job.job_id,
            attempt.attempt_id,
            authority,
            result_plan=plan,
            source_identity=binding,
            commit=commit,
        )

    assert callback_calls == 0


@pytest.mark.parametrize(
    "malformation",
    (
        "job_id",
        "run_id",
        "bundle_id",
        "source_id",
        "revision_id",
        "primary_draft_id",
        "transcript_id",
        "evidence_set_id",
        "quality_report_id",
        "manifest_digest",
        "display_asset_prefix",
        "duplicate_display_asset",
        "usage_bool",
        "quality_type",
        "publish_eligible_type",
        "warning_type",
        "binding_connector",
        "binding_canonical_identity",
    ),
)
def test_commit_video_result_atomic_rejects_malformed_semantics_before_callback(
    repo: SqliteJobRepository,
    malformation: str,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A, principal="local", client_request_id=None
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "commit").attempt_id, authority
    )
    plan, binding, receipt = _video_commit_inputs(job.job_id)
    wrong_uuid = "00000000-0000-4000-8000-000000000000"
    if malformation == "job_id":
        plan = replace(plan, job_id=f"job_{wrong_uuid}")
    elif malformation == "run_id":
        plan = replace(plan, run_id=f"run_{wrong_uuid}")
    elif malformation == "bundle_id":
        malformed = f"bnd_{wrong_uuid}"
        plan = replace(plan, bundle_id=malformed)
        binding = replace(binding, owning_bundle_id=malformed)
        receipt = replace(receipt, bundle_id=malformed)
    elif malformation == "source_id":
        malformed = f"src_{wrong_uuid}"
        plan = replace(plan, source_id=malformed)
        binding = replace(binding, source_id=malformed)
    elif malformation == "revision_id":
        plan = replace(plan, source_revision_id=f"rev_{wrong_uuid}")
    elif malformation in {
        "primary_draft_id",
        "transcript_id",
        "evidence_set_id",
        "quality_report_id",
    }:
        field_by_case = {
            "primary_draft_id": "primary_draft_artifact_id",
            "transcript_id": "transcript_artifact_id",
            "evidence_set_id": "evidence_set_artifact_id",
            "quality_report_id": "quality_report_artifact_id",
        }
        plan = replace(plan, **{field_by_case[malformation]: f"art_{wrong_uuid}"})
    elif malformation == "manifest_digest":
        malformed = "sha256:" + "A" * 64
        plan = replace(plan, manifest_sha256=malformed)
        binding = replace(binding, manifest_sha256=malformed)
        receipt = replace(receipt, manifest_sha256=malformed)
    elif malformation == "display_asset_prefix":
        plan = replace(plan, display_asset_ids=(REVISION_ID,))
    elif malformation == "duplicate_display_asset":
        plan = replace(
            plan, display_asset_ids=(DISPLAY_ASSET_ID, DISPLAY_ASSET_ID)
        )
    elif malformation == "usage_bool":
        plan = replace(plan, usage={"input_tokens": True})
    elif malformation == "quality_type":
        plan = replace(plan, quality_overall="pass")  # type: ignore[arg-type]
    elif malformation == "publish_eligible_type":
        plan = replace(plan, publish_eligible=1)  # type: ignore[arg-type]
    elif malformation == "warning_type":
        plan = replace(plan, warnings=(1,))  # type: ignore[arg-type]
    elif malformation == "binding_connector":
        binding = replace(binding, connector_id="")
    else:
        binding = replace(binding, canonical_identity=1)  # type: ignore[arg-type]
    callback_calls = 0

    def commit() -> PortableCommitReceipt:
        nonlocal callback_calls
        callback_calls += 1
        return receipt

    with pytest.raises(DomainError, match="job_result_plan_invalid"):
        repo.commit_video_result_atomic(
            job.job_id,
            attempt.attempt_id,
            authority,
            result_plan=plan,
            source_identity=binding,
            commit=commit,
        )

    assert callback_calls == 0
    assert repo.get_job(job.job_id).state is JobState.RUNNING
    assert repo.get_job_result(job.job_id) is None


def test_job_request_round_trips_and_direct_repository_call_defaults_none(
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "request-store"
    writer = SqliteJobRepository.open(machine_root)
    request_json = '{"input":"fixture://course"}'
    persisted = writer.create_job(
        request_hash=sha256_digest(request_json),
        request_json=request_json,
        principal="local",
        client_request_id="persisted",
    )
    direct = writer.create_job(
        request_hash=HASH_B,
        principal="local",
        client_request_id="direct",
    )

    reader = SqliteJobRepository.open(machine_root)

    assert reader.get_job_request(persisted.job_id) == '{"input":"fixture://course"}'
    assert reader.get_job_request(direct.job_id) is None


def test_repository_rejects_noncanonical_or_secret_request_json(
    repo: SqliteJobRepository,
) -> None:
    for request_json in (
        '{"b":2, "a":1}',
        '{"api_key":"never-persist"}',
    ):
        with pytest.raises(DomainError, match="job_request_json_invalid"):
            repo.create_job(
                request_hash=sha256_digest(request_json),
                request_json=request_json,
                principal="local",
                client_request_id=None,
            )

    assert b"never-persist" not in repo.database_path.read_bytes()


@pytest.mark.parametrize(
    "secret_field",
    (
        "X-Auth-Token",
        "clientSecret",
        "aws_secret_access_key",
        "apiToken",
        "privateKey",
        "bearerToken",
    ),
)
def test_repository_rejects_secret_aliases_from_request_and_error_without_leak(
    repo: SqliteJobRepository,
    secret_field: str,
) -> None:
    secret = f"never-persist-{secret_field}"
    request_json = json.dumps(
        {secret_field: secret},
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(DomainError) as request_error:
        repo.create_job(
            request_hash=sha256_digest(request_json),
            request_json=request_json,
            principal="local",
            client_request_id=None,
        )

    job = repo.create_job(
        request_hash=HASH_A, principal="local", client_request_id=None
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    with pytest.raises(DomainError) as failure_error:
        repo.fail_job_atomic(
            job.job_id,
            ErrorDetail(
                "unsafe_error",
                ErrorCategory.INTERNAL,
                "Unsafe detail rejected",
                {secret_field: secret},
            ),
        )

    assert request_error.value.code == "job_request_json_invalid"
    assert failure_error.value.code == "job_error_invalid"
    for caught in (request_error, failure_error):
        assert secret_field not in str(caught.value)
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)
    assert secret.encode() not in repo.database_path.read_bytes()


def test_repository_allows_monkey_identifier_in_request_and_error(
    repo: SqliteJobRepository,
) -> None:
    request_json = '{"monkey":"banana"}'
    job = repo.create_job(
        request_hash=sha256_digest(request_json),
        request_json=request_json,
        principal="local",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    error = ErrorDetail(
        "monkey_error",
        ErrorCategory.INTERNAL,
        "Monkey remained safe",
        {"monkey": "banana"},
    )

    repo.fail_job_atomic(job.job_id, error)

    assert repo.get_job_request(job.job_id) == request_json
    assert repo.get_job_error(job.job_id) == error


def test_failed_error_round_trips_after_repository_reopen(tmp_path: Path) -> None:
    machine_root = tmp_path / "error-store"
    writer = SqliteJobRepository.open(machine_root)
    job = writer.create_job(
        request_hash=HASH_A, principal="local", client_request_id=None
    )
    writer.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(writer)
    attempt = writer.start_attempt(
        writer.create_attempt(job.job_id, "preflight").attempt_id, authority
    )
    error = ErrorDetail(
        "preflight_workspace_failed",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Workspace preflight failed",
        {"check": "portable_contract"},
    )

    failed = writer.fail_job_atomic(
        job.job_id,
        error,
        attempt_id=attempt.attempt_id,
        authority=authority,
    )
    reader = SqliteJobRepository.open(machine_root)

    assert failed.state is JobState.FAILED
    assert reader.get_job_error(job.job_id) == error


def test_atomic_failure_rejects_secret_details_without_mutation(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A, principal="local", client_request_id=None
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    secret = "never-persist-error-secret"

    with pytest.raises(DomainError, match="job_error_invalid"):
        repo.fail_job_atomic(
            job.job_id,
            ErrorDetail(
                "unsafe_error",
                ErrorCategory.INTERNAL,
                "Unsafe detail rejected",
                {"api_key": secret},
            ),
        )

    assert repo.get_job(job.job_id).state is JobState.RUNNING
    assert secret.encode() not in repo.database_path.read_bytes()


def test_take_over_running_attempt_requires_new_expired_lease_fence(
    tmp_path: Path,
) -> None:
    now_ms = 1_000
    repo = SqliteJobRepository.open(
        tmp_path / "takeover-store", clock=lambda: now_ms
    )
    job = repo.create_job(
        request_hash=HASH_A, principal="local", client_request_id=None
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    old_authority = repo.acquire_scheduler_lease("old-owner", ttl_seconds=1)
    old_attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "commit").attempt_id, old_authority
    )

    with pytest.raises(DomainError, match="scheduler_busy"):
        repo.acquire_scheduler_lease("new-owner", ttl_seconds=1)
    with pytest.raises(DomainError, match="attempt_takeover_not_fenced"):
        repo.take_over_running_attempt(
            job.job_id, old_attempt.attempt_id, old_authority
        )

    now_ms = 2_001
    new_authority = repo.acquire_scheduler_lease("new-owner", ttl_seconds=1)
    replacement = repo.take_over_running_attempt(
        job.job_id, old_attempt.attempt_id, new_authority
    )

    assert replacement.attempt_id != old_attempt.attempt_id
    assert replacement.step_id == old_attempt.step_id
    assert replacement.state is AttemptState.RUNNING
    assert replacement.fencing_token == new_authority.fencing_token
    with repo._connect() as connection:
        old_state = connection.execute(
            "SELECT state FROM attempts WHERE attempt_id = ?",
            (old_attempt.attempt_id,),
        ).fetchone()[0]
    assert old_state == AttemptState.INTERRUPTED.value
    with pytest.raises(DomainError, match="attempt_fenced"):
        repo.transition_attempt(
            replacement.attempt_id,
            AttemptState.SUCCEEDED,
            authority=old_authority,
        )


def test_pause_for_external_outcome_is_atomic_and_fenced(tmp_path: Path) -> None:
    now_ms = 1_000
    repo = SqliteJobRepository.open(tmp_path / "pause-store", clock=lambda: now_ms)
    job = repo.create_job(request_hash=HASH_A, principal="local", client_request_id=None)
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = repo.acquire_scheduler_lease("current-owner", ttl_seconds=1)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "generate_draft").attempt_id,
        authority,
    )
    operation = repo.prepare_external_operation(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="fixture/provider-v1",
        request_hash=HASH_A,
        operation_idempotency_key=None,
        summary_json="{}",
        authority=authority,
    )
    repo.start_external_operation(operation.operation_id, authority)
    repo.mark_external_operation_unknown(
        operation.operation_id,
        summary_json="{}",
        authority=authority,
    )

    now_ms = 2_001
    current_authority = repo.acquire_scheduler_lease("next-owner", ttl_seconds=1)
    replacement = repo.take_over_running_attempt(
        job.job_id, attempt.attempt_id, current_authority
    )
    with pytest.raises(DomainError, match="attempt_fenced"):
        repo.pause_for_external_outcome_atomic(
            job.job_id,
            attempt.attempt_id,
            authority,
        )
    running_job, active_attempt, no_challenge = repo.get_job_details(job.job_id)
    assert running_job.state is JobState.RUNNING
    assert active_attempt == replacement
    assert no_challenge is None

    challenge = repo.pause_for_external_outcome_atomic(
        job.job_id,
        replacement.attempt_id,
        current_authority,
    )

    paused_job, _, pending = repo.get_job_details(job.job_id)
    assert paused_job.state is JobState.WAITING_FOR_INPUT
    assert pending == challenge
    assert json.loads(challenge.prompt_json) == {
        "code": "external_outcome_unknown",
        "operation_ids": [operation.operation_id],
    }
    with repo._connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE attempt_id = ?", (replacement.attempt_id,)
        ).fetchone()[0]
    assert state == AttemptState.NEEDS_INPUT.value


def test_pause_for_external_outcome_rolls_back_without_unknown_operation(
    tmp_path: Path,
) -> None:
    repo = SqliteJobRepository.open(tmp_path / "pause-rollback-store")
    job = repo.create_job(request_hash=HASH_A, principal="local", client_request_id=None)
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.start_attempt(
        repo.create_attempt(job.job_id, "generate_draft").attempt_id,
        authority,
    )

    with pytest.raises(DomainError, match="external_outcome_unknown_required"):
        repo.pause_for_external_outcome_atomic(
            job.job_id,
            attempt.attempt_id,
            authority,
        )

    current_job, active_attempt, challenge = repo.get_job_details(job.job_id)
    assert current_job.state is JobState.RUNNING
    assert active_attempt == attempt
    assert challenge is None


def _create_needs_input_attempt(repo: SqliteJobRepository):
    job = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id="challenge-original",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.create_attempt(job.job_id, "acquire")
    attempt = repo.start_attempt(attempt.attempt_id, authority)
    attempt = repo.transition_attempt(
        attempt.attempt_id,
        AttemptState.NEEDS_INPUT,
        authority=authority,
    )
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
    authority = _authority(repo)
    attempt = repo.create_attempt(job.job_id, "model")
    attempt = repo.start_attempt(attempt.attempt_id, authority)
    for operation_id in operation_ids:
        _record_unknown_operation(
            repo,
            job_id=job.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            operation_id=operation_id,
        )
    repo.transition_attempt(
        attempt.attempt_id, AttemptState.FAILED, authority=authority
    )
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
    terminal = _terminalize_job(repo, job.job_id, terminal_state)

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


def test_cancel_cancels_pending_attempt_and_persists_request(
    repo: SqliteJobRepository,
) -> None:
    job = repo.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id=None,
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    pending = repo.create_attempt(job.job_id, "pending-step")

    cancelled = repo.cancel_job(job.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancellation_requested is True
    with pytest.raises(DomainError, match="attempt_terminal"):
        repo.start_attempt(pending.attempt_id, _authority(repo))


def test_cancel_waiting_job_cancels_pending_challenge(
    repo: SqliteJobRepository,
) -> None:
    job, attempt = _create_needs_input_attempt(repo)
    challenge = repo.create_challenge(job.job_id, attempt.attempt_id, "{}")

    cancelled = repo.cancel_job(job.job_id)
    with sqlite3.connect(repo.database_path) as connection:
        persisted = connection.execute(
            "SELECT state FROM challenges WHERE challenge_id = ?",
            (challenge.challenge_id,),
        ).fetchone()

    assert cancelled.state is JobState.CANCELLED
    assert persisted[0] == "cancelled"
    assert repo.get_job_details(job.job_id)[2] is None

    with sqlite3.connect(repo.database_path) as connection:
        connection.execute(
            "UPDATE challenges SET state = 'pending' WHERE challenge_id = ?",
            (challenge.challenge_id,),
        )
    assert repo.cancel_job(job.job_id).state is JobState.CANCELLED
    assert repo.get_job_details(job.job_id)[2] is None


def test_running_job_cancellation_is_durable_across_real_repository_reopen(
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "machine-root"
    writer = SqliteJobRepository.open(machine_root)
    job = writer.create_job(
        request_hash=HASH_A,
        principal="agent",
        client_request_id=None,
    )
    writer.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(writer)
    attempt = writer.create_attempt(job.job_id, "running-step")
    attempt = writer.start_attempt(attempt.attempt_id, authority)
    requested = writer.cancel_job(job.job_id)
    assert requested.state is JobState.RUNNING

    reopened = SqliteJobRepository.open(machine_root)
    token = CancellationToken(reopened, job.job_id)

    assert token.is_cancelled() is True
    with pytest.raises(DomainError, match="job_cancelled"):
        token.raise_if_cancelled()
    persisted = reopened.get_job(job.job_id)
    assert persisted.state is JobState.RUNNING
    assert persisted.cancellation_requested is True
    reopened.transition_attempt(
        attempt.attempt_id,
        AttemptState.CANCELLED,
        authority=authority,
    )
    assert reopened.get_job(job.job_id).state is JobState.CANCELLED


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
