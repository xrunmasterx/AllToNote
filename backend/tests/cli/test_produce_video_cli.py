from __future__ import annotations

import importlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from app.cli.main import main
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    JobSnapshot,
    JobState,
    VideoDocumentKind,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission


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
            "--input",
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


def test_human_mode_uses_the_same_result_without_json_protocol_noise(
    runtime_factory,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, calls = runtime_factory()

    code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--wait",
        ],
        runtime=runtime,
    )
    captured = capsys.readouterr()
    lines = captured.out.splitlines()

    assert code == 0
    assert len(lines) == 4
    assert lines[0].startswith("Job: job_")
    assert lines[1] == "State: succeeded"
    assert lines[2].startswith("Bundle: bnd_")
    assert lines[3].startswith("Draft: art_")
    assert "{" not in captured.out
    assert captured.err == ""
    assert calls.commit == 1


def test_json_usage_error_is_one_safe_protocol_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["produce", "video", "--json"])
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 2
    assert envelope["ok"] is False
    assert envelope["command"] == "produce video"
    assert envelope["error"] == {
        "code": "cli_usage_invalid",
        "category": "invalid_request",
        "message": "Command arguments are invalid",
        "retryable": False,
        "next_actions": ["Correct the request and run the command again"],
        "details": {},
    }
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def test_human_usage_error_uses_stderr_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["produce", "video"])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert "Error [cli_usage_invalid]" in captured.err
    assert "Action:" in captured.err


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
            "--input",
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


class _GenericCaptureRuntime:
    def __init__(self) -> None:
        self.requests: list[ProduceRequest] = []
        self.video_calls = 0
        self.wait_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.snapshot = JobSnapshot(
            job_id="job_generic",
            state=JobState.QUEUED,
            cancellation_requested=False,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=None,
            error=None,
        )

    def submit(self, request: ProduceRequest) -> ProduceSubmission:
        self.requests.append(request)
        return ProduceSubmission("job_generic", request.recipe_key, JobState.QUEUED)

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        del request
        self.video_calls += 1
        return self.snapshot

    def get_job(self, job_id: str) -> JobSnapshot:
        assert job_id == "job_generic"
        return self.snapshot

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del event_sink
        self.wait_calls.append(job_id)
        return self.snapshot

    def cancel_job(self, job_id: str) -> JobSnapshot:
        self.cancel_calls.append(job_id)
        return self.snapshot


