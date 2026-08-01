from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from app.adapters.jobs import machine_resource_lease as lease_module
from app.core.errors import DomainError, ErrorCategory


EXPECTED_RESOURCE_LEASE_SCHEMA = """CREATE TABLE resource_leases (
    resource_name TEXT PRIMARY KEY CHECK(length(resource_name) > 0),
    workspace_identity TEXT NOT NULL CHECK(length(workspace_identity) > 0),
    process_instance_id TEXT NOT NULL CHECK(length(process_instance_id) > 0),
    process_id INTEGER CHECK(process_id IS NULL OR process_id > 0),
    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
    expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms >= 0),
    handoff_workspace_identity TEXT,
    handoff_process_instance_id TEXT,
    handoff_process_id INTEGER,
    handoff_nonce TEXT,
    handoff_expires_at_ms INTEGER,
    CHECK (
        (handoff_workspace_identity IS NULL
         AND handoff_process_instance_id IS NULL
         AND handoff_process_id IS NULL
         AND handoff_nonce IS NULL
         AND handoff_expires_at_ms IS NULL)
        OR
        (length(handoff_workspace_identity) > 0
         AND length(handoff_process_instance_id) > 0
         AND (handoff_process_id IS NULL OR handoff_process_id > 0)
         AND length(handoff_nonce) = 43
         AND handoff_expires_at_ms >= 0)
    )
)"""

V1_RESOURCE_LEASE_SCHEMA = """CREATE TABLE resource_leases (
    resource_name TEXT PRIMARY KEY CHECK(length(resource_name) > 0),
    workspace_identity TEXT NOT NULL CHECK(length(workspace_identity) > 0),
    process_instance_id TEXT NOT NULL CHECK(length(process_instance_id) > 0),
    process_id INTEGER CHECK(process_id IS NULL OR process_id > 0),
    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
    expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms >= 0)
)"""


class _Clock:
    def __init__(self) -> None:
        self.now_ms = 2_000_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


