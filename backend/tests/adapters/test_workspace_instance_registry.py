from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

import app.adapters.jobs.workspace_instance_registry as registry_module
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.core.errors import DomainError


WORKSPACE_IDENTITY = "workspace-lineage-1"
VALID_INSTANCE_ID = "1" * 32


def _inspect_workspace(workspace_root: Path) -> str:
    return (workspace_root / "workspace-id.txt").read_text(encoding="utf-8").strip()


def _resolve_in_process(local_app_data: str, workspace_root: str) -> str:
    registry = WorkspaceInstanceRegistry(
        Path(local_app_data), inspect_workspace=_inspect_workspace
    )
    return registry.resolve(Path(workspace_root)).instance_id


def _registry_path(local_app_data: Path) -> Path:
    path = local_app_data / "AllToNote" / "workspace-instances.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _valid_entry(root: Path, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "instance_id": VALID_INSTANCE_ID,
        "workspace_identity": "stored-workspace",
        "canonical_root": _canonical(root),
    }
    entry.update(overrides)
    return entry


def _write_registry(local_app_data: Path, payload: object) -> None:
    _registry_path(local_app_data).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        command = [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(target),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"directory junction creation unavailable: {result.stderr}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")


def _remove_directory_link(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


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


def test_registry_and_machine_state_are_never_written_inside_portable_workspace(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
) -> None:
    before = {
        path.relative_to(workspace_root): path.read_bytes()
        for path in workspace_root.rglob("*")
        if path.is_file()
    }

    instance = instance_registry.resolve(workspace_root)

    after = {
        path.relative_to(workspace_root): path.read_bytes()
        for path in workspace_root.rglob("*")
        if path.is_file()
    }
    machine_parent = (local_app_data / "AllToNote" / "workspaces").resolve(
        strict=False
    )
    assert after == before
    assert instance.machine_root.resolve(strict=False).parent == machine_parent
    assert not instance.machine_root.is_relative_to(workspace_root)
    assert not _registry_path(local_app_data).is_relative_to(workspace_root)


@pytest.mark.parametrize("escape_component", ("AllToNote", "workspaces"))
def test_registry_rejects_physical_root_escape_before_writing_artifacts(
    workspace_root: Path,
    tmp_path: Path,
    escape_component: str,
) -> None:
    local_app_data = tmp_path / "local-app-data"
    local_app_data.mkdir()
    escaped_target = tmp_path / "escaped-target"
    escaped_target.mkdir()
    if escape_component == "AllToNote":
        link = local_app_data / "AllToNote"
    else:
        app_root = local_app_data / "AllToNote"
        app_root.mkdir()
        link = app_root / "workspaces"
    _create_directory_link(link, escaped_target)

    try:
        registry = WorkspaceInstanceRegistry(
            local_app_data, inspect_workspace=_inspect_workspace
        )
        with pytest.raises(DomainError, match="workspace_instance_root_unsafe"):
            registry.resolve(workspace_root)

        for artifact_name in (
            "workspace-instances.json",
            "workspace-instances.json.lock",
            "jobs.sqlite",
        ):
            assert list(tmp_path.rglob(artifact_name)) == []
    finally:
        _remove_directory_link(link)


def test_constructor_supplied_local_root_redirection_is_trusted(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    trusted_physical_root = tmp_path / "trusted-physical-root"
    trusted_physical_root.mkdir()
    supplied_root = tmp_path / "redirected-local-root"
    _create_directory_link(supplied_root, trusted_physical_root)

    try:
        instance = WorkspaceInstanceRegistry(
            supplied_root, inspect_workspace=_inspect_workspace
        ).resolve(workspace_root)

        assert (
            trusted_physical_root / "AllToNote" / "workspace-instances.json"
        ).is_file()
        assert instance.machine_root.is_relative_to(trusted_physical_root)
    finally:
        _remove_directory_link(supplied_root)


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


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {},
        {"version": 1},
        {"instances": []},
        {"version": 1, "instances": [], "extra": True},
        {"version": True, "instances": []},
        {"version": "1", "instances": []},
        {"version": 1, "instances": {}},
    ),
)
def test_registry_rejects_invalid_top_level_shape(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    payload: object,
) -> None:
    _write_registry(local_app_data, payload)

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)