def test_generic_produce_calls_runtime_submit_once(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _GenericCaptureRuntime()

    code = main(
        [
            "produce",
            "fixture://course",
            "--recipe",
            "alltonote.video-producer@2",
            "--workspace",
            str(workspace_root),
            "--json",
        ],
        runtime=runtime,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0
    assert envelope["command"] == "produce"
    assert runtime.video_calls == 0
    assert len(runtime.requests) == 1
    assert runtime.requests[0].recipe_key.recipe_id == "alltonote.video-producer"
    assert runtime.requests[0].recipe_key.recipe_version == 2
    assert runtime.requests[0].requested_outputs == ("knowledge-note",)


def test_generic_explicit_v2_matches_legacy_v2_job_identity(
    runtime_factory,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, _ = runtime_factory()
    monkeypatch.setattr(
        "app.cli.main.new_typed_id",
        lambda prefix: "corr_018f0000-0000-7000-8000-000000000002",
    )
    common = ["--workspace", str(workspace_root), "--json"]

    legacy_code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--recipe-version",
            "2",
            *common,
        ],
        runtime=runtime,
    )
    legacy = json.loads(capsys.readouterr().out)
    generic_code = main(
        [
            "produce",
            "fixture://course",
            "--recipe",
            "alltonote.video-producer@2",
            *common,
        ],
        runtime=runtime,
    )
    generic = json.loads(capsys.readouterr().out)

    assert legacy_code == generic_code == 0
    assert legacy["data"] == generic["data"]
    assert legacy["job"] == generic["job"]
    assert legacy["artifacts"] == generic["artifacts"]
    assert legacy["command"] == "produce video"
    assert generic["command"] == "produce"


def test_generic_literal_video_input_uses_generic_route(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _GenericCaptureRuntime()

    assert main(
        [
            "produce",
            "video",
            "--recipe",
            "alltonote.video-course-note@1",
            "--workspace",
            str(workspace_root),
            "--json",
        ],
        runtime=runtime,
    ) == 0
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["command"] == "produce"
    assert runtime.requests[0].input.value == "video"
    assert runtime.video_calls == 0


def test_generic_literal_video_input_accepts_equals_recipe_option(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _GenericCaptureRuntime()

    assert main(
        [
            "produce",
            "video",
            "--recipe=alltonote.video-course-note@1",
            f"--workspace={workspace_root}",
            "--json",
        ],
        runtime=runtime,
    ) == 0
    capsys.readouterr()

    assert runtime.requests[0].input.value == "video"
    assert runtime.video_calls == 0


def test_generic_produce_help_does_not_expose_internal_parser_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["produce", "--help"])

    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "usage: alltonote produce " in captured.out
    assert "_generic" not in captured.out


def test_request_file_rejects_conflicting_workspace(
    tmp_path: Path,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "request.json"
    path.write_text("{}", encoding="utf-8")

    assert main(
        [
            "produce",
            "--request",
            str(path),
            "--workspace",
            str(workspace_root),
            "--json",
        ],
        runtime=_GenericCaptureRuntime(),
    ) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "cli_usage_invalid"


def test_generic_direct_uses_provider_default_model(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.core.config.model import ProviderProfileConfig, RuntimeConfig
    from app.runtime_config import effective_runtime_config

    class ConfigService:
        def effective(self, *, profile: object, cli_overrides: object) -> object:
            del profile, cli_overrides
            return effective_runtime_config(
                RuntimeConfig(
                    default_workspace=workspace_root,
                    providers={
                        "default": ProviderProfileConfig(
                            "fixture", default_model="model-from-profile"
                        )
                    },
                )
            )

    runtime = _GenericCaptureRuntime()
    assert main(
        [
            "produce",
            "fixture://course",
            "--recipe",
            "alltonote.video-producer@2",
            "--json",
        ],
        runtime=runtime,
        config_service=ConfigService(),  # type: ignore[arg-type]
    ) == 0
    capsys.readouterr()

    assert runtime.requests[0].parameters["model_override"] == "model-from-profile"


def test_request_file_resolves_workspace_in_submitted_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    working = tmp_path / "working"
    workspace = working / "vault"
    workspace.mkdir(parents=True)
    request_path = working / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "recipe_key": {
                    "recipe_id": "alltonote.video-course-note",
                    "recipe_version": 1,
                },
                "input": {"kind": "source", "value": "fixture://course"},
                "workspace_ref": "vault",
                "requested_outputs": ["knowledge-note"],
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )
    runtime = _GenericCaptureRuntime()
    monkeypatch.chdir(working)

    assert main(["produce", "--request", str(request_path), "--json"], runtime=runtime) == 0
    capsys.readouterr()

    assert runtime.requests[0].workspace_ref == str(workspace.resolve())


def test_generic_request_file_uses_existing_contract(
    tmp_path: Path,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _GenericCaptureRuntime()
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "recipe_key": {
                    "recipe_id": "alltonote.video-course-note",
                    "recipe_version": 1,
                },
                "input": {"kind": "source", "value": "fixture://course"},
                "workspace_ref": str(workspace_root),
                "requested_outputs": ["knowledge-note"],
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )

    assert main(["produce", "--request", str(request_path), "--json"], runtime=runtime) == 0
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["command"] == "produce"
    assert len(runtime.requests) == 1
    assert runtime.requests[0].input.value == "fixture://course"


def test_request_file_binds_current_config_snapshot_and_reopens(
    tmp_path: Path,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.core.config.model import RuntimeConfig
    from app.runtime import create_fake_runtime
    from app.runtime_config import effective_runtime_config

    base_config = RuntimeConfig(default_workspace=workspace_root)
    effective = effective_runtime_config(base_config)

    class ConfigService:
        def effective(self, *, profile: object, cli_overrides: object) -> object:
            del profile, cli_overrides
            return effective

    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "recipe_key": {
                    "recipe_id": "alltonote.video-course-note",
                    "recipe_version": 1,
                },
                "input": {"kind": "source", "value": "fixture://course"},
                "workspace_ref": str(workspace_root),
                "requested_outputs": ["knowledge-note"],
                "parameters": {},
                "client_request_id": "request-file-snapshot",
            }
        ),
        encoding="utf-8",
    )
    machine_root = tmp_path / "machine"
    runtime = create_fake_runtime(machine_root)

    assert main(
        ["produce", f"--request={request_path}", "--json"],
        runtime=runtime,
        config_service=ConfigService(),  # type: ignore[arg-type]
    ) == 0
    envelope = json.loads(capsys.readouterr().out)
    job_id = envelope["data"]["job_id"]
    events = runtime.job_repository.list_events(job_id)

    assert [event.event_type for event in events] == ["configuration.snapshot.v1"]
    assert json.loads(events[0].payload_json)["digest"] == effective.digest

    changed_config = replace(
        base_config,
        recipe_defaults=replace(base_config.recipe_defaults, style="changed"),
    )
    mismatched = create_fake_runtime(
        machine_root,
        current_config_snapshot=effective_runtime_config(
            changed_config
        ).job_snapshot(),
    )
    with pytest.raises(DomainError, match="effective_config_drift"):
        mismatched.wait_job(job_id)
    assert mismatched.get_job(job_id).state is JobState.QUEUED

    reopened = create_fake_runtime(
        machine_root,
        current_config_snapshot=effective.job_snapshot(),
    )
    assert reopened.wait_job(job_id).state is JobState.SUCCEEDED


def test_generic_wait_reuses_job_control_and_interrupt_cancels(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _GenericCaptureRuntime()

    def interrupt(job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del job_id, event_sink
        raise KeyboardInterrupt

    runtime.wait_job = interrupt  # type: ignore[method-assign]
    code = main(
        [
            "produce",
            "fixture://course",
            "--recipe",
            "alltonote.video-course-note@1",
            "--workspace",
            str(workspace_root),
            "--wait",
            "--json",
        ],
        runtime=runtime,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 130
    assert envelope["error"]["code"] == "interrupted"
    assert runtime.cancel_calls == ["job_generic"]


class _RequestCaptureRuntime:
    def __init__(self) -> None:
        self.request: VideoProduceRequest | None = None

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        self.request = request
        return JobSnapshot(
            job_id="job_captured",
            state=JobState.QUEUED,
            cancellation_requested=False,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=None,
            error=None,
        )


def test_canonical_input_and_positional_alias_keep_request_hash_compatible(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "app.cli.main.new_typed_id",
        lambda prefix: "corr_018f0000-0000-7000-8000-000000000001",
    )
    canonical = _RequestCaptureRuntime()
    alias = _RequestCaptureRuntime()

    canonical_code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--json",
        ],
        runtime=canonical,
    )
    canonical_envelope = json.loads(capsys.readouterr().out)
    alias_code = main(
        [
            "produce",
            "video",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--json",
        ],
        runtime=alias,
    )
    alias_envelope = json.loads(capsys.readouterr().out)

    assert canonical_code == alias_code == 0
    assert canonical_envelope["warnings"] == []
    assert alias_envelope["warnings"] == [
        "Positional video input is deprecated; use --input"
    ]
    assert canonical.request == alias.request
    assert canonical.request is not None
    assert alias.request is not None
    from app.core.application.video_service import VideoService

    assert VideoService._request_hash(canonical.request) == VideoService._request_hash(
        alias.request
    )


def test_human_positional_video_warning_preserves_success_output_contract(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _RequestCaptureRuntime()

    code = main(
        [
            "produce",
            "video",
            "fixture://course",
            "--workspace",
            str(workspace_root),
        ],
        runtime=runtime,
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == "Job: job_captured\nState: queued\n"
    assert captured.err == "Warning: Positional video input is deprecated; use --input\n"
    assert runtime.request is not None


def test_cli_rejects_conflicting_canonical_and_legacy_inputs_before_submit(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _RequestCaptureRuntime()

    code = main(
        [
            "produce",
            "video",
            "legacy://input",
            "--input",
            "canonical://input",
            "--workspace",
            str(workspace_root),
            "--json",
        ],
        runtime=runtime,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 2
    assert envelope["error"]["code"] == "cli_usage_invalid"
    assert runtime.request is None


@pytest.mark.parametrize(
    ("extra_args", "schema", "recipe_id", "recipe_version"),
    (
        ((), 1, "alltonote.video-course-note", 1),
        (
            ("--recipe-version", "2", "--quality", "balanced"),
            2,
            "alltonote.video-producer",
            2,
        ),
    ),
)
def test_cli_freezes_explicit_recipe_selection_without_changing_default(
    extra_args: tuple[str, ...],
    schema: int,
    recipe_id: str,
    recipe_version: int,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _RequestCaptureRuntime()
    code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--json",
            *extra_args,
        ],
        runtime=runtime,
    )

    envelope = json.loads(capsys.readouterr().out)
    assert code == 0
    assert envelope["data"] == {"job_id": "job_captured", "state": "queued"}
    assert runtime.request is not None
    assert runtime.request.request_schema_version == schema
    assert runtime.request.recipe_id == recipe_id
    assert runtime.request.recipe_version == recipe_version
    assert runtime.request.quality_preset == "balanced"
    assert runtime.request.requested_outputs == (VideoDocumentKind.KNOWLEDGE_NOTE,)


def test_cli_freezes_dual_output_and_translation_policy(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _RequestCaptureRuntime()

    code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--output",
            "knowledge-note",
            "--output",
            "faithful-edition",
            "--faithful-language",
            "translate-to-output",
            "--output-language",
            "zh-CN",
            "--json",
        ],
        runtime=runtime,
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert runtime.request is not None
    assert runtime.request.request_schema_version == 2
    assert runtime.request.recipe_id == "alltonote.video-producer"
    assert runtime.request.recipe_version == 2
    assert runtime.request.requested_outputs == (
        VideoDocumentKind.KNOWLEDGE_NOTE,
        VideoDocumentKind.FAITHFUL_EDITION,
    )
    assert runtime.request.faithful_language_policy is (
        FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
    )


def test_cli_freezes_provider_model_and_transcriber_profile_references(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _RequestCaptureRuntime()

    code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--provider-profile",
            "provider-main",
            "--model",
            "model-stable",
            "--transcriber-profile",
            "transcriber-local",
            "--json",
        ],
        runtime=runtime,
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == []
    assert runtime.request is not None
    assert runtime.request.provider_profile == "provider-main"
    assert runtime.request.model_override == "model-stable"
    assert runtime.request.transcriber_profile == "transcriber-local"


def test_cli_rejects_v2_output_with_explicit_recipe_v1(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--recipe-version",
            "1",
            "--output",
            "faithful-edition",
            "--json",
        ],
        runtime=_RequestCaptureRuntime(),
    )

    envelope = json.loads(capsys.readouterr().out)
    assert code == 2
    assert envelope["error"]["code"] == "recipe_version_conflict"


class _UnexpectedRuntime:
    def submit_video(self, request: object) -> object:
        del request
        raise RuntimeError("provider leaked implementation detail")


class _InterruptedRuntime:
    def submit_video(self, request: object) -> object:
        del request
        raise KeyboardInterrupt


class _WaitInterruptedRuntime:
    def __init__(self) -> None:
        self.cancelled_job_ids: list[str] = []

    def submit_video(self, request: object) -> JobSnapshot:
        del request
        return JobSnapshot(
            job_id="job_wait_interrupted",
            state=JobState.QUEUED,
            cancellation_requested=False,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=None,
            error=None,
        )

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del job_id, event_sink
        raise KeyboardInterrupt

    def cancel_job(self, job_id: str) -> JobSnapshot:
        self.cancelled_job_ids.append(job_id)
        return JobSnapshot(
            job_id=job_id,
            state=JobState.CANCELLED,
            cancellation_requested=True,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=None,
            error=None,
        )


def test_foreground_produce_interrupt_requests_structured_job_cancellation(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _WaitInterruptedRuntime()

    code = main(
        [
            "produce",
            "video",
            "--input",
            "fixture://course",
            "--workspace",
            str(workspace_root),
            "--wait",
            "--json",
        ],
        runtime=runtime,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 130
    assert envelope["error"]["code"] == "interrupted"
    assert runtime.cancelled_job_ids == ["job_wait_interrupted"]


def test_runtime_cancel_api_cancels_queued_video_without_external_work(
    runtime_factory,
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    submitted = runtime.submit_video(
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=workspace_root,
            input_value="fixture://course",
            client_request_id="runtime-cancel-api",
        )
    )

    cancelled = runtime.cancel_job(submitted.job_id)
    observed = runtime.wait_job(submitted.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancellation_requested is True
    assert observed == cancelled
    assert calls.download == calls.transcribe == calls.model == calls.commit == 0


def _unsafe_error_details(secret: str) -> dict[str, object]:
    return {
        "X-Auth-Token": secret,
        "prompt": secret,
        "provider_raw": {"response": secret},
        "credential_path": r"C:\Users\private\credential.json",
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


class _InconsistentTerminalRuntime:
    def __init__(self, state: JobState) -> None:
        self._snapshot = JobSnapshot(
            job_id="job_inconsistent",
            state=state,
            cancellation_requested=False,
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


@pytest.mark.parametrize("state", (JobState.SUCCEEDED, JobState.FAILED))
def test_inconsistent_terminal_projection_fails_closed(
    state: JobState,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run_fake_cli(_InconsistentTerminalRuntime(state), workspace_root)
    envelope = json.loads(capsys.readouterr().out)

    assert code == 70
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "job_projection_invalid"
    assert envelope["job"]["job_id"] == "job_inconsistent"


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
            "retryable": False,
            "next_actions": [
                "Submit a new Job or explicitly retry the terminal Job"
            ],
            "details": {},
        },
        "warnings": [],
        "job": {
            "job_id": "job_cancelled",
            "state": "cancelled",
            "cancellation_requested": True,
            "active_attempt_id": None,
            "challenge_id": None,
            "retry_of_job_id": None,
        },
        "artifacts": [],
        "capabilities": [],
        "versions": {
            "runtime_version": "0.1.0",
            "cli_protocol_version": 1,
        },
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
    assert envelope["error"]["details"]["prompt"] == "[REDACTED]"
    assert envelope["error"]["details"]["provider_raw"] == "[REDACTED]"
    assert envelope["error"]["details"]["credential_path"] == "[REDACTED]"
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
    render_module = importlib.import_module("app.cli.render")

    def fail_projection(identifier: str) -> bool:
        del identifier
        raise RuntimeError("projection failed")

    monkeypatch.setattr(render_module, "is_sensitive_identifier", fail_projection)

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
        "retryable": False,
        "next_actions": [
            "Satisfy the required policy, capability, grant, or credential"
        ],
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


def test_default_runtime_uses_real_codex_factory_and_never_fake(
    runtime_factory,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, calls = runtime_factory()
    runtime_module = importlib.import_module("app.runtime")
    requested_workspaces: list[Path] = []

    def create_real_runtime(workspace: Path, **options: object):
        requested_workspaces.append(workspace)
        assert options["current_config_snapshot"].snapshot_version == 1
        return runtime

    monkeypatch.setattr(
        runtime_module,
        "create_codex_app_server_runtime_for_workspace",
        create_real_runtime,
    )
    monkeypatch.setattr(
        runtime_module,
        "create_fake_runtime_for_workspace",
        lambda workspace: pytest.fail(
            f"The production CLI attempted to use Fake Runtime for {workspace}"
        ),
    )

    code = main(
        [
            "produce",
            "video",
            "--input",
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
    assert requested_workspaces == [workspace_root.resolve()]
    assert calls.commit == 1


def test_codex_runtime_factory_uses_workspace_instance_machine_root(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    local_app_data = tmp_path / "local-app-data"
    local_app_data.mkdir()
    captured: dict[str, object] = {}
    sentinel = object()

    monkeypatch.setattr(
        runtime_module.CodexAppServerStatusService,
        "get_status",
        staticmethod(
            lambda: SimpleNamespace(ready=True, default_model="codex-test-model")
        ),
    )
    pack_root = tmp_path / "packs"

    def resolved_pack(contract, digest: str):
        generation = pack_root / contract.pack_id
        entrypoints = {}
        for name, relative in contract.entrypoints("windows-x86_64").items():
            path = generation.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            entrypoints[name] = path.resolve()
        if contract.pack_id == "transcribe-cpu":
            (generation / "models" / "small").mkdir(parents=True)
        return runtime_module.ResolvedOfficialVideoPack(
            pack_id=contract.pack_id,
            pack_version=contract.pack_version,
            platform="windows-x86_64",
            manifest_sha256="sha256:" + digest * 64,
            generation=generation.resolve(),
            entrypoints=entrypoints,
        )

    resolved = {
        "media-basic": resolved_pack(runtime_module.MEDIA_BASIC, "a"),
        "transcribe-cpu": resolved_pack(runtime_module.TRANSCRIBE_CPU, "b"),
    }
    exact_resolutions: list[tuple[str, str]] = []

    class _Resolver:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def resolve_active(self, contract):
            return resolved[contract.pack_id]

        def resolve_exact(self, contract, manifest_sha256: str):
            exact_resolutions.append((contract.pack_id, manifest_sha256))
            selected = resolved[contract.pack_id]
            assert selected.manifest_sha256 == manifest_sha256
            return selected

    monkeypatch.setattr(runtime_module, "OfficialVideoPackResolver", _Resolver)

    def capture_runtime(machine_root: Path, **options: object):
        captured["machine_root"] = machine_root
        captured.update(options)
        return sentinel

    monkeypatch.setattr(
        runtime_module, "create_platform_video_runtime", capture_runtime
    )

    created = runtime_module.create_codex_app_server_runtime_for_workspace(
        workspace_root,
        local_app_data=local_app_data,
    )

    registry = json.loads(
        (local_app_data / "AllToNote" / "workspace-instances.json").read_text(
            encoding="utf-8"
        )
    )
    instance_id = registry["instances"][0]["instance_id"]
    workspace_identity = registry["instances"][0]["workspace_identity"]
    expected_machine_root = (
        local_app_data / "AllToNote" / "workspaces" / instance_id
    )
    binding = captured["model_execution_binding"]
    model = captured["model"]
    resource_owner = captured["resource_owner"]
    resource_lease_store = captured["resource_lease_store"]

    assert created is sentinel
    assert captured["machine_root"] == expected_machine_root
    assert captured["local_instance_id"] == instance_id
    assert captured["owner_id"] == resource_owner.process_instance_id
    assert resource_owner.workspace_identity == workspace_identity
    assert resource_owner.process_id == os.getpid()
    assert resource_lease_store.database_path == (
        local_app_data / "AllToNote" / "machine" / "leases.sqlite"
    )
    assert captured["source_metadata"] == {}
    assert captured["pack_environment"].packs[0].pack_id == "media-basic"
    assert captured["pack_environment"].packs[1].pack_id == "transcribe-cpu"
    pack_port_resolver = captured["pack_port_resolver"]
    _source, transcriber, transcriber_identity = pack_port_resolver(
        captured["pack_environment"]
    )
    assert transcriber_identity == transcriber.identity
    assert exact_resolutions == [
        ("media-basic", "sha256:" + "a" * 64),
        ("transcribe-cpu", "sha256:" + "b" * 64),
    ]
    assert captured["model_execution_profile"] == "default"
    assert model.provider_kind == "codex-app-server"
    assert model.model_identity == "codex-test-model"
    assert binding.provider_type == "codex-app-server"
    assert binding.model_identity == "codex-test-model"
    assert binding.credential_profile_ref == "codex/local-login"
