from __future__ import annotations

import json
import multiprocessing
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from app.adapters.models.legacy_gpt import (
    LegacyKnownRetryableModelFailure,
    LegacyModelBinding,
    LegacyModelCapabilities,
    LegacyModelResponse,
)
from app.adapters.sources.legacy_video import (
    LegacyNoSubtitleError,
    LegacyVideoSourceAdapter,
)
from app.core.application.video_acquisition import transcript_identity
from app.core.application.video_checkpoints import (
    decode_acquired,
    decode_source,
    encode_source,
)
from app.core.application.video_service import VideoService
from app.core.domain.ids import sha256_digest
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    JobState,
    QualityOverall,
    TranscriptDocument,
    TranscriptSegment,
    VideoDocumentKind,
    VideoProduceRequest,
)
from app.core.errors import DomainError
from app.core.jobs.model import AttemptState
from app.core.packs.events import (
    JOB_PACK_ENVIRONMENT_EVENT,
    ExecutionPackIdentity,
    JobPackEnvironmentSnapshot,
)
from app.core.ports.model_executor import (
    ModelExecutionBinding,
    ModelExecutionRequest,
)
from app.core.ports.transcript import MediaInput
import app.runtime as runtime_module
import app.adapters.iwiki.portable_gateway as portable_gateway_module


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
WORKSPACE_FIXTURE = FIXTURE_ROOT / "workspace-v2"
PLATFORM_FIXTURES = FIXTURE_ROOT / "video"
PLATFORM_URLS = {
    "bilibili": "https://www.bilibili.com/video/BV1Golden15?p=1",
    "youtube": "https://www.youtube.com/watch?v=GoldenPath1",
}


def test_platform_runtime_composition_is_available() -> None:
    assert callable(getattr(runtime_module, "create_platform_video_runtime", None))


def test_resolved_platform_source_checkpoint_round_trips() -> None:
    source = LegacyVideoSourceAdapter(local_machine_id="fixture-machine").resolve(
        PLATFORM_URLS["youtube"]
    )

    assert decode_source(encode_source(source)) == source


@dataclass
class Calls:
    metadata: int = 0
    subtitle: int = 0
    media_download: int = 0
    ffmpeg: int = 0
    transcriber: int = 0
    model: int = 0
    model_prompts: list[str] = field(default_factory=list)
    model_stages: list[str] = field(default_factory=list)


class _FixtureDownloader:
    def __init__(
        self,
        platform: str,
        calls: Calls,
        *,
        subtitle_outcome: str = "available",
    ) -> None:
        self._metadata = _fixture(platform, "metadata.json")
        self._subtitle = _fixture(platform, "subtitle.json")
        self._calls = calls
        self._subtitle_outcome = subtitle_outcome

    def download(self, _url: str, **kwargs: object) -> object:
        if kwargs.get("skip_download") is not True:
            self._calls.media_download += 1
            media_path = Path(str(kwargs["output_dir"])) / "fixture-audio.mp3"
            media_path.write_bytes(b"fixture-audio")
        else:
            media_path = None
        self._calls.metadata += 1
        return SimpleNamespace(
            title=self._metadata["title"],
            duration=self._metadata["duration_ms"] / 1000,
            cover_url=None,
            file_path=None if media_path is None else str(media_path),
            video_path=None,
        )

    def download_subtitles(self, _url: str, **_kwargs: object) -> object:
        self._calls.subtitle += 1
        if self._subtitle_outcome == "unavailable":
            raise LegacyNoSubtitleError
        if self._subtitle_outcome == "malformed":
            return SimpleNamespace(
                language=self._subtitle["language"],
                segments=[{"start": 0.0, "end": 1.0}],
            )
        return SimpleNamespace(
            language=self._subtitle["language"],
            segments=self._subtitle["segments"],
        )


class _Completion:
    def __init__(self, calls: Calls, failure: str | None = None) -> None:
        self._calls = calls
        self._failure = failure

    def complete_once(self, prompt: str, *, check_cancelled=None) -> LegacyModelResponse:
        if check_cancelled is not None:
            check_cancelled()
        assert "seg_000001" in prompt
        self._calls.model += 1
        if self._failure == "known":
            raise LegacyKnownRetryableModelFailure
        if self._failure == "unknown":
            raise TimeoutError("provider response was lost")
        return LegacyModelResponse(
            markdown=(
                "# Video note\n\n"
                "## Verifiable source\n\n"
                "The note preserves evidence from the transcript.[^seg_000001]\n"
            ),
            provider_request_id="fixture-request-15",
            input_tokens=20,
            output_tokens=10,
            actual_model="fixture/model-v1",
        )


class _Transcript:
    def __init__(self, calls: Calls) -> None:
        self._calls = calls

    def transcribe(self, media: MediaInput, token: object) -> TranscriptDocument:
        assert media.media_path is not None
        assert media.media_path.is_file()
        token.raise_if_cancelled()
        self._calls.transcriber += 1
        return TranscriptDocument(
            language="zh-CN",
            segments=(
                TranscriptSegment(
                    "seg_000001",
                    0,
                    2_000,
                    "无平台字幕时回退到本地转写。",
                ),
            ),
        )


