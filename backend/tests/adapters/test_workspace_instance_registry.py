from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry


WORKSPACE_IDENTITY = "workspace-lineage-1"


def _inspect_workspace(workspace_root: Path) -> str:
    return (workspace_root / "workspace-id.txt").read_text(encoding="utf-8").strip()


def _resolve_in_process(local_app_data: str, workspace_root: str) -> str:
    registry = WorkspaceInstanceRegistry(
        Path(local_app_data), inspect_workspace=_inspect_workspace
    )
    return registry.resolve(Path(workspace_root)).instance_id


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "portable-workspace"
    root.mkdir()
    (root / "workspace-id.txt").write_text(WORKSPACE_IDENTITY, encoding="utf-8")
    return root


@pytest.fixture
def local_app_data(tmp_path: Path) -> Path:
    root = tmp_path / "local-app-data"
    root.mkdir()
    return root


@pytest.fixture
def instance_registry(local_app_data: Path) -> WorkspaceInstanceRegistry:
    return WorkspaceInstanceRegistry(
        local_app_data, inspect_workspace=_inspect_workspace
    )


def test_same_root_is_stable_but_copied_root_gets_new_local_instance(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    workspace_copy = tmp_path / "portable-workspace-copy"
    workspace_copy.mkdir()
    (workspace_copy / "workspace-id.txt").write_text(
        WORKSPACE_IDENTITY, encoding="utf-8"
    )

    first = instance_registry.resolve(workspace_root)

    assert instance_registry.resolve(workspace_root).instance_id == first.instance_id
    assert instance_registry.resolve(workspace_copy).instance_id != first.instance_id
    assert first.workspace_identity == WORKSPACE_IDENTITY
    assert first.canonical_root == workspace_root.resolve(strict=True)


def test_registry_key_contains_inspected_identity_and_canonical_root(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
) -> None:
    instance = instance_registry.resolve(workspace_root / ".")
    registry_path = local_app_data / "AllToNote" / "workspace-instances.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))

    assert payload == {
        "version": 1,
        "instances": [
            {
                "canonical_root": os.path.normcase(
                    os.path.normpath(str(workspace_root.resolve(strict=True)))
                ),
                "instance_id": instance.instance_id,
                "workspace_identity": WORKSPACE_IDENTITY,
            }
        ],
    }
    assert instance.machine_root == (
        local_app_data / "AllToNote" / "workspaces" / instance.instance_id
    )
    assert list(workspace_root.iterdir()) == [workspace_root / "workspace-id.txt"]


def test_same_root_is_stable_across_processes(
    local_app_data: Path,
    workspace_root: Path,
) -> None:
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        futures = [
            pool.submit(
                _resolve_in_process,
                str(local_app_data),
                str(workspace_root),
            )
            for _ in range(2)
        ]

    assert len({future.result() for future in futures}) == 1


def test_resolve_rejects_a_missing_or_non_directory_root(
    instance_registry: WorkspaceInstanceRegistry,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    file_root = tmp_path / "file"
    file_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="workspace_root_not_directory"):
        instance_registry.resolve(missing)
    with pytest.raises(ValueError, match="workspace_root_not_directory"):
        instance_registry.resolve(file_root)
