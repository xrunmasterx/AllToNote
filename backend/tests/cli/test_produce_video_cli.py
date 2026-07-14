from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from types import MappingProxyType

import pytest

from app.cli.main import main
from app.core.domain.video import JobSnapshot, JobState
from app.core.errors import DomainError, ErrorCategory, ErrorDetail


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
    service_module = importlib.import_module("app.core.application.video_service")
    runtime, calls = runtime_factory(
        capabilities=service_module.VideoPreflightCapabilities(
            video_feature_pack=False
        )
    )

    code = _run_fake_cli(runtime, workspace_root)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 10
    assert envelope["ok"] is False
    assert envelope["command"] == "produce video"
    assert envelope["error"]["code"] == "video_feature_pack_unavailable"
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


class _UnexpectedRuntime:
    def submit_video(self, request: object) -> object:
        del request
        raise RuntimeError("provider leaked implementation detail")


class _InterruptedRuntime:
    def submit_video(self, request: object) -> object:
        del request
        raise KeyboardInterrupt


def _unsafe_error_details(secret: str) -> dict[str, object]:
    return {
        "X-Auth-Token": secret,
        "outer": MappingProxyType(
            {
                "clientSecret": secret,
                "visible": "kept",
                "nested": {"api_key": secret, "count": 2},
            }
        ),
        "explicit_list": ["kept", {"ok": True}, secret.encode("utf-8")],
        "binary": secret.encode("utf-8"),
        "set_value": {secret, "unsafe"},
        "frozen_set_value": frozenset({secret, "unsafe"}),
    }


class _ImmediateDomainErrorRuntime:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def submit_video(self, request: object) -> object:
        del request
        raise DomainError(
            "provider_policy_denied",
            ErrorCategory.POLICY_DENIED,
            "Provider policy denied the request",
            _unsafe_error_details(self._secret),
        )


class _PersistedErrorRuntime:
    def __init__(self, secret: str) -> None:
        self._snapshot = JobSnapshot(
            job_id="job_failed",
            state=JobState.FAILED,
            cancellation_requested=False,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=None,
            error=ErrorDetail(
                "recipe_output_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Recipe output was invalid",
                _unsafe_error_details(secret),
            ),
        )

    def submit_video(self, request: object) -> JobSnapshot:
        del request
        return self._snapshot

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del job_id, event_sink
        return self._snapshot


class _CancelledRuntime:
    def __init__(self) -> None:
        self._snapshot = JobSnapshot(
            job_id="job_cancelled",
            state=JobState.CANCELLED,
            cancellation_requested=True,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=None,
            error=None,
        )

    def submit_video(self, request: object) -> JobSnapshot:
        del request
        return self._snapshot

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del job_id, event_sink
        return self._snapshot


def test_cancelled_job_is_a_structured_failure_with_cancelled_exit(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run_fake_cli(_CancelledRuntime(), workspace_root)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 60
    assert envelope == {
        "alltonote_cli_protocol_version": 1,
        "ok": False,
        "command": "produce video",
        "correlation_id": envelope["correlation_id"],
        "data": {"job_id": "job_cancelled", "state": "cancelled"},
        "error": {
            "code": "job_cancelled",
            "category": "cancelled",
            "message": "Job was cancelled",
            "details": {},
        },
        "warnings": [],
    }
    assert envelope["correlation_id"].startswith("corr_")
    assert captured.err == ""
    assert captured.out.count("\n") == 1


@pytest.mark.parametrize(
    ("runtime", "expected_exit", "expected_code", "expected_category"),
    (
        (
            _ImmediateDomainErrorRuntime("immediate-secret-value"),
            40,
            "provider_policy_denied",
            "policy_denied",
        ),
        (
            _PersistedErrorRuntime("persisted-secret-value"),
            50,
            "recipe_output_invalid",
            "recipe_failed",
        ),
    ),
)
def test_error_details_are_redacted_and_always_json_safe(
    runtime: object,
    expected_exit: int,
    expected_code: str,
    expected_category: str,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run_fake_cli(runtime, workspace_root)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    serialized = json.dumps(envelope, ensure_ascii=False)

    assert code == expected_exit
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["category"] == expected_category
    assert envelope["error"]["message"] in {
        "Provider policy denied the request",
        "Recipe output was invalid",
    }
    assert envelope["error"]["details"]["outer"]["visible"] == "kept"
    assert envelope["error"]["details"]["outer"]["nested"]["count"] == 2
    assert envelope["error"]["details"]["explicit_list"] == [
        "kept",
        {"ok": True},
    ]
    assert envelope["error"]["details"]["X-Auth-Token"] == "[REDACTED]"
    assert envelope["error"]["details"]["outer"]["clientSecret"] == "[REDACTED]"
    assert envelope["error"]["details"]["outer"]["nested"]["api_key"] == "[REDACTED]"
    assert "binary" not in envelope["error"]["details"]
    assert "set_value" not in envelope["error"]["details"]
    assert "frozen_set_value" not in envelope["error"]["details"]
    assert "immediate-secret-value" not in serialized
    assert "persisted-secret-value" not in serialized
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_error_detail_projection_failure_falls_back_to_empty_details(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_module = importlib.import_module("app.cli.main")

    def fail_projection(identifier: str) -> bool:
        del identifier
        raise RuntimeError("projection failed")

    monkeypatch.setattr(cli_module, "is_sensitive_identifier", fail_projection)

    code = _run_fake_cli(
        _ImmediateDomainErrorRuntime("fallback-secret-value"), workspace_root
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 40
    assert envelope["error"] == {
        "code": "provider_policy_denied",
        "category": "policy_denied",
        "message": "Provider policy denied the request",
        "details": {},
    }
    assert "fallback-secret-value" not in captured.out
    assert captured.err == ""
    assert captured.out.count("\n") == 1


@pytest.mark.parametrize(
    ("runtime", "expected_code", "error_code"),
    (
        (_UnexpectedRuntime(), 70, "internal_error"),
        (_InterruptedRuntime(), 130, "interrupted"),
    ),
)
def test_unexpected_failures_are_one_safe_json_envelope(
    runtime: object,
    expected_code: int,
    error_code: str,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run_fake_cli(runtime, workspace_root)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == expected_code
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == error_code
    assert "provider leaked" not in captured.out
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_default_runtime_uses_workspace_instance_machine_root(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local_app_data = tmp_path / "local-app-data"
    local_app_data.mkdir()
    user_profile = tmp_path / "user-profile"
    user_profile.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("USERPROFILE", str(user_profile))

    code = main(
        [
            "produce",
            "video",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--wait",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert json.loads(captured.out)["data"]["state"] == "succeeded"
    registry_path = local_app_data / "AllToNote" / "workspace-instances.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    instance_id = registry["instances"][0]["instance_id"]
    machine_root = local_app_data / "AllToNote" / "workspaces" / instance_id
    assert (machine_root / "job-store" / "jobs.sqlite").is_file()
    assert not (user_profile / ".alltonote" / "runtime").exists()

    second_code = main(
        [
            "produce",
            "video",
            "fixture://second-course",
            "--workspace",
            str(workspace_root),
            "--wait",
            "--json",
        ]
    )
    second = json.loads(capsys.readouterr().out)

    assert second_code == 0
    assert second["data"]["state"] == "succeeded"