def test_registry_rejects_missing_or_extra_entry_fields(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
) -> None:
    valid = _valid_entry(tmp_path / "stale")
    missing = dict(valid)
    missing.pop("workspace_identity")
    extra = {**valid, "extra": "value"}

    for entry in (missing, extra, "not-an-object"):
        _write_registry(local_app_data, {"version": 1, "instances": [entry]})
        with pytest.raises(
            ValueError, match="^workspace_instance_registry_invalid$"
        ):
            instance_registry.resolve(workspace_root)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("instance_id", ""),
        ("instance_id", 1),
        ("workspace_identity", ""),
        ("workspace_identity", True),
        ("canonical_root", ""),
        ("canonical_root", None),
    ),
)
def test_registry_rejects_nonempty_exact_string_field_violations(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    entry = _valid_entry(tmp_path / "stale", **{field: value})
    _write_registry(local_app_data, {"version": 1, "instances": [entry]})

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)


@pytest.mark.parametrize(
    "field", ("instance_id", "workspace_identity", "canonical_root")
)
def test_registry_rejects_string_subclass_values(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    class StringSubclass(str):
        pass

    _registry_path(local_app_data).write_text("{}", encoding="utf-8")
    entry = _valid_entry(tmp_path / "stale")
    entry[field] = StringSubclass(entry[field])
    payload = {"version": 1, "instances": [entry]}
    monkeypatch.setattr(registry_module.json, "load", lambda _stream: payload)

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)


@pytest.mark.parametrize(
    "instance_id",
    (
        "a" * 31,
        "g" * 32,
        "A" * 32,
        "../escape",
        "..\\escape",
        "C:\\escape",
        "/absolute/escape",
    ),
)
def test_registry_rejects_invalid_or_path_like_instance_ids(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
    instance_id: str,
) -> None:
    entry = _valid_entry(tmp_path / "stale", instance_id=instance_id)
    _write_registry(local_app_data, {"version": 1, "instances": [entry]})

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)


def test_registry_rejects_relative_or_noncanonical_roots(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
) -> None:
    absolute = _canonical(tmp_path / "stale")
    noncanonical = str(Path(absolute) / "child" / "..")

    for canonical_root in ("relative/workspace", noncanonical):
        entry = _valid_entry(tmp_path / "stale", canonical_root=canonical_root)
        _write_registry(local_app_data, {"version": 1, "instances": [entry]})
        with pytest.raises(
            ValueError, match="^workspace_instance_registry_invalid$"
        ):
            instance_registry.resolve(workspace_root)


def test_registry_allows_a_canonical_stale_root(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
) -> None:
    stale_root = tmp_path / "removed-workspace"
    assert not stale_root.exists()
    _write_registry(
        local_app_data,
        {"version": 1, "instances": [_valid_entry(stale_root)]},
    )

    instance_registry.resolve(workspace_root)

    payload = json.loads(_registry_path(local_app_data).read_text(encoding="utf-8"))
    assert len(payload["instances"]) == 2


def test_registry_rejects_duplicate_identity_root_keys(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
) -> None:
    first = _valid_entry(tmp_path / "stale")
    duplicate = {**first, "instance_id": "2" * 32}
    _write_registry(
        local_app_data,
        {"version": 1, "instances": [first, duplicate]},
    )

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)


def test_registry_rejects_duplicate_instance_ids(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    tmp_path: Path,
) -> None:
    first = _valid_entry(tmp_path / "stale")
    duplicate = _valid_entry(
        tmp_path / "other-stale", workspace_identity="other-workspace"
    )
    _write_registry(
        local_app_data,
        {"version": 1, "instances": [first, duplicate]},
    )

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)


@pytest.mark.parametrize("raw", (b"{not-json", b'\xff{"version":1}'))
def test_registry_wraps_json_and_unicode_parse_failures(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
    raw: bytes,
) -> None:
    _registry_path(local_app_data).write_bytes(raw)

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)


def test_registry_wraps_file_read_failures(
    instance_registry: WorkspaceInstanceRegistry,
    workspace_root: Path,
    local_app_data: Path,
) -> None:
    _registry_path(local_app_data).mkdir()

    with pytest.raises(ValueError, match="^workspace_instance_registry_invalid$"):
        instance_registry.resolve(workspace_root)
