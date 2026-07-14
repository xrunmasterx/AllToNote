from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.core.errors import DomainError


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