def _task7_lease_api():
    adapter = importlib.import_module(
        "app.adapters.jobs.machine_resource_lease"
    )
    core = importlib.import_module("app.core.jobs.resource_lease")
    return adapter.MachineResourceLeaseStore, core.ResourceOwner


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _assert_schema_invalid(machine_root: Path) -> None:
    MachineResourceLeaseStore, _ = _task7_lease_api()
    with pytest.raises(DomainError, match="machine_lease_schema_invalid") as caught:
        MachineResourceLeaseStore.open(machine_root)
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_machine_lease_is_exclusive_across_workspaces_and_uses_separate_db(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    clock = _Clock()
    store = MachineResourceLeaseStore.open(tmp_path / "machine", clock=clock)
    workspace_a = ResourceOwner("workspace-a", "process-a", process_id=123)
    workspace_b = ResourceOwner("workspace-b", "process-b", process_id=123)

    first = store.acquire(
        "transcriber:faster-whisper:gpu", workspace_a, ttl_seconds=30
    )
    with pytest.raises(DomainError, match="resource_busy"):
        store.acquire(
            "transcriber:faster-whisper:gpu", workspace_b, ttl_seconds=30
        )

    assert store.database_path == tmp_path / "machine" / "leases.sqlite"
    assert first.owner == workspace_a
    assert first.release() is True
    second = store.acquire(
        "transcriber:faster-whisper:gpu", workspace_b, ttl_seconds=30
    )
    assert second.fencing_token == first.fencing_token + 1


def test_expired_lease_takeover_fences_heartbeat_and_stale_release(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    clock = _Clock()
    store = MachineResourceLeaseStore.open(tmp_path / "machine", clock=clock)
    first = store.acquire(
        "gpu",
        ResourceOwner("workspace-a", "process-a", process_id=100),
        ttl_seconds=10,
    )
    clock.advance(10_001)
    second = store.acquire(
        "gpu",
        ResourceOwner("workspace-b", "process-b", process_id=100),
        ttl_seconds=10,
    )

    with pytest.raises(DomainError, match="resource_lease_lost"):
        first.heartbeat(ttl_seconds=10)
    assert first.release() is False
    renewed = second.heartbeat(ttl_seconds=20)
    assert renewed.fencing_token == second.fencing_token
    assert renewed.expires_at_ms == clock.now_ms + 20_000


def test_same_owner_cannot_reenter_exclusive_lease_without_reference_count(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    store = MachineResourceLeaseStore.open(tmp_path / "machine", clock=_Clock())
    owner = ResourceOwner("workspace-a", "process-a", process_id=100)
    first = store.acquire("produce:heavy:v1", owner, ttl_seconds=30)

    with pytest.raises(DomainError, match="resource_busy"):
        store.acquire("produce:heavy:v1", owner, ttl_seconds=30)

    assert first.release()
    second = store.acquire("produce:heavy:v1", owner, ttl_seconds=30)
    assert second.fencing_token == first.fencing_token + 1


@pytest.mark.parametrize("ttl_seconds", (0, 301, True))
def test_machine_lease_ttl_is_bounded(
    tmp_path: Path,
    ttl_seconds: object,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    store = MachineResourceLeaseStore.open(tmp_path / "machine", clock=_Clock())

    with pytest.raises(DomainError, match="resource_lease_ttl_invalid"):
        store.acquire(
            "gpu",
            ResourceOwner("workspace-a", "process-a", process_id=100),
            ttl_seconds=ttl_seconds,
        )


@pytest.mark.parametrize(
    "owner_args",
    (("", "process-a"), ("workspace-a", "")),
)
def test_machine_lease_owner_requires_workspace_and_process_instance_identity(
    owner_args: tuple[str, str],
) -> None:
    _, ResourceOwner = _task7_lease_api()

    with pytest.raises(DomainError, match="resource_owner_invalid"):
        ResourceOwner(*owner_args, process_id=123)


def test_machine_lease_version_zero_empty_database_creates_exact_v2_schema(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, _ = _task7_lease_api()
    machine_root = tmp_path / "machine"
    store = MachineResourceLeaseStore.open(machine_root, clock=_Clock())

    with sqlite3.connect(store.database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        objects = connection.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()

    assert version == 2
    assert journal_mode == "wal"
    assert synchronous == 2
    assert len(objects) == 1
    assert objects[0][0:2] == ("table", "resource_leases")
    assert _normalized_sql(objects[0][2]) == _normalized_sql(
        EXPECTED_RESOURCE_LEASE_SCHEMA
    )
    assert MachineResourceLeaseStore.open(machine_root, clock=_Clock()).database_path == (
        store.database_path
    )


def test_machine_lease_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    MachineResourceLeaseStore, _ = _task7_lease_api()
    machine_root = tmp_path / "machine"
    machine_root.mkdir()
    with sqlite3.connect(machine_root / "leases.sqlite") as connection:
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(
        DomainError, match="machine_lease_schema_unsupported"
    ) as caught:
        MachineResourceLeaseStore.open(machine_root)
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_machine_lease_rejects_version_zero_nonempty_without_mutation(
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "machine"
    machine_root.mkdir()
    database_path = machine_root / "leases.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy_secret(value TEXT)")
        connection.execute("INSERT INTO legacy_secret VALUES ('do-not-touch')")
    before = database_path.read_bytes()

    _assert_schema_invalid(machine_root)

    assert database_path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    ("missing", "extra", "wrong_primary_key", "wrong_constraint"),
)
def test_machine_lease_v2_rejects_non_exact_schema(
    tmp_path: Path,
    mutation: str,
) -> None:
    MachineResourceLeaseStore, _ = _task7_lease_api()
    machine_root = tmp_path / mutation
    store = MachineResourceLeaseStore.open(machine_root)
    with sqlite3.connect(store.database_path) as connection:
        if mutation == "missing":
            connection.execute("DROP TABLE resource_leases")
        elif mutation == "extra":
            connection.execute("CREATE TABLE unexpected(value TEXT)")
        elif mutation == "wrong_primary_key":
            connection.execute("DROP TABLE resource_leases")
            connection.execute(
                """
                CREATE TABLE resource_leases (
                    resource_name TEXT NOT NULL,
                    workspace_identity TEXT NOT NULL,
                    process_instance_id TEXT NOT NULL,
                    process_id INTEGER,
                    fencing_token INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (resource_name, workspace_identity)
                )
                """
            )
        else:
            connection.execute("DROP TABLE resource_leases")
            connection.execute(
                """
                CREATE TABLE resource_leases (
                    resource_name TEXT PRIMARY KEY CHECK(length(resource_name) >= 0),
                    workspace_identity TEXT NOT NULL CHECK(length(workspace_identity) > 0),
                    process_instance_id TEXT NOT NULL CHECK(length(process_instance_id) > 0),
                    process_id INTEGER CHECK(process_id IS NULL OR process_id > 0),
                    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
                    expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms >= 0)
                )
                """
            )

    _assert_schema_invalid(machine_root)


def test_machine_lease_migrates_exact_v1_and_preserves_active_lease(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    machine_root = tmp_path / "v1"
    machine_root.mkdir()
    database_path = machine_root / "leases.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(V1_RESOURCE_LEASE_SCHEMA)
        connection.execute(
            """
            INSERT INTO resource_leases (
                resource_name, workspace_identity, process_instance_id,
                process_id, fencing_token, expires_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("produce:heavy:v1", "workspace", "old-owner", 100, 7, 9_999_999),
        )
        connection.execute("PRAGMA user_version = 1")

    store = MachineResourceLeaseStore.open(machine_root, clock=_Clock())

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        row = connection.execute(
            "SELECT * FROM resource_leases WHERE resource_name = ?",
            ("produce:heavy:v1",),
        ).fetchone()
    assert version == 2
    assert row[:6] == (
        "produce:heavy:v1",
        "workspace",
        "old-owner",
        100,
        7,
        9_999_999,
    )
    assert row[6:] == (None, None, None, None, None)
    with pytest.raises(DomainError, match="resource_busy"):
        store.acquire(
            "produce:heavy:v1",
            ResourceOwner("workspace", "new-owner"),
            ttl_seconds=30,
        )


def test_machine_lease_handoff_is_one_use_and_preserves_fencing(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    clock = _Clock()
    store = MachineResourceLeaseStore.open(tmp_path / "handoff", clock=clock)
    supervisor = ResourceOwner("workspace", "engine-supervisor", process_id=10)
    worker = ResourceOwner("workspace", "engine-worker")
    source = store.acquire("produce:heavy:v1", supervisor, ttl_seconds=30)

    handoff = store.handoff(source, worker, ttl_seconds=30)
    adopted = store.adopt(handoff, ttl_seconds=30)

    assert adopted.owner == worker
    assert adopted.fencing_token == source.fencing_token
    assert source.release() is False
    with pytest.raises(DomainError, match="resource_handoff_invalid"):
        store.adopt(handoff, ttl_seconds=30)
    renewed = store.heartbeat_adopted(handoff, ttl_seconds=30)
    assert renewed.owner == worker
    assert renewed.fencing_token == adopted.fencing_token
    assert store.release_adopted(handoff) is True
    assert adopted.release() is False


def test_machine_lease_handoff_expiry_and_wrong_target_fail_closed(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    clock = _Clock()
    store = MachineResourceLeaseStore.open(tmp_path / "handoff-expiry", clock=clock)
    source = store.acquire(
        "produce:heavy:v1",
        ResourceOwner("workspace", "engine-supervisor"),
        ttl_seconds=30,
    )
    handoff = store.handoff(
        source,
        ResourceOwner("workspace", "engine-worker"),
        ttl_seconds=1,
    )
    clock.advance(1_001)

    with pytest.raises(DomainError, match="resource_handoff_invalid"):
        store.adopt(handoff, ttl_seconds=30)
    with pytest.raises(DomainError, match="resource_lease_lost"):
        store.heartbeat_adopted(handoff, ttl_seconds=30)
    assert store.release_adopted(handoff) is False
    assert source.release() is True


def test_machine_lease_corrupt_database_and_runtime_sqlite_errors_are_sanitized(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    marker = "raw-secret-marker"
    corrupt_root = tmp_path / "corrupt-open"
    corrupt_root.mkdir()
    corrupt_path = corrupt_root / "leases.sqlite"
    corrupt_path.write_bytes(marker.encode("utf-8"))
    before = corrupt_path.read_bytes()

    with pytest.raises(DomainError, match="machine_lease_schema_invalid") as caught:
        MachineResourceLeaseStore.open(corrupt_root)
    assert marker not in str(caught.value)
    assert str(corrupt_path) not in str(caught.value)
    assert corrupt_path.read_bytes() == before

    runtime_root = tmp_path / "runtime-corrupt"
    store = MachineResourceLeaseStore.open(runtime_root)
    store.database_path.write_bytes(marker.encode("utf-8"))
    with pytest.raises(DomainError, match="machine_lease_store_invalid") as caught:
        store.acquire(
            "gpu",
            ResourceOwner("workspace", "process", process_id=1),
            ttl_seconds=10,
        )
    assert marker not in str(caught.value)
    assert str(store.database_path) not in str(caught.value)


def test_machine_lease_open_busy_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    MachineResourceLeaseStore, _ = _task7_lease_api()
    monkeypatch.setattr(lease_module, "_BUSY_TIMEOUT_MS", 25)
    machine_root = tmp_path / "busy-open"
    store = MachineResourceLeaseStore.open(machine_root)
    holder = sqlite3.connect(store.database_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DomainError) as raised:
            MachineResourceLeaseStore.open(machine_root)
    finally:
        holder.rollback()
        holder.close()

    assert raised.value.code == "machine_lease_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert dict(raised.value.details) == {
        "sqlite_result": "busy",
        "busy_timeout_ms": 25,
    }
    assert str(store.database_path) not in str(raised.value)


def test_machine_lease_acquire_busy_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    MachineResourceLeaseStore, ResourceOwner = _task7_lease_api()
    monkeypatch.setattr(lease_module, "_BUSY_TIMEOUT_MS", 25)
    store = MachineResourceLeaseStore.open(tmp_path / "busy-acquire")
    holder = sqlite3.connect(store.database_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DomainError) as raised:
            store.acquire(
                "gpu",
                ResourceOwner("workspace", "process", process_id=1),
                ttl_seconds=10,
            )
    finally:
        holder.rollback()
        holder.close()

    assert raised.value.code == "machine_lease_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert dict(raised.value.details) == {
        "sqlite_result": "busy",
        "busy_timeout_ms": 25,
    }


@pytest.mark.parametrize(
    ("result_code", "result_name"),
    (
        (sqlite3.SQLITE_BUSY, "busy"),
        (sqlite3.SQLITE_LOCKED, "locked"),
        (sqlite3.SQLITE_BUSY | (2 << 8), "busy"),
    ),
)
def test_machine_lease_busy_result_codes_are_stable_and_sanitized(
    result_code: int,
    result_name: str,
) -> None:
    error = sqlite3.OperationalError("private SQLite detail")
    error.sqlite_errorcode = result_code

    with pytest.raises(DomainError) as raised:
        lease_module._raise_if_store_busy(error)

    assert raised.value.code == "machine_lease_store_busy"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert dict(raised.value.details) == {
        "sqlite_result": result_name,
        "busy_timeout_ms": lease_module._BUSY_TIMEOUT_MS,
    }
    assert "private" not in str(raised.value)


def test_machine_lease_fails_closed_when_wal_is_unavailable() -> None:
    class FakeResult:
        def fetchone(self):
            return ("delete",)

    class FakeConnection:
        row_factory = None

        def execute(self, statement: str):
            if statement.startswith("PRAGMA busy_timeout"):
                return FakeResult()
            if statement == "PRAGMA journal_mode = WAL":
                return FakeResult()
            pytest.fail(f"unexpected statement after rejected WAL mode: {statement}")

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    with pytest.raises(DomainError) as raised:
        lease_module._configure_connection(FakeConnection())

    assert raised.value.code == "machine_lease_wal_unavailable"
    assert raised.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE
