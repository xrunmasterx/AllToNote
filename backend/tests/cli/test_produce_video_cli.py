from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path

import pytest

from app.cli.main import main


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
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


@pytest.fixture
def runtime_factory(tmp_path: Path):
    created = 0

    def create(**options: object):
        nonlocal created
        created += 1
        runtime_module = importlib.import_module("app.runtime")
        calls = runtime_module.FakeCallCounts()
        runtime = runtime_module.create_fake_runtime(
            tmp_path / f"machine-{created}",
            calls=calls,
            **options,
        )
        return runtime, calls

    return create


def _run_fake_cli(runtime: object, workspace_root: Path) -> int:
    return main(
        [
            "produce",
            "video",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--wait",
            "--json",
        ],
        runtime=runtime,
    )


def test_produce_video_json_is_one_stdout_envelope(
    runtime_factory,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, calls = runtime_factory()

    code = _run_fake_cli(runtime, workspace_root)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 0
    assert envelope["alltonote_cli_protocol_version"] == 1
    assert envelope["ok"] is True
    assert envelope["command"] == "produce video"
    assert envelope["correlation_id"].startswith("corr_")
    assert envelope["data"]["state"] == "succeeded"
    assert envelope["data"]["bundle_id"].startswith("bnd_")
    assert envelope["data"]["workspace_relative_bundle_path"].startswith(
        "raw/personal/bundles/"
    )
    assert envelope["data"]["primary_draft_artifact_id"].startswith("art_")
    assert envelope["warnings"] == []
    assert calls.commit == 1
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_quality_fail_cli_is_successful_and_not_publish_eligible(
    runtime_factory,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, _ = runtime_factory(quality_fail=True)

    code = _run_fake_cli(runtime, workspace_root)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 0
    assert envelope["ok"] is True
    assert envelope["data"]["state"] == "succeeded"
    assert envelope["data"]["quality"] == {
        "overall": "fail",
        "publish_eligible": False,
    }
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_preflight_failure_is_one_safe_envelope_and_nonzero_exit(
    runtime_factory,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, calls = runtime_factory(preflight_failure="workspace_contract")

    code = _run_fake_cli(runtime, workspace_root)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 10
    assert envelope["ok"] is False
    assert envelope["command"] == "produce video"
    assert envelope["error"]["code"] == "preflight_workspace_contract_failed"
    assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_invalid_video_request_is_one_safe_envelope(
    runtime_factory,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, calls = runtime_factory()

    code = main(
        [
            "produce",
            "video",
            "",
            "--workspace",
            str(workspace_root),
            "--wait",
            "--json",
        ],
        runtime=runtime,
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "video_produce_request_invalid"
    assert calls.download == calls.transcribe == calls.model == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
