from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from iwiki.workspace import open_workspace

import app.runtime_paths as runtime_paths_module
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.cli.main import main
from app.core.domain.video import VideoProduceRequest
from app.core.errors import DomainError
from app.core.recipes.contracts import InputDescriptor, ProduceRequest
from app.core.recipes.video.descriptor import VIDEO_COURSE_NOTE_V1
from app.runtime import create_fake_runtime, create_fake_runtime_for_workspace
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


def _workspace(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURE_ROOT, root)
    shutil.rmtree(root / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def _registry(paths: RuntimePaths) -> WorkspaceInstanceRegistry:
    paths.workspace_registry_parent.mkdir(parents=True, exist_ok=True)
    return WorkspaceInstanceRegistry(
        paths.workspace_registry_parent,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )


def _workspace_files(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("config_suffix", "data_suffix", "cache_suffix", "state_suffix", "log_suffix"),
    (
        (
            "AppData/Local/AllToNote",
            "AppData/Local/AllToNote",
            "AppData/Local/AllToNote/Cache",
            "AppData/Local/AllToNote",
            "AppData/Local/AllToNote/Logs",
        ),
        (
            "Library/Application Support/AllToNote",
            "Library/Application Support/AllToNote",
            "Library/Caches/AllToNote",
            "Library/Application Support/AllToNote",
            "Library/Logs/AllToNote",
        ),
    ),
    ids=("windows", "macos"),
)
def test_platformdirs_roles_are_resolved_without_creating_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_suffix: str,
    data_suffix: str,
    cache_suffix: str,
    state_suffix: str,
    log_suffix: str,
) -> None:
    values = {
        "config": tmp_path / config_suffix,
        "data": tmp_path / data_suffix,
        "cache": tmp_path / cache_suffix,
        "state": tmp_path / state_suffix,
        "log": tmp_path / log_suffix,
    }
    monkeypatch.setattr(
        runtime_paths_module,
        "user_config_path",
        lambda _app, appauthor=False: values["config"],
    )
    monkeypatch.setattr(
        runtime_paths_module,
        "user_data_path",
        lambda _app, appauthor=False: values["data"],
    )
    monkeypatch.setattr(
        runtime_paths_module,
        "user_cache_path",
        lambda _app, appauthor=False: values["cache"],
    )
    monkeypatch.setattr(
        runtime_paths_module,
        "user_state_path",
        lambda _app, appauthor=False: values["state"],
    )
    monkeypatch.setattr(
        runtime_paths_module,
        "user_log_path",
        lambda _app, appauthor=False: values["log"],
    )

    paths = resolve_runtime_paths()

    assert paths.config_dir == values["config"].resolve()
    assert paths.data_dir == values["data"].resolve()
    assert paths.cache_dir == values["cache"].resolve()
    assert paths.state_dir == (values["state"] / "State").resolve()
    assert paths.log_dir == values["log"].resolve()
    assert not paths.data_dir.exists()


def test_machine_state_override_supports_chinese_and_spaces_without_io(
    tmp_path: Path,
) -> None:
    root = tmp_path / "机器 状态" / "本地 数据"

    paths = resolve_runtime_paths(machine_state_root=root)

    assert [record["role"] for record in paths.role_records()] == [
        "config",
        "data",
        "cache",
        "state",
        "log",
    ]
    assert paths.data_dir == (root / "data" / "AllToNote").resolve()
    assert paths.workspace_registry_parent == (root / "data").resolve()
    assert not root.exists()


def test_runtime_roots_overlapping_a_workspace_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "Vault 只读 中文")
    paths = resolve_runtime_paths(machine_state_root=workspace / "machine-state")

    with pytest.raises(DomainError, match="runtime_state_inside_workspace"):
        paths.assert_outside_workspace(workspace)


def test_workspace_runtime_factory_rejects_overlap_before_writing(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "Vault overlap gate")
    blocked_root = workspace / "machine-state"

    with pytest.raises(DomainError, match="runtime_state_inside_workspace"):
        create_fake_runtime_for_workspace(
            workspace,
            local_app_data=blocked_root,
        )

    assert not blocked_root.exists()


def test_read_only_compatible_workspace_gets_no_machine_state_files(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "只读 Vault 有 空格")
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "机器 状态")
    before = _workspace_files(workspace)

    instance = _registry(paths).resolve(workspace)

    assert _workspace_files(workspace) == before
    assert instance.machine_root.is_relative_to(paths.workspace_machine_parent)
    assert not tuple(workspace.rglob("jobs.sqlite"))
    assert not tuple(workspace.rglob("workspace-instances.json"))