class _V2Completion:
    def __init__(self, calls: Calls, failure: str | None = None) -> None:
        self._calls = calls
        self._failure = failure

    def complete_once(self, prompt: str, *, check_cancelled=None) -> LegacyModelResponse:
        if check_cancelled is not None:
            check_cancelled()
        self._calls.model_prompts.append(prompt)
        user_content = prompt.split("<user_content>\n", 1)[1].split(
            "\n</user_content>", 1
        )[0]
        payload = json.loads(user_content)
        self._calls.model += 1
        self._calls.model_stages.append(
            "faithful-edit" if "section" in payload else "global-compose"
        )
        if self._failure == "unknown":
            raise TimeoutError("provider response was lost")
        if "section" in payload:
            section = payload["section"]
            segments = section["segments"]
            return LegacyModelResponse(
                markdown=json.dumps(
                    {
                        "schema_version": 1,
                        "section_id": section["section_id"],
                        "section_ordinal": section["section_ordinal"],
                        "title": "Chronological source",
                        "paragraphs": [
                            {
                                "paragraph_ordinal": 0,
                                "text": " ".join(value["text"] for value in segments),
                                "source_segment_ids": [
                                    value["segment_id"] for value in segments
                                ],
                            }
                        ],
                        "summary": {
                            "text": "Faithful section summary",
                            "source_segment_ids": [segments[0]["segment_id"]],
                        },
                        "key_points": [],
                        "uncertainties": [],
                        "warnings": [],
                    }
                ),
                provider_request_id=f"fixture-faithful-{self._calls.model}",
                input_tokens=40,
                output_tokens=20,
                actual_model="fixture/model-v2",
            )
        assert payload["coverage_input_kind"] == "segment"
        segment_ids = [value["segment_id"] for value in payload["segments"]]
        return LegacyModelResponse(
            markdown=json.dumps(
                {
                    "schema_version": 1,
                    "markdown": (
                        "# One coherent article\n\n"
                        "## Main lesson\n\n"
                        f"The source remains verifiable.[^{segment_ids[0]}]"
                    ),
                    "covered_input_ids": payload["coverage_input_ids"],
                    "omissions": [],
                    "warnings": [],
                }
            ),
            provider_request_id="fixture-v2-request-1",
            input_tokens=40,
            output_tokens=20,
            actual_model="fixture/model-v2",
        )

    def complete_request(
        self,
        prompt: str,
        request: ModelExecutionRequest,
        *,
        check_cancelled=None,
    ) -> LegacyModelResponse:
        if check_cancelled is not None:
            check_cancelled()
        assert request.max_output_tokens > 0
        assert request.timeout_seconds > 0
        return self.complete_once(prompt)


def _v2_model_binding() -> ModelExecutionBinding:
    return ModelExecutionBinding(
        schema_version=1,
        provider_type="fixture/provider-v2",
        model_identity="fixture/model-v2",
        credential_profile_ref="default",
        context_window_tokens=16_384,
        max_output_tokens=2_048,
        max_concurrency=2,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )


def _use_source_tree_iwiki_version(monkeypatch: pytest.MonkeyPatch) -> None:
    locked = json.loads(
        (Path(runtime_module.__file__).parent / "runtime-lock.json").read_text("utf-8")
    )["iwiki_package"].split("==", 1)[1]
    monkeypatch.setattr(portable_gateway_module.metadata, "version", lambda _name: locked)


class _TerminateBeforeModel:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def generate_draft(self, *_args: object, **_kwargs: object) -> object:
        os._exit(23)


def _fixture(platform: str, name: str) -> dict[str, object]:
    return json.loads((PLATFORM_FIXTURES / platform / name).read_text("utf-8"))


class _RuntimeHarness:
    def __init__(self, runtime: object, video_service: object) -> None:
        self.runtime = runtime
        self.video_service = video_service

    def __getattr__(self, name: str) -> object:
        return getattr(self.runtime, name)


def _create_runtime(
    machine_root: Path,
    platform: str,
    calls: Calls,
    *,
    failure: str | None = None,
    subtitle_outcome: str = "available",
    owner_id: str | None = None,
    now_ms: int | None = None,
    media_fallback: bool = False,
    pack_environment: JobPackEnvironmentSnapshot | None = None,
    resolved_pack_environments: list[JobPackEnvironmentSnapshot] | None = None,
) -> object:
    downloader = _FixtureDownloader(
        platform,
        calls,
        subtitle_outcome=subtitle_outcome,
    )
    source = LegacyVideoSourceAdapter(
        local_machine_id="fixture-machine",
        factories={platform: lambda: downloader},
    )
    model = LegacyModelBinding(
        provider_kind="fixture/provider-v1",
        model_identity="fixture/model-v1",
        bridge=_Completion(calls, failure),
        capabilities=LegacyModelCapabilities(),
    )
    transcriber = _Transcript(calls) if media_fallback else None

    def resolve_pack_ports(
        snapshot: JobPackEnvironmentSnapshot,
    ) -> tuple[object, object | None, str]:
        assert resolved_pack_environments is not None
        resolved_pack_environments.append(snapshot)
        return source, transcriber, "fixture/transcriber-v1"

    runtime, service = runtime_module._create_platform_video_runtime_components(
        machine_root,
        source=source,
        source_metadata={platform: _fixture(platform, "metadata.json")},
        transcriber=transcriber,
        generated_transcriber_identity=(
            "fixture/transcriber-v1" if media_fallback else None
        ),
        pack_environment=pack_environment,
        pack_port_resolver=(
            resolve_pack_ports
            if resolved_pack_environments is not None
            else None
        ),
        model=model,
        owner_id=owner_id,
        clock=(None if now_ms is None else lambda: now_ms),
    )
    return _RuntimeHarness(runtime, service)


