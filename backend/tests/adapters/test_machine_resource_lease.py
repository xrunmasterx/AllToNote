from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from app.core.errors import DomainError, ErrorCategory


EXPECTED_RESOURCE_LEASE_SCHEMA = """CREATE TABLE resource_leases (
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


def test_machine_lease_version_zero_empty_database_creates_exact_v1_schema(
    tmp_path: Path,
) -> None:
    MachineResourceLeaseStore, _ = _task7_lease_api()
    machine_root = tmp_path / "machine"
    store = MachineResourceLeaseStore.open(machine_root, clock=_Clock())

    with sqlite3.connect(store.database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()

    assert version == 1
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
        connection.execute("PRAGMA user_version = 2")

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
def test_machine_lease_v1_rejects_non_exact_schema(
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
