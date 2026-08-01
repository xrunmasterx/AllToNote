from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.core.errors import DomainError
from app.core.jobs.resource_lease import ResourceOwner
from app.core.jobs.workspace_publish import (
    WorkspacePublishCoordinator,
    workspace_publish_resource_name,
)


def test_publish_resource_name_binds_workspace_identity_and_physical_root(
    tmp_path: Path,
) -> None:
    first = tmp_path / "Workspace 有空格"
    second = tmp_path / "Workspace copy"
    first.mkdir()
    second.mkdir()

    resource_name = workspace_publish_resource_name("workspace-id", first)

    assert resource_name.startswith("workspace:publish:v1:")
    assert resource_name == workspace_publish_resource_name(
        "workspace-id",
        first.resolve(),
    )
    assert resource_name != workspace_publish_resource_name(
        "other-workspace-id",
        first,
    )
    assert resource_name != workspace_publish_resource_name(
        "workspace-id",
        second,
    )
    assert str(first) not in resource_name


def test_publish_coordinator_holds_one_workspace_slot_and_releases_on_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MachineResourceLeaseStore.open(tmp_path / "machine")
    owner = ResourceOwner("workspace-id", "publisher-a", process_id=101)
    competing = ResourceOwner("workspace-id", "publisher-b", process_id=202)
    coordinator = WorkspacePublishCoordinator(
        store,
        owner,
        workspace_root=workspace,
    )

    with pytest.raises(RuntimeError, match="portable commit failed"):
        with coordinator.hold():
            with pytest.raises(DomainError, match="resource_busy"):
                store.acquire(
                    coordinator.resource_name,
                    competing,
                    ttl_seconds=300,
                )
            raise RuntimeError("portable commit failed")

    recovered = store.acquire(
        coordinator.resource_name,
        competing,
        ttl_seconds=300,
    )
    assert recovered.release()