def _pack_environment(
    *,
    media_digest: str = "a",
    transcribe_digest: str = "b",
) -> JobPackEnvironmentSnapshot:
    return JobPackEnvironmentSnapshot(
        schema_version=1,
        packs=(
            ExecutionPackIdentity(
                pack_id="media-basic",
                pack_version="yt-dlp-2026.7.4-ffmpeg-8.1.2-r1",
                platform="windows-x86_64",
                manifest_sha256="sha256:" + media_digest * 64,
            ),
            ExecutionPackIdentity(
                pack_id="transcribe-cpu",
                pack_version="faster-whisper-1.1.1-small-536b0662-r1",
                platform="windows-x86_64",
                manifest_sha256="sha256:" + transcribe_digest * 64,
            ),
        ),
    )


def _create_v2_runtime(
    machine_root: Path,
    calls: Calls,
    *,
    model_binding: ModelExecutionBinding | None = None,
    model_profile: str | None = "default",
    failure: str | None = None,
) -> object:
    downloader = _FixtureDownloader("youtube", calls)
    source = LegacyVideoSourceAdapter(
        local_machine_id="fixture-machine",
        factories={"youtube": lambda: downloader},
    )
    legacy_model = LegacyModelBinding(
        provider_kind="fixture/provider-v2",
        model_identity="fixture/model-v2",
        bridge=_V2Completion(calls, failure),
        capabilities=LegacyModelCapabilities(),
    )
    runtime, service = runtime_module._create_platform_video_runtime_components(
        machine_root,
        source=source,
        source_metadata={"youtube": _fixture("youtube", "metadata.json")},
        model=legacy_model,
        model_execution_binding=model_binding,
        model_execution_profile=model_profile,
    )
    return _RuntimeHarness(runtime, service)


def _terminate_job_before_model(machine_root: str, job_id: str) -> None:
    runtime = _create_runtime(
        Path(machine_root),
        "youtube",
        Calls(),
        owner_id="terminated-process",
        now_ms=1_000,
    )
    service = runtime.video_service
    service._operations = _TerminateBeforeModel(service._operations)
    runtime.wait_job(job_id)
    os._exit(24)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    shutil.copytree(WORKSPACE_FIXTURE, root)
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


def test_platform_runtime_rejects_mismatched_explicit_v2_binding(
    tmp_path: Path,
) -> None:
    binding = replace(_v2_model_binding(), model_identity="fixture/other-model")

    with pytest.raises(DomainError) as caught:
        _create_v2_runtime(tmp_path / "machine", Calls(), model_binding=binding)

    assert caught.value.code == "model_execution_binding_mismatch"


def test_platform_runtime_requires_explicit_profile_for_v2_binding(
    tmp_path: Path,
) -> None:
    with pytest.raises(DomainError) as caught:
        _create_v2_runtime(
            tmp_path / "machine",
            Calls(),
            model_binding=_v2_model_binding(),
            model_profile=None,
        )

    assert caught.value.code == "model_execution_profile_required"