def test_generic_and_legacy_runtime_submit_share_identity_after_reopen(
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "generic-runtime-machine"
    workspace = _workspace(tmp_path, "Vault generic runtime")
    first = create_fake_runtime(machine_root)
    generic = ProduceRequest(
        1,
        VIDEO_COURSE_NOTE_V1.key,
        InputDescriptor("source", "fixture://course"),
        str(workspace),
        ("knowledge-note",),
        client_request_id="runtime-generic-identity",
    )
    legacy = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace,
        input_value="fixture://course",
        client_request_id="runtime-generic-identity",
    )

    generic_submission = first.submit(generic)
    legacy_snapshot = first.submit_video(legacy)
    reopened = create_fake_runtime(machine_root)

    assert generic_submission.job_id == legacy_snapshot.job_id
    assert reopened.get_job(generic_submission.job_id).job_id == generic_submission.job_id
    assert reopened.wait_job(generic_submission.job_id).result is not None


def test_copied_workspace_does_not_copy_a_live_job(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "Vault 原始")
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "machine-state")
    registry = _registry(paths)
    first_instance = registry.resolve(workspace)
    first_runtime = create_fake_runtime(
        first_instance.machine_root,
        local_instance_id=first_instance.instance_id,
    )
    submitted = first_runtime.submit_video(
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=workspace,
            input_value="fixture://course",
            client_request_id="runtime-path-copy",
        )
    )
    copied = tmp_path / "Vault 副本"
    shutil.copytree(workspace, copied)

    copied_instance = registry.resolve(copied)
    copied_runtime = create_fake_runtime(
        copied_instance.machine_root,
        local_instance_id=copied_instance.instance_id,
    )

    assert copied_instance.instance_id != first_instance.instance_id
    assert first_runtime.get_job(submitted.job_id).job_id == submitted.job_id
    with pytest.raises(DomainError, match="job_not_found"):
        copied_runtime.get_job(submitted.job_id)


def test_deleting_machine_cache_does_not_change_job_or_bundle(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "Vault cache gate")
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "machine-state")
    instance = _registry(paths).resolve(workspace)
    runtime = create_fake_runtime(
        instance.machine_root,
        local_instance_id=instance.instance_id,
    )
    submitted = runtime.submit_video(
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=workspace,
            input_value="fixture://course",
            client_request_id="runtime-cache-delete",
        )
    )
    completed = runtime.wait_job(submitted.job_id)
    assert completed.result is not None
    bundle = workspace / completed.result.workspace_relative_bundle_path
    bundle_before = _workspace_files(bundle)
    cache_marker = paths.cache_dir / "downloads" / "discardable.tmp"
    cache_marker.parent.mkdir(parents=True, exist_ok=True)
    cache_marker.write_text("cache", encoding="utf-8")

    shutil.rmtree(paths.cache_dir)

    assert runtime.get_job(submitted.job_id).result == completed.result
    assert _workspace_files(bundle) == bundle_before


def test_same_workspace_under_two_machine_roots_does_not_share_lease(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "Vault lease gate")
    first_paths = resolve_runtime_paths(machine_state_root=tmp_path / "host-one")
    second_paths = resolve_runtime_paths(machine_state_root=tmp_path / "host-two")
    first_instance = _registry(first_paths).resolve(workspace)
    second_instance = _registry(second_paths).resolve(workspace)
    first_runtime = create_fake_runtime(first_instance.machine_root)
    second_runtime = create_fake_runtime(second_instance.machine_root)

    first_lease = first_runtime.job_repository.acquire_scheduler_lease(
        "owner-one", ttl_seconds=30
    )
    second_lease = second_runtime.job_repository.acquire_scheduler_lease(
        "owner-two", ttl_seconds=30
    )

    assert first_lease.fencing_token == 1
    assert second_lease.fencing_token == 1
    assert first_instance.machine_root != second_instance.machine_root


def test_runtime_paths_json_redacts_by_default_and_requires_explicit_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "用户 名" / "状态")
    monkeypatch.setattr(runtime_paths_module, "resolve_runtime_paths", lambda: paths)

    assert main(["runtime", "paths", "--json"]) == 0
    hidden = json.loads(capsys.readouterr().out)
    assert {record["path"] for record in hidden["data"]["paths"]} == {
        "[PATH_REDACTED]"
    }

    assert main(["runtime", "paths", "--json", "--show-paths"]) == 0
    visible = json.loads(capsys.readouterr().out)
    assert visible["command"] == "runtime paths"
    assert visible["data"]["paths"] == list(paths.role_records())
