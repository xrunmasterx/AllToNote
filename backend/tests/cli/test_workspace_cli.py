from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from filelock import Timeout
from iwiki.workspace import open_workspace

from app.cli.main import main
from app.core.config.loader import load_runtime_config
from app.runtime_config import RuntimeConfigService
from app.runtime_paths import resolve_runtime_paths


def _service(tmp_path: Path) -> RuntimeConfigService:
    return RuntimeConfigService(
        paths=resolve_runtime_paths(machine_state_root=tmp_path / "machine-state")
    )


def test_workspace_init_creates_valid_v2_workspace_for_unicode_path(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "知识 Vault with spaces"

    assert (
        main(
            [
                "workspace",
                "init",
                str(root),
                "--name",
                "图形学知识库",
                "--json",
            ],
            config_service=_service(tmp_path),
        )
        == 0
    )

    output = capsys.readouterr()
    envelope = json.loads(output.out)
    workspace = open_workspace(root, writable=True)

    assert output.err == ""
    assert envelope["command"] == "workspace init"
    assert envelope["data"] == {
        "created": True,
        "default_set": False,
        "name": "图形学知识库",
        "schema_version": 2,
        "workspace_id": workspace.manifest.workspace_id,
    }
    assert UUID(workspace.manifest.workspace_id).version == 4
    assert workspace.manifest.name == "图形学知识库"
    assert workspace.manifest.relative_paths == {
        "raw_common": "raw/common",
        "raw_personal": "raw/personal",
        "wiki_common": "wiki/common",
        "wiki_personal": "wiki/personal",
        "cache": ".cache",
    }
    manifest_bytes = (root / ".llm-wiki" / "manifest.yaml").read_bytes()
    assert b"\r" not in manifest_bytes
    assert manifest_bytes.endswith(b"\n")
    assert "图形学知识库".encode("utf-8") in manifest_bytes
    for relative in (
        "raw/common",
        "raw/personal",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        assert (root / relative).is_dir()
    assert not tuple(root.rglob("jobs.sqlite"))
    assert not tuple(root.rglob("workspace-instances.json"))
    assert not (root / ".gitattributes").exists()


def test_workspace_init_is_idempotent_and_preserves_existing_content(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "existing-notes"
    root.mkdir()
    existing = root / "README.md"
    existing.write_text("keep me", encoding="utf-8")
    service = _service(tmp_path)
    arguments = [
        "workspace",
        "init",
        str(root),
        "--name",
        "Existing Notes",
        "--json",
    ]

    assert main(arguments, config_service=service) == 0
    first = json.loads(capsys.readouterr().out)
    manifest = root / ".llm-wiki" / "manifest.yaml"
    manifest_before = manifest.read_bytes()

    assert main(arguments, config_service=service) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert second["data"]["workspace_id"] == first["data"]["workspace_id"]
    assert manifest.read_bytes() == manifest_before
    assert existing.read_text(encoding="utf-8") == "keep me"
    assert not tuple((root / ".llm-wiki").glob("*.tmp"))
    assert not tuple((root / ".llm-wiki").glob("*.lock"))
    assert len(tuple((service.paths.state_dir / "locks").glob("*.lock"))) == 1


def test_workspace_init_serializes_concurrent_identical_initialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "concurrent-workspace"

    def initialize() -> tuple[bool, str]:
        from app.workspace_initializer import initialize_workspace

        result = initialize_workspace(
            root,
            "Concurrent Workspace",
            lock_root=tmp_path / "machine-locks",
        )
        return result.created, result.workspace_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: initialize(), range(2)))

    workspace = open_workspace(root, writable=True)
    assert sorted(created for created, _workspace_id in results) == [False, True]
    assert {workspace_id for _created, workspace_id in results} == {
        workspace.manifest.workspace_id
    }
    assert not tuple((root / ".llm-wiki").glob("*.tmp"))
    assert not tuple((root / ".llm-wiki").glob("*.lock"))


def test_workspace_init_busy_lock_does_not_create_vault(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    from app import workspace_initializer

    root = tmp_path / "busy-workspace"

    def reject_acquire(*_args, **_kwargs) -> None:
        raise Timeout("held")

    monkeypatch.setattr(
        workspace_initializer._WorkspaceFileLock,
        "acquire",
        reject_acquire,
    )

    exit_code = main(
        ["workspace", "init", str(root), "--name", "Busy", "--json"],
        config_service=_service(tmp_path),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 20
    assert envelope["error"]["code"] == "workspace_init_busy"
    assert not root.exists()


def test_workspace_init_does_not_repair_existing_contract(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "missing-contract-directory"
    service = _service(tmp_path)
    arguments = [
        "workspace",
        "init",
        str(root),
        "--name",
        "Existing",
        "--json",
    ]
    assert main(arguments, config_service=service) == 0
    capsys.readouterr()
    manifest = root / ".llm-wiki" / "manifest.yaml"
    before = manifest.read_bytes()
    (root / "raw" / "personal").rmdir()

    exit_code = main(arguments, config_service=service)
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 10
    assert envelope["error"]["code"] == "workspace_invalid"
    assert manifest.read_bytes() == before
    assert not (root / "raw" / "personal").exists()


def test_workspace_init_rejects_dangling_link_before_writing(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "linked-workspace"
    try:
        root.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable on this host")

    exit_code = main(
        ["workspace", "init", str(root), "--name", "Linked", "--json"],
        config_service=_service(tmp_path),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 10
    assert envelope["error"]["code"] == "workspace_path_invalid"
    assert not (tmp_path / "missing-target").exists()


def test_workspace_init_can_set_default_workspace_after_initialization(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "default-workspace"
    service = _service(tmp_path)

    assert (
        main(
            [
                "workspace",
                "init",
                str(root),
                "--name",
                "Default Workspace",
                "--set-default",
                "--json",
            ],
            config_service=service,
        )
        == 0
    )

    envelope = json.loads(capsys.readouterr().out)
    config = load_runtime_config(service.paths.config_file, {})

    assert envelope["data"]["default_set"] is True
    assert config.default_workspace == root.resolve()
    assert not tuple(service.paths.config_dir.glob("*.tmp"))


def test_workspace_init_rejects_a_different_name_without_rewriting_manifest(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "named-workspace"
    service = _service(tmp_path)
    assert (
        main(
            ["workspace", "init", str(root), "--name", "Original", "--json"],
            config_service=service,
        )
        == 0
    )
    capsys.readouterr()
    manifest = root / ".llm-wiki" / "manifest.yaml"
    before = manifest.read_bytes()

    exit_code = main(
        ["workspace", "init", str(root), "--name", "Different", "--json"],
        config_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 20
    assert envelope["error"]["code"] == "workspace_already_initialized"
    assert manifest.read_bytes() == before


def test_workspace_init_fails_before_writing_when_contract_path_is_a_file(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "invalid-workspace"
    root.mkdir()
    (root / "raw").write_text("not a directory", encoding="utf-8")

    exit_code = main(
        ["workspace", "init", str(root), "--name", "Invalid", "--json"],
        config_service=_service(tmp_path),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 10
    assert envelope["error"]["code"] == "workspace_path_invalid"
    assert not (root / ".llm-wiki").exists()


def test_workspace_init_rejects_occupied_control_directory(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "occupied-control"
    control = root / ".llm-wiki"
    control.mkdir(parents=True)
    marker = control / "foreign.txt"
    marker.write_text("do not touch", encoding="utf-8")

    exit_code = main(
        ["workspace", "init", str(root), "--name", "Occupied", "--json"],
        config_service=_service(tmp_path),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 20
    assert envelope["error"]["code"] == "workspace_control_conflict"
    assert marker.read_text(encoding="utf-8") == "do not touch"
    assert not (control / "manifest.yaml").exists()
    assert not tuple(control.glob("*.tmp"))
    assert not tuple(control.glob("*.lock"))


def test_workspace_init_rejects_blank_name_as_cli_error(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "blank-name"

    exit_code = main(
        ["workspace", "init", str(root), "--name", "   ", "--json"],
        config_service=_service(tmp_path),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "workspace_name_invalid"
    assert not root.exists()


def test_workspace_init_parse_error_preserves_command_identity(capsys) -> None:
    exit_code = main(["workspace", "init", "--json"])
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["command"] == "workspace init"
    assert envelope["error"]["code"] == "cli_usage_invalid"


def test_workspace_init_rejects_runtime_state_location_before_writing(
    tmp_path: Path,
    capsys,
) -> None:
    service = _service(tmp_path)
    root = service.paths.data_dir / "invalid-vault"

    exit_code = main(
        ["workspace", "init", str(root), "--name", "Invalid", "--json"],
        config_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 10
    assert envelope["error"]["code"] == "runtime_state_inside_workspace"
    assert not root.exists()