def test_platform_runtime_v2_uses_explicit_binding_and_restart_does_not_replay(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_source_tree_iwiki_version(monkeypatch)
    calls = Calls()
    machine_root = tmp_path / "machine"
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS["youtube"],
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        client_request_id="platform-runtime-v2",
    )

    runtime = _create_v2_runtime(
        machine_root,
        calls,
        model_binding=_v2_model_binding(),
    )
    submitted = runtime.submit_video(request)
    completed = runtime.wait_job(submitted.job_id)

    assert completed.state is JobState.SUCCEEDED
    assert completed.result is not None
    assert calls.model == 1

    restarted = _create_v2_runtime(
        machine_root,
        calls,
        model_binding=_v2_model_binding(),
    )
    recovered = restarted.submit_video(request)
    recovered = restarted.wait_job(recovered.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    assert recovered.job_id == submitted.job_id
    assert calls.model == 1


def test_platform_runtime_v2_rejects_request_model_drift_before_paid_call(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_source_tree_iwiki_version(monkeypatch)
    calls = Calls()
    runtime = _create_v2_runtime(
        tmp_path / "machine",
        calls,
        model_binding=_v2_model_binding(),
    )
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS["youtube"],
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        model_override="fixture/unfrozen-model",
    )

    submitted = runtime.submit_video(request)
    failed = runtime.wait_job(submitted.job_id)

    assert failed.state is JobState.FAILED
    assert failed.error is not None
    assert failed.error.code == "model_execution_binding_mismatch"
    assert calls.model == 0


def test_platform_runtime_shares_one_coordinator_between_v2_compilers(
    tmp_path: Path,
) -> None:
    runtime = _create_v2_runtime(
        tmp_path / "machine",
        Calls(),
        model_binding=_v2_model_binding(),
    )
    service = runtime.video_service

    assert (
        service._knowledge_compiler._compiler._coordinator
        is service._faithful_compiler._compiler._coordinator
    )


def test_platform_runtime_v2_dual_outputs_commit_atomically_and_recover(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_source_tree_iwiki_version(monkeypatch)
    calls = Calls()
    machine_root = tmp_path / "machine"
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS["youtube"],
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        requested_outputs=(
            VideoDocumentKind.KNOWLEDGE_NOTE,
            VideoDocumentKind.FAITHFUL_EDITION,
        ),
        client_request_id="platform-runtime-dual-output",
    )
    runtime = _create_v2_runtime(
        machine_root,
        calls,
        model_binding=_v2_model_binding(),
    )

    submitted = runtime.submit_video(request)
    completed = runtime.wait_job(submitted.job_id)

    assert completed.state is JobState.SUCCEEDED
    assert completed.result is not None
    result = completed.result
    assert [document.document_kind for document in result.documents] == [
        VideoDocumentKind.KNOWLEDGE_NOTE,
        VideoDocumentKind.FAITHFUL_EDITION,
    ]
    assert result.primary_draft_artifact_id == result.documents[0].draft_artifact_id
    assert (
        result.quality_report_artifact_id
        == result.documents[0].quality_report_artifact_id
    )
    assert all(
        document.quality_overall is QualityOverall.PASS
        and document.publish_eligible
        for document in result.documents
    )
    assert calls.model_stages == ["global-compose", "faithful-edit"]

    bundle = workspace_root / result.workspace_relative_bundle_path
    manifest = json.loads((bundle / "bundle.json").read_bytes())
    document_profile = manifest["extensions"][
        "alltonote.video:output-profile"
    ]["documents"]
    assert document_profile == [
        {
            "document_kind": document.document_kind.value,
            "draft_artifact_id": document.draft_artifact_id,
            "quality_report_artifact_id": document.quality_report_artifact_id,
        }
        for document in result.documents
    ]
    assert manifest["outputs"]["primary_draft"] == result.primary_draft_artifact_id
    assert manifest["outputs"]["drafts"] == [
        document.draft_artifact_id for document in result.documents
    ]
    assert manifest["outputs"]["quality_reports"] == [
        document.quality_report_artifact_id for document in result.documents
    ]
    assert manifest["required_contracts"] == [
        "urn:alltonote:video-producer:output-profile:v2"
    ]
    artifacts = {
        artifact["artifact_id"]: artifact for artifact in manifest["artifacts"]
    }
    receipt = json.loads((bundle / "receipt.json").read_bytes())
    model_executor = next(
        item for item in receipt["executors"] if item["kind"] == "model"
    )
    assert model_executor["identity"] == "fixture/model-v2"
    receipt_outputs = receipt["parameters"]["summary"]["document_outputs"]
    assert len(receipt_outputs) == 2
    for document in result.documents:
        draft = bundle / "drafts" / f"{document.draft_artifact_id}.md"
        quality_path = (
            bundle
            / "quality"
            / f"{document.quality_report_artifact_id}.json"
        )
        quality = json.loads(quality_path.read_bytes())
        assert quality["subject"] == {
            "artifact_id": document.draft_artifact_id,
            "bundle_id": result.bundle_id,
            "sha256": sha256_digest(draft.read_bytes()),
        }
        assert quality["overall"] == QualityOverall.PASS.value
        draft_profile = artifacts[document.draft_artifact_id]["extensions"][
            "alltonote.video:draft"
        ]
        assert draft_profile["transcript_basis"] == "platform-caption"
        assert draft_profile["source_language"] == "en"
        expected_policy = (
            "output-language"
            if document.document_kind is VideoDocumentKind.KNOWLEDGE_NOTE
            else "preserve-source"
        )
        assert draft_profile["language_policy"] == expected_policy
        assert draft_profile["target_language"] == (
            "zh-CN" if expected_policy == "output-language" else None
        )
        compiler_checks = [
            check
            for check in quality["checks"]
            if check["id"].startswith("compiler.")
        ]
        compiler_quality = quality["metrics"]["compiler_quality"]
        assert compiler_checks
        assert len(compiler_checks) == sum(
            compiler_quality["method_summary"].values()
        )
        receipt_output = next(
            item
            for item in receipt_outputs
            if item["draft_artifact_id"] == document.draft_artifact_id
        )
        assert receipt_output["quality_report_artifact_id"] == (
            document.quality_report_artifact_id
        )
        assert receipt_output["draft_sha256"] == sha256_digest(draft.read_bytes())
        assert receipt_output["quality_report_sha256"] == sha256_digest(
            quality_path.read_bytes()
        )
        assert receipt_output["model_binding"]["sha256"].startswith("sha256:")
        assert receipt_output["execution"]["model_calls"] == 1
        assert "legacy_finish_reason_unavailable" in receipt_output["warnings"]
        assert receipt_output["quality"]["check_count"] == len(
            compiler_checks
        )
        if document.document_kind is VideoDocumentKind.FAITHFUL_EDITION:
            assert receipt_output["faithful"] == {
                "section_count": 1,
                "uncertainty_count": 0,
                "anchor_warning_count": 0,
                "body_segment_reference_coverage_ratio": 1.0,
            }
    assert list((workspace_root / "raw" / "personal" / "bundles").iterdir()) == [
        bundle
    ]

    restarted = _create_v2_runtime(
        machine_root,
        calls,
        model_binding=_v2_model_binding(),
    )
    recovered = restarted.wait_job(restarted.submit_video(request).job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.job_id == submitted.job_id
    assert recovered.result is not None
    assert recovered.result.bundle_id == result.bundle_id
    assert calls.model == 2
    assert calls.model_stages == ["global-compose", "faithful-edit"]


def test_platform_runtime_v2_unknown_model_outcome_is_not_resent(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_source_tree_iwiki_version(monkeypatch)
    calls = Calls()
    runtime = _create_v2_runtime(
        tmp_path / "machine",
        calls,
        model_binding=_v2_model_binding(),
        failure="unknown",
    )
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS["youtube"],
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        client_request_id="platform-runtime-v2-unknown",
    )
    submitted = runtime.submit_video(request)

    first = runtime.wait_job(submitted.job_id)
    second = runtime.wait_job(submitted.job_id)

    assert first.state is second.state is JobState.WAITING_FOR_INPUT
    assert first.challenge_id is not None
    assert second.challenge_id == first.challenge_id
    assert calls.model == 1
    assert calls.model_stages == ["global-compose"]
    _assert_challenge_matches_unknown_operations(runtime, submitted.job_id)


def test_platform_runtime_v2_produces_single_faithful_edition(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_source_tree_iwiki_version(monkeypatch)
    calls = Calls()
    runtime = _create_v2_runtime(
        tmp_path / "machine",
        calls,
        model_binding=_v2_model_binding(),
    )
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS["youtube"],
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        requested_outputs=(VideoDocumentKind.FAITHFUL_EDITION,),
        client_request_id="platform-runtime-faithful",
    )

    completed = runtime.wait_job(runtime.submit_video(request).job_id)

    assert completed.state is JobState.SUCCEEDED
    assert completed.result is not None
    assert len(completed.result.documents) == 1
    assert (
        completed.result.documents[0].document_kind
        is VideoDocumentKind.FAITHFUL_EDITION
    )
    assert calls.model == 1
    assert "Keep the edited body in the source language en." in calls.model_prompts[0]


def test_platform_runtime_passes_explicit_faithful_translation_target(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_source_tree_iwiki_version(monkeypatch)
    calls = Calls()
    runtime = _create_v2_runtime(
        tmp_path / "machine",
        calls,
        model_binding=_v2_model_binding(),
    )
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS["youtube"],
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        requested_outputs=(VideoDocumentKind.FAITHFUL_EDITION,),
        faithful_language_policy=FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT,
        output_language="zh-CN",
        client_request_id="platform-runtime-faithful-translation",
    )

    completed = runtime.wait_job(runtime.submit_video(request).job_id)

    assert completed.state is JobState.SUCCEEDED
    assert calls.model == 1
    assert (
        "Translate conservatively from en to zh-CN;"
        in calls.model_prompts[0]
    )


@pytest.mark.parametrize(
    "request_override",
    (
        {"provider_profile": "other-profile"},
        {"model_override": "fixture/other-model"},
    ),
)
def test_platform_runtime_faithful_rejects_binding_profile_drift_before_paid_call(
    request_override: dict[str, str],
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_source_tree_iwiki_version(monkeypatch)
    calls = Calls()
    runtime = _create_v2_runtime(
        tmp_path / "machine",
        calls,
        model_binding=_v2_model_binding(),
    )
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS["youtube"],
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        requested_outputs=(VideoDocumentKind.FAITHFUL_EDITION,),
        **request_override,
    )

    failed = runtime.wait_job(runtime.submit_video(request).job_id)

    assert failed.state is JobState.FAILED
    assert failed.error is not None
    assert failed.error.code == "model_execution_binding_mismatch"
    assert calls.model == 0


@pytest.fixture
def runtime_factory(tmp_path: Path) -> Callable[..., tuple[object, Calls]]:
    created = 0

    def create(
        *,
        platform: str,
        failure: str | None = None,
        subtitle_outcome: str = "available",
        machine_root: Path | None = None,
        owner_id: str | None = None,
        clock: Callable[[], int] | None = None,
    ) -> tuple[object, Calls]:
        nonlocal created
        created += 1
        calls = Calls()
        runtime = _create_runtime(
            machine_root or tmp_path / f"machine-{created}",
            platform,
            calls,
            failure=failure,
            subtitle_outcome=subtitle_outcome,
            owner_id=owner_id,
            now_ms=(clock() if clock is not None else None),
        )
        return runtime, calls

    return create


def request(
    platform: str,
    workspace_root: Path,
    *,
    provided_transcript: TranscriptDocument | None = None,
    client_request_id: str | None = None,
) -> VideoProduceRequest:
    return VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS[platform],
        client_request_id=client_request_id or f"subtitle-{platform}",
        provided_transcript=provided_transcript,
    )


def test_job_freezes_both_official_video_pack_generations(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    environment = _pack_environment()
    runtime = _create_runtime(
        tmp_path / "machine",
        "bilibili",
        Calls(),
        pack_environment=environment,
    )

    submitted = runtime.submit_video(request("bilibili", workspace_root))

    binding = runtime.job_repository.get_job_execution_binding(
        submitted.job_id
    )
    assert binding.pack_id == "media-basic"
    assert binding.pack_version == environment.pack("media-basic").pack_version
    events = [
        event
        for event in runtime.job_repository.list_events(submitted.job_id)
        if event.event_type == JOB_PACK_ENVIRONMENT_EVENT
    ]
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload == {
        "packs": [
            {
                "manifest_sha256": "sha256:" + "a" * 64,
                "pack_id": "media-basic",
                "pack_version": "yt-dlp-2026.7.4-ffmpeg-8.1.2-r1",
                "platform": "windows-x86_64",
            },
            {
                "manifest_sha256": "sha256:" + "b" * 64,
                "pack_id": "transcribe-cpu",
                "pack_version": "faster-whisper-1.1.1-small-536b0662-r1",
                "platform": "windows-x86_64",
            },
        ],
        "schema_version": 1,
    }


def test_recovery_refuses_same_version_with_different_pack_digest(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    machine = tmp_path / "machine"
    first = _create_runtime(
        machine,
        "bilibili",
        Calls(),
        pack_environment=_pack_environment(),
    )
    submitted = first.submit_video(request("bilibili", workspace_root))
    restarted = _create_runtime(
        machine,
        "bilibili",
        Calls(),
        pack_environment=_pack_environment(media_digest="c"),
    )

    with pytest.raises(DomainError) as caught:
        restarted.wait_job(submitted.job_id)

    assert caught.value.code == "execution_pack_drift"


def test_recovery_rebinds_to_exact_recorded_pack_generations(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    machine = tmp_path / "machine"
    submitted_environment = _pack_environment()
    first = _create_runtime(
        machine,
        "bilibili",
        Calls(),
        pack_environment=submitted_environment,
    )
    submitted = first.submit_video(request("bilibili", workspace_root))
    resolved: list[JobPackEnvironmentSnapshot] = []
    restarted = _create_runtime(
        machine,
        "bilibili",
        Calls(),
        pack_environment=_pack_environment(media_digest="c"),
        resolved_pack_environments=resolved,
    )

    completed = restarted.wait_job(submitted.job_id)

    assert completed.state is JobState.SUCCEEDED
    assert resolved == [submitted_environment]
    bundle = workspace_root / completed.result.workspace_relative_bundle_path
    receipt = json.loads((bundle / "receipt.json").read_text("utf-8"))
    feature_packs = [
        executor
        for executor in receipt["executors"]
        if executor["kind"] == "feature-pack"
    ]
    assert [pack["manifest_sha256"] for pack in feature_packs] == [
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    ]


def test_same_canonical_video_can_be_produced_again_with_stable_source_identity(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime = _create_runtime(
        tmp_path / "machine",
        "bilibili",
        Calls(),
    )

    first = runtime.wait_job(
        runtime.submit_video(
            request(
                "bilibili",
                workspace_root,
                client_request_id="repeat-video-one",
            )
        ).job_id
    )
    second = runtime.wait_job(
        runtime.submit_video(
            request(
                "bilibili",
                workspace_root,
                client_request_id="repeat-video-two",
            )
        ).job_id
    )

    assert first.state is JobState.SUCCEEDED
    assert second.state is JobState.SUCCEEDED
    assert first.result.bundle_id != second.result.bundle_id
    assert first.result.source_id == second.result.source_id
    assert first.result.source_revision_id != second.result.source_revision_id


def _decode_acquisition_checkpoint(runtime: object, job_id: str) -> object:
    metadata = runtime.job_repository.latest_checkpoint(job_id, "acquire")
    assert metadata is not None
    payload_path = (
        runtime.job_repository.machine_root.parent
        / "attempts"
        / metadata.relative_path
    )
    return decode_acquired(payload_path.read_bytes())


def _rewrite_acquisition_as_literal_pre16a_payload(runtime: object, job_id: str) -> None:
    metadata = runtime.job_repository.latest_checkpoint(job_id, "acquire")
    assert metadata is not None
    payload_path = (
        runtime.job_repository.machine_root.parent
        / "attempts"
        / metadata.relative_path
    )
    current = json.loads(payload_path.read_bytes())
    assert current.pop("stored_media") is None
    legacy_payload = json.dumps(
        current,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    payload_path.write_bytes(legacy_payload)
    with runtime.job_repository._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE checkpoints SET output_hash = ?, byte_length = ? WHERE checkpoint_id = ?",
            (sha256_digest(legacy_payload), len(legacy_payload), metadata.checkpoint_id),
        )


def _assert_challenge_matches_unknown_operations(runtime: object, job_id: str) -> None:
    _, _, challenge = runtime.job_repository.get_job_details(job_id)
    assert challenge is not None
    prompt = json.loads(challenge.prompt_json)
    with runtime.job_repository._connect() as connection:
        rows = connection.execute(
            """
            SELECT operation_id FROM external_operations
            WHERE job_id = ? AND step_id = 'generate_draft'
              AND outcome = 'external_outcome_unknown'
            ORDER BY operation_id
            """,
            (job_id,),
        ).fetchall()
    operation_ids = [row[0] for row in rows]
    assert operation_ids
    assert prompt == {
        "code": "external_outcome_unknown",
        "operation_ids": operation_ids,
    }


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_platform_subtitle_path_commits_without_media(
    platform: str,
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(platform=platform)

    result = runtime.wait_job(runtime.submit_video(request(platform, workspace_root)).job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert result.result.bundle_id.startswith("bnd_")
    assert result.result.quality_overall is QualityOverall.PASS
    assert calls.subtitle == 1
    assert calls.media_download == 0
    assert calls.transcriber == 0
    assert calls.ffmpeg == 0
    assert calls.model == 1
    bundle = workspace_root / result.result.workspace_relative_bundle_path
    draft = (bundle / "drafts" / f"{result.result.primary_draft_artifact_id}.md").read_text(
        "utf-8"
    )
    assert "[^seg_" not in draft
    assert "[^ev_" in draft
    assert "]: Video 00:00.000-00:02.000" in draft
    receipt = json.loads((bundle / "receipt.json").read_text("utf-8"))
    transcriber = next(
        executor
        for executor in receipt["executors"]
        if executor["kind"] == "transcriber"
    )
    assert transcriber["identity"] == f"{platform}/platform-subtitle-v1"


def test_platform_subtitle_path_does_not_require_transcribe_pack(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    calls = Calls()
    media_only = JobPackEnvironmentSnapshot(
        schema_version=1,
        packs=(_pack_environment().pack("media-basic"),),
    )
    runtime = _create_runtime(
        tmp_path / "machine-media-only",
        "bilibili",
        calls,
        pack_environment=media_only,
    )

    submitted = runtime.submit_video(request("bilibili", workspace_root))
    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert calls.subtitle == 1
    assert calls.media_download == 0
    assert calls.transcriber == 0
    bundle = workspace_root / result.result.workspace_relative_bundle_path
    receipt = json.loads((bundle / "receipt.json").read_text("utf-8"))
    feature_packs = [
        executor
        for executor in receipt["executors"]
        if executor["kind"] == "feature-pack"
    ]
    assert [pack["pack_id"] for pack in feature_packs] == ["media-basic"]
    summary = receipt["parameters"]["summary"]
    receipt_steps = summary["steps"]
    persisted_attempts = {
        attempt.attempt_id: attempt
        for attempt in runtime.job_repository.list_attempts(submitted.job_id)
    }
    receipt_attempt = persisted_attempts[summary["attempt_id"]]
    assert receipt_attempt.step_id == receipt_steps[-1]["step_id"]
    assert receipt["started_at"] == min(
        step["started_at"] for step in receipt_steps
    )
    assert receipt["completed_at"] == max(
        step["completed_at"] for step in receipt_steps
    )
    for step in receipt_steps:
        matching = [
            attempt
            for attempt in persisted_attempts.values()
            if attempt.step_id == step["step_id"]
            and attempt.created_at == step["started_at"]
            and attempt.updated_at == step["completed_at"]
            and attempt.state.value == step["state"]
        ]
        assert len(matching) == 1


def test_platform_without_subtitles_downloads_media_and_transcribes(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    calls = Calls()
    runtime = _create_runtime(
        tmp_path / "machine-fallback",
        "bilibili",
        calls,
        subtitle_outcome="unavailable",
        media_fallback=True,
    )

    submitted = runtime.submit_video(request("bilibili", workspace_root))
    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert calls.subtitle == 1
    assert calls.media_download == 1
    assert calls.transcriber == 1
    assert calls.model == 1
    acquisition = _decode_acquisition_checkpoint(runtime, submitted.job_id)
    assert acquisition.stored_media is not None
    assert acquisition.metadata.subtitle_acquisition == "generated"
    bundle = workspace_root / result.result.workspace_relative_bundle_path
    receipt = json.loads((bundle / "receipt.json").read_text("utf-8"))
    transcriber = next(
        executor
        for executor in receipt["executors"]
        if executor["kind"] == "transcriber"
    )
    assert transcriber["identity"] == "fixture/transcriber-v1"


@pytest.mark.parametrize("subtitle_outcome", ["unavailable", "malformed"])
def test_provided_transcript_precedes_lower_priority_platform_subtitle(
    subtitle_outcome: str,
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    provided = TranscriptDocument(
        language="en",
        segments=(TranscriptSegment("seg_000001", 0, 1000, "provided wins"),),
    )
    runtime, calls = runtime_factory(
        platform="youtube",
        subtitle_outcome=subtitle_outcome,
    )
    provided_request = request(
        "youtube",
        workspace_root,
        provided_transcript=provided,
        client_request_id="provided-transcript",
    )
    submitted = runtime.submit_video(provided_request)
    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert calls.metadata == 1
    assert calls.subtitle == 0
    acquisition = _decode_acquisition_checkpoint(runtime, submitted.job_id)
    assert acquisition.transcript == provided
    assert acquisition.transcript_identity == transcript_identity(provided)
    assert acquisition.metadata.subtitle_acquisition == "provided"
    assert acquisition.subtitle_availability.value == "available"
    assert acquisition.transcript_provenance.value == "provided"
    bundle = workspace_root / result.result.workspace_relative_bundle_path
    transcript = (bundle / "evidence" / "transcript.jsonl").read_text("utf-8")
    assert "provided wins" in transcript


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("provider_profile", "provider-two"),
        ("model_override", "model-two"),
        ("transcriber_profile", "transcriber-two"),
        (
            "provided_transcript",
            TranscriptDocument(
                language="en",
                segments=(
                    TranscriptSegment("seg_000001", 0, 1_000, "provided fingerprint"),
                ),
            ),
        ),
    ],
)
def test_request_fingerprint_changes_for_each_execution_input(
    field_name: str,
    changed_value: object,
    workspace_root: Path,
) -> None:
    baseline = request("youtube", workspace_root)

    assert VideoService._request_hash(baseline) != VideoService._request_hash(
        replace(baseline, **{field_name: changed_value})
    )


def test_known_model_failure_is_terminal_without_challenge(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(platform="youtube", failure="known")

    result = runtime.wait_job(runtime.submit_video(request("youtube", workspace_root)).job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "model_generation_failed"
    assert result.challenge_id is None
    assert calls.model == 1


def test_unknown_model_outcome_pauses_once_without_resending(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(platform="bilibili", failure="unknown")
    submitted = runtime.submit_video(request("bilibili", workspace_root))

    first = runtime.wait_job(submitted.job_id)
    second = runtime.wait_job(submitted.job_id)

    assert first.state is second.state is JobState.WAITING_FOR_INPUT
    assert first.challenge_id is not None
    assert second.challenge_id == first.challenge_id
    assert calls.model == 1
    _assert_challenge_matches_unknown_operations(runtime, submitted.job_id)
    with runtime.job_repository._connect() as connection:
        attempt_state = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'generate_draft'",
            (submitted.job_id,),
        ).fetchone()[0]
    assert attempt_state == AttemptState.NEEDS_INPUT.value


def test_process_loss_before_model_reuses_acquisition_checkpoints(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "recovery-machine"
    first_runtime, _ = runtime_factory(
        platform="youtube",
        machine_root=machine_root,
        owner_id="submitting-process",
        clock=lambda: 1_000,
    )
    submitted = first_runtime.submit_video(request("youtube", workspace_root))
    process = multiprocessing.get_context("spawn").Process(
        target=_terminate_job_before_model,
        args=(str(machine_root), submitted.job_id),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23
    _rewrite_acquisition_as_literal_pre16a_payload(first_runtime, submitted.job_id)

    recovered_runtime, recovered_calls = runtime_factory(
        platform="youtube",
        machine_root=machine_root,
        owner_id="recovery-process",
        clock=lambda: 302_001,
    )
    result = recovered_runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert recovered_calls.model == 1
    assert recovered_calls.metadata == 0
    assert recovered_calls.subtitle == 0
    assert recovered_calls.media_download == 0
    assert recovered_calls.transcriber == 0
    with recovered_runtime.job_repository._connect() as connection:
        states = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'generate_draft' ORDER BY rowid",
            (submitted.job_id,),
        ).fetchall()
        acquisition_attempts = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'acquire'",
            (submitted.job_id,),
        ).fetchall()
    assert [row[0] for row in states] == [
        AttemptState.INTERRUPTED.value,
        AttemptState.SUCCEEDED.value,
    ]
    assert [row[0] for row in acquisition_attempts] == [AttemptState.SUCCEEDED.value]


def test_reopen_with_started_model_operation_pauses_before_provider_resend(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "unknown-recovery-machine"
    first_runtime, _ = runtime_factory(
        platform="bilibili",
        machine_root=machine_root,
        owner_id="lost-process",
        clock=lambda: 1_000,
    )
    submitted = first_runtime.submit_video(request("bilibili", workspace_root))
    repository = first_runtime.job_repository
    repository.transition_job(submitted.job_id, JobState.RUNNING)
    old_authority = repository.acquire_scheduler_lease(
        "lost-process", ttl_seconds=1
    )
    old_attempt = repository.start_attempt(
        repository.create_attempt(submitted.job_id, "generate_draft").attempt_id,
        old_authority,
    )
    operation = repository.prepare_external_operation(
        job_id=submitted.job_id,
        step_id=old_attempt.step_id,
        attempt_id=old_attempt.attempt_id,
        provider="fixture/provider-v1",
        request_hash="sha256:" + "1" * 64,
        operation_idempotency_key=None,
        summary_json="{}",
        authority=old_authority,
    )
    repository.start_external_operation(operation.operation_id, old_authority)

    recovered_runtime, recovered_calls = runtime_factory(
        platform="bilibili",
        machine_root=machine_root,
        owner_id="recovery-process",
        clock=lambda: 2_001,
    )
    first = recovered_runtime.wait_job(submitted.job_id)
    second = recovered_runtime.wait_job(submitted.job_id)

    assert first.state is second.state is JobState.WAITING_FOR_INPUT
    assert first.challenge_id == second.challenge_id
    assert recovered_calls.model == 0
    assert recovered_runtime.job_repository.get_external_operation(
        operation.operation_id
    ).outcome.value == "external_outcome_unknown"
    _assert_challenge_matches_unknown_operations(
        recovered_runtime, submitted.job_id
    )
    with recovered_runtime.job_repository._connect() as connection:
        states = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'generate_draft' ORDER BY rowid",
            (submitted.job_id,),
        ).fetchall()
    assert [row[0] for row in states] == [
        AttemptState.INTERRUPTED.value,
        AttemptState.NEEDS_INPUT.value,
    ]
