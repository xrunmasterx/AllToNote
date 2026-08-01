from __future__ import annotations

import importlib
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from iwiki.portable import PortableBundleRef, ValidationLevel, validate_bundle
from iwiki.workspace import open_workspace

import app.core.application.video_service as video_service_module
from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.core.application.job_service import JobService
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    GeneratedVideoDraft,
    JobState,
    QualityOverall,
    RetryJobRequest,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
    VideoDocumentKind,
    VideoProduceRequest,
)
from app.core.application.video_service import (
    VideoFaithfulCompilationInput,
    VideoKnowledgeCompilationInput,
    VideoService,
    VideoPreflightCapabilities,
    _CandidateCheckpoint,
)
from app.core.application.video_checkpoints import decode_draft, encode_draft
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.resource_lease import ResourceOwner
from app.core.jobs.workspace_publish import WorkspacePublishCoordinator


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
PREFLIGHT_CAPABILITY_FAILURES = (
    ("video_feature_pack", "video_feature_pack_unavailable"),
    ("source_capability", "source_capability_unavailable"),
    ("transcript_capability", "transcript_capability_unavailable"),
    ("model_capability", "model_capability_unavailable"),
    ("ffmpeg_loadable", "ffmpeg_unavailable"),
    ("model_loadable", "model_unavailable"),
    ("transcriber_loadable", "transcriber_unavailable"),
    ("effective_config_valid", "effective_config_invalid"),
    ("credential_references_resolvable", "credential_reference_unavailable"),
)
CHECKPOINT_STEPS = (
    "preflight",
    "resolve_source",
    "acquire",
    "normalize_transcript",
    "create_source_revision",
    "generate_draft",
    "optional_screenshots",
    "assemble_candidate_bundle",
    "quality_and_portable_validation",
)
_HEARTBEAT_THREAD_PREFIX = "alltonote-scheduler-heartbeat-"


def _background_heartbeat_threads() -> tuple[threading.Thread, ...]:
    return tuple(
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(_HEARTBEAT_THREAD_PREFIX)
    )


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


class _RuntimeHarness:
    def __init__(self, runtime: object, video_service: object) -> None:
        self.runtime = runtime
        self.video_service = video_service

    def __getattr__(self, name: str) -> object:
        return getattr(self.runtime, name)


def _create_fake_runtime(runtime_module: object, *args: object, **kwargs: object) -> _RuntimeHarness:
    runtime, service = runtime_module._create_fake_runtime_components(*args, **kwargs)
    return _RuntimeHarness(runtime, service)


@pytest.fixture
def runtime_factory(
    tmp_path: Path,
) -> Callable[..., tuple[object, object]]:
    created = 0

    def create(**options: object) -> tuple[object, object]:
        nonlocal created
        created += 1
        runtime_module = importlib.import_module("app.runtime")
        calls = runtime_module.FakeCallCounts()
        runtime = _create_fake_runtime(
            runtime_module,
            tmp_path / f"machine-{created}",
            calls=calls,
            **options,
        )
        return runtime, calls

    return create


def valid_request(
    workspace_root: Path,
    *,
    client_request_id: str = "fake-video-1",
) -> VideoProduceRequest:
    return VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id=client_request_id,
    )


class _FakeV2KnowledgeCompiler:
    def __init__(self, identity: str = "sha256:" + "a" * 64) -> None:
        self.calls: list[tuple[VideoKnowledgeCompilationInput, object]] = []
        self.identity = identity

    def compilation_identity(self) -> str:
        return self.identity

    def compile(
        self,
        request: VideoKnowledgeCompilationInput,
        *,
        execution: object,
    ) -> object:
        self.calls.append((request, execution))
        return SimpleNamespace(
            document_kind=VideoDocumentKind.KNOWLEDGE_NOTE,
            model_identity="fixture/model-v2",
            markdown=(
                "# Compiled video note\n\n"
                "## Main lesson\n\nA coherent article.[^seg_000001]"
            ),
            cited_segment_ids=("seg_000001",),
            screenshot_requests=(),
            usage=SimpleNamespace(
                input_tokens=321,
                output_tokens=123,
                token_counts_complete=False,
            ),
            execution_summary=SimpleNamespace(
                topology=SimpleNamespace(value="direct"),
                chunk_count=1,
                knowledge_item_count=0,
                model_operation_count=1,
                sequential_model_waves=1,
            ),
            coverage=SimpleNamespace(
                covered_input_ids=("seg_000001",),
                omissions=(),
            ),
            plan=SimpleNamespace(
                model_binding_sha256="sha256:" + "a" * 64,
                expected_sequential_model_waves=1,
            ),
            warnings=("compiler_fixture_warning",),
        )


class _FakeV2FaithfulCompiler:
    def __init__(self, identity: str = "sha256:" + "b" * 64) -> None:
        self.calls: list[tuple[VideoFaithfulCompilationInput, object]] = []
        self.identity = identity

    def compilation_identity(self) -> str:
        return self.identity

    def compile(
        self,
        request: VideoFaithfulCompilationInput,
        *,
        execution: object,
    ) -> object:
        self.calls.append((request, execution))
        return SimpleNamespace(
            document_kind=VideoDocumentKind.FAITHFUL_EDITION,
            model_identity="fixture/model-v2",
            markdown=(
                "# Faithful edition\n\n"
                "## Faithful body\n\nThe source stays complete.[^seg_000001]\n\n"
                "## AI summary\n\nA separate summary.[^seg_000001]"
            ),
            cited_segment_ids=("seg_000001",),
            screenshot_requests=(),
            usage=SimpleNamespace(
                input_tokens=222,
                output_tokens=111,
                token_counts_complete=True,
            ),
            execution_summary=SimpleNamespace(
                section_count=1,
                model_operation_count=1,
                sequential_model_waves=1,
                repair_operation_count=0,
                uncertainty_count=0,
                anchor_warning_count=0,
                body_segment_reference_coverage_ratio=1.0,
            ),
            text_assessment=SimpleNamespace(
                overall=QualityOverall.PASS,
                checks=(
                    SimpleNamespace(
                        check_id="body_segment_mapping",
                        method=SimpleNamespace(value="deterministic"),
                        status=SimpleNamespace(value="pass"),
                        safe_details="Body segment mapping is complete",
                    ),
                ),
                metrics=SimpleNamespace(
                    body_segment_reference_coverage_ratio=1.0,
                    order_violation_count=0,
                    unknown_reference_count=0,
                    duplicate_assignment_count=0,
                    source_character_count=24,
                    target_character_count=24,
                    length_ratio=1.0,
                    number_mismatch_count=0,
                    technical_token_mismatch_count=0,
                    qualifier_warning_count=0,
                    uncertainty_count=0,
                    anchor_warning_count=0,
                ),
            ),
            plan=SimpleNamespace(
                model_binding_sha256="sha256:" + "a" * 64,
            ),
            warnings=(),
        )


def _validate_committed_bundle(workspace_root: Path, bundle_id: str) -> None:
    workspace = open_workspace(workspace_root, writable=True)
    report = validate_bundle(
        workspace,
        PortableBundleRef.committed(bundle_id),
        ValidationLevel.SEMANTIC,
    )

    assert report.valid
    assert report.bundle_id == bundle_id
    assert report.issues == ()


def test_fake_recipe_commits_once_and_returns_bundle(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()

    submitted = runtime.submit_video(valid_request(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert submitted.state is JobState.QUEUED
    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    result = snapshot.result
    assert result.bundle_id.startswith("bnd_")
    assert result.workspace_relative_bundle_path.startswith(
        "raw/personal/bundles/"
    )
    assert result.primary_draft_artifact_id.startswith("art_")
    assert result.quality_overall is QualityOverall.PASS
    assert result.publish_eligible is True
    assert calls.download == 1
    assert calls.transcribe == 1
    assert calls.model == 1
    assert calls.ffmpeg == 0
    assert calls.commit == 1
    final = workspace_root / result.workspace_relative_bundle_path
    assert (final / "commit.json").is_file()
    assert [path.name for path in final.parent.iterdir()] == [result.bundle_id]
    _validate_committed_bundle(workspace_root, result.bundle_id)
    for step_id in CHECKPOINT_STEPS:
        assert runtime.job_repository.latest_checkpoint(
            submitted.job_id,
            step_id,
        ) is not None


def test_wait_returns_cancelled_snapshot_when_job_changes_before_claim(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, calls = runtime_factory()
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="cancel-before-claim")
    )
    repository = runtime.job_repository
    original_claim = repository.claim_job

    def cancel_then_claim(
        job_id: str,
        owner_id: str,
        *,
        ttl_seconds: int,
    ):
        repository.cancel_job(job_id)
        return original_claim(job_id, owner_id, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(repository, "claim_job", cancel_then_claim)

    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.CANCELLED
    assert calls.download == 0
    assert calls.transcribe == 0
    assert calls.model == 0
    assert calls.commit == 0


def test_v2_single_knowledge_note_uses_injected_compiler_and_draft_checkpoint(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    service = runtime.video_service
    compiler = _FakeV2KnowledgeCompiler()
    service._knowledge_compiler = compiler
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value="fixture://course",
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        client_request_id="v2-knowledge-compiler",
    )

    submitted = runtime.submit_video(request)
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    assert snapshot.result.usage == {"input_tokens": 321, "output_tokens": 123}
    assert snapshot.result.warnings == ()
    assert calls.model == 0
    assert len(compiler.calls) == 1
    compiled_request, execution = compiler.calls[0]
    assert compiled_request.output == request.resolved_outputs[0]
    assert compiled_request.source_title
    assert compiled_request.transcript.segments[0].segment_id == "seg_000001"
    assert not hasattr(compiled_request, "workspace_root")
    assert not hasattr(compiled_request, "input_value")
    assert execution.job_id == submitted.job_id
    assert execution.step_id == "generate_draft"
    assert execution.authority.fencing_token > 0
    draft_checkpoint = runtime.job_repository.latest_checkpoint(
        submitted.job_id,
        "generate_draft",
    )
    assert draft_checkpoint is not None
    draft = decode_draft(service._checkpoint_reader(draft_checkpoint))
    expected_usage = {
        "chunk_count": 1,
        "compilation_topology": "direct",
        "input_tokens": 321,
        "knowledge_item_count": 0,
        "model_operation_count": 1,
        "output_tokens": 123,
        "sequential_model_waves": 1,
        "token_counts_complete": "false",
    }
    assert {key: draft.usage[key] for key in expected_usage} == expected_usage
    assert draft.usage["model_binding_sha256"] == "sha256:" + "a" * 64
    assert draft.usage["repair_operation_count"] == 0
    quality_summary = json.loads(draft.usage["compiler_quality_summary"])
    assert quality_summary["overall"] == "pass"
    assert len(quality_summary["checks"]) == 8
    assert draft.warnings == (
        "compiler_fixture_warning",
        "model_token_usage_incomplete",
    )


def test_v2_recovery_rejects_compiler_binding_drift_before_replay(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    service = runtime.video_service

    class InterruptedCompiler(_FakeV2KnowledgeCompiler):
        def compile(
            self,
            request: VideoKnowledgeCompilationInput,
            *,
            execution: object,
        ) -> object:
            self.calls.append((request, execution))
            raise RuntimeError("injected after the frozen compiler identity")

    first_compiler = InterruptedCompiler("sha256:" + "1" * 64)
    service._knowledge_compiler = first_compiler
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value="fixture://course",
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        client_request_id="v2-binding-drift",
    )
    submitted = runtime.submit_video(request)

    with pytest.raises(
        RuntimeError, match="injected after the frozen compiler identity"
    ):
        runtime.wait_job(submitted.job_id)

    freeze_checkpoint = runtime.job_repository.latest_checkpoint(
        submitted.job_id,
        "freeze_knowledge_note_compilation",
    )
    assert freeze_checkpoint is not None
    replacement = _FakeV2KnowledgeCompiler("sha256:" + "2" * 64)
    service._knowledge_compiler = replacement

    recovered = runtime.wait_job(submitted.job_id)

    assert recovered.state is JobState.FAILED
    assert recovered.error is not None
    assert recovered.error.code == "compilation_binding_drift"
    assert len(first_compiler.calls) == 1
    assert replacement.calls == []
    assert calls.download == calls.transcribe == 1
    assert calls.model == calls.commit == 0


def test_v2_recovery_rejects_document_behavior_drift_before_replay(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, calls = runtime_factory()
    service = runtime.video_service
    compiler = _FakeV2KnowledgeCompiler()
    service._knowledge_compiler = compiler
    original_assemble = service._assemble

    def interrupt_after_draft(_bundle_input: object) -> object:
        raise RuntimeError("injected after the draft checkpoint")

    service._assemble = interrupt_after_draft
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value="fixture://course",
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        client_request_id="v2-document-behavior-drift",
    )
    submitted = runtime.submit_video(request)

    with pytest.raises(RuntimeError, match="after the draft checkpoint"):
        runtime.wait_job(submitted.job_id)

    assert runtime.job_repository.latest_checkpoint(
        submitted.job_id,
        "generate_draft",
    ) is not None
    assert len(compiler.calls) == 1
    monkeypatch.setattr(
        video_service_module,
        "_DOCUMENT_COMPILATION_BEHAVIOR",
        "projection-v999/finalization-v999/citation-format-v999",
    )
    service._assemble = original_assemble

    recovered = runtime.wait_job(submitted.job_id)

    assert recovered.state is JobState.FAILED
    assert recovered.error is not None
    assert recovered.error.code == "compilation_binding_drift"
    assert len(compiler.calls) == 1
    assert calls.model == calls.commit == 0


@pytest.mark.parametrize("dual", [False, True])
def test_v2_faithful_and_dual_outputs_commit_one_atomic_bundle(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    dual: bool,
) -> None:
    runtime, calls = runtime_factory()
    service = runtime.video_service
    knowledge = _FakeV2KnowledgeCompiler()
    faithful = _FakeV2FaithfulCompiler()
    service._knowledge_compiler = knowledge
    service._faithful_compiler = faithful
    requested = (
        (VideoDocumentKind.KNOWLEDGE_NOTE, VideoDocumentKind.FAITHFUL_EDITION)
        if dual
        else (VideoDocumentKind.FAITHFUL_EDITION,)
    )
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=workspace_root,
        input_value="fixture://course",
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        requested_outputs=requested,
        faithful_language_policy=FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT,
        output_language="zh-CN",
        client_request_id=f"v2-faithful-{dual}",
    )

    submitted = runtime.submit_video(request)
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    assert calls.commit == 1
    assert calls.model == 0
    assert [document.document_kind for document in snapshot.result.documents] == list(
        requested
    )
    assert len(faithful.calls) == 1
    assert faithful.calls[0][0].language_policy is (
        FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
    )
    assert faithful.calls[0][0].output_language == "zh-CN"
    assert len(knowledge.calls) == (1 if dual else 0)
    faithful_step = "generate_faithful_edition_draft" if dual else "generate_draft"
    assert runtime.job_repository.latest_checkpoint(
        submitted.job_id, faithful_step
    ) is not None
    _validate_committed_bundle(workspace_root, snapshot.result.bundle_id)


@pytest.mark.parametrize(
    ("request_factory", "expected_code"),
    (
        (
            lambda root: VideoProduceRequest(
                request_schema_version=2,
                workspace_root=root,
                input_value="fixture://course",
                recipe_id="alltonote.video-course-note",
                recipe_version=1,
            ),
            "recipe_version_unsupported",
        ),
        (
            lambda root: VideoProduceRequest(
                request_schema_version=2,
                workspace_root=root,
                input_value="fixture://course",
                recipe_id="alltonote.video-producer",
                recipe_version=2,
                quality_preset="fast",
            ),
            "output_quality_unsupported",
        ),
        (
            lambda root: VideoProduceRequest(
                request_schema_version=2,
                workspace_root=root,
                input_value="fixture://course",
                recipe_id="alltonote.video-producer",
                recipe_version=2,
                requested_outputs=(VideoDocumentKind.FAITHFUL_EDITION,),
            ),
            "faithful_compiler_unavailable",
        ),
    ),
)
def test_v2_preflight_fails_closed_before_external_work(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    request_factory: Callable[[Path], VideoProduceRequest],
    expected_code: str,
) -> None:
    runtime, calls = runtime_factory()
    runtime.video_service._knowledge_compiler = _FakeV2KnowledgeCompiler()

    submitted = runtime.submit_video(request_factory(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == expected_code
    assert calls.download == 0
    assert calls.transcribe == 0
    assert calls.model == 0


def test_repeated_segment_citations_keep_two_uses_and_one_definition(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, _ = runtime_factory()
    service = runtime.video_service
    delegate = service._operations

    class RepeatedCitationOperations:
        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

        def generate_draft(
            self,
            request_value: VideoProduceRequest,
            transcript_value: TranscriptDocument,
            *,
            execution: object,
        ) -> GeneratedVideoDraft:
            generated = delegate.generate_draft(
                request_value,
                transcript_value,
                execution=execution,
            )
            return replace(
                generated,
                markdown=(
                    "# Video note\n\n"
                    "## First\n\nFirst claim[^seg_000001].\n\n"
                    "## Second\n\nSecond claim[^seg_000001].\n"
                ),
                cited_segment_ids=("seg_000001",),
            )

    service._operations = RepeatedCitationOperations()
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="repeated-citation-uses")
    )
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    assert snapshot.result.quality_overall is QualityOverall.PASS
    evidence_id = VideoService._derived_id(
        submitted.job_id,
        "ev",
        "seg_000001",
    )
    label = f"[^{evidence_id}]"
    draft_path = (
        workspace_root
        / snapshot.result.workspace_relative_bundle_path
        / "drafts"
        / f"{snapshot.result.primary_draft_artifact_id}.md"
    )
    lines = draft_path.read_text(encoding="utf-8").splitlines()
    definition_lines = [line for line in lines if line.startswith(f"{label}:")]
    body = "\n".join(line for line in lines if not line.startswith(f"{label}:"))

    assert body.count(label) == 2
    assert len(definition_lines) == 1
    _validate_committed_bundle(workspace_root, snapshot.result.bundle_id)


def test_quality_fail_still_commits_and_returns_success(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(quality_fail=True)

    submitted = runtime.submit_video(valid_request(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    assert snapshot.result.quality_overall is QualityOverall.FAIL
    assert snapshot.result.publish_eligible is False
    assert calls.commit == 1
    _validate_committed_bundle(workspace_root, snapshot.result.bundle_id)


def test_retry_bundle_receipt_preserves_parent_job_id(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "retry-receipt-machine"
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        capabilities=replace(
            VideoPreflightCapabilities(), source_capability=False
        ),
        owner_id="retry-receipt-first",
    )
    original = first.wait_job(
        first.submit_video(
            valid_request(workspace_root, client_request_id="retry-receipt-original")
        ).job_id
    )
    assert original.state is JobState.FAILED

    retried = JobService(first.job_repository).retry(
        original.job_id,
        RetryJobRequest(
            retry_request_schema_version=1,
            client_request_id="retry-receipt-child",
            expected_original_job_state=JobState.FAILED,
        ),
    )
    del first
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        owner_id="retry-receipt-second",
    )

    snapshot = reopened.wait_job(retried.job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    bundle = workspace_root / snapshot.result.workspace_relative_bundle_path
    receipt = json.loads((bundle / "receipt.json").read_text("utf-8"))
    assert receipt["parameters"]["summary"]["retry_of_job_id"] == original.job_id


@pytest.mark.parametrize(("field_name", "error_code"), PREFLIGHT_CAPABILITY_FAILURES)
def test_preflight_failure_starts_no_external_work(
    runtime_factory: Callable[..., tuple[object, object]],
    field_name: str,
    error_code: str,
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(
        capabilities=replace(
            VideoPreflightCapabilities(), **{field_name: False}
        )
    )

    submitted = runtime.submit_video(valid_request(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == error_code
    assert calls.download == 0
    assert calls.transcribe == 0
    assert calls.model == 0
    assert calls.ffmpeg == 0
    assert calls.commit == 0
    staging = workspace_root / "raw" / "personal" / ".staging"
    assert tuple(staging.iterdir()) == ()


def test_screenshot_capability_is_checked_only_when_requested(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(
        capabilities=replace(
            VideoPreflightCapabilities(), screenshot_capability=False
        )
    )
    request = replace(
        valid_request(workspace_root, client_request_id="screenshots-required"),
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == "screenshot_capability_unavailable"
    assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0


def test_empty_on_demand_request_does_not_execute_ffmpeg(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    request = replace(
        valid_request(workspace_root, client_request_id="screenshots-empty"),
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.error is None
    assert calls.ffmpeg == 0


@pytest.mark.parametrize(
    ("policy", "screenshot_request", "error_code"),
    (
        (
            ScreenshotPolicy.OFF,
            ScreenshotRequest("seg_000001", 0),
            "screenshot_request_not_allowed",
        ),
        (
            ScreenshotPolicy.ON_DEMAND,
            ScreenshotRequest("seg_000001", 2_000),
            "screenshot_request_invalid",
        ),
    ),
)
def test_invalid_screenshot_work_never_reaches_screenshot_operation(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    policy: ScreenshotPolicy,
    screenshot_request: ScreenshotRequest,
    error_code: str,
) -> None:
    runtime, calls = runtime_factory()
    service = runtime.video_service
    delegate = service._operations

    class InvalidDraftOperations:
        screenshot_calls = 0

        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

        def generate_draft(self, *args: object, **kwargs: object) -> GeneratedVideoDraft:
            draft = delegate.generate_draft(*args, **kwargs)
            return replace(draft, screenshot_requests=(screenshot_request,))

        def screenshots(self, *args: object, **kwargs: object) -> tuple[object, ...]:
            del args, kwargs
            self.screenshot_calls += 1
            return ()

    operations = InvalidDraftOperations()
    service._operations = operations
    request = replace(
        valid_request(workspace_root, client_request_id=f"invalid-{policy.value}"),
        screenshot_policy=policy,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == error_code
    assert operations.screenshot_calls == 0
    assert calls.ffmpeg == 0


@pytest.mark.parametrize(
    ("policy", "expected_state"),
    (
        (ScreenshotPolicy.ON_DEMAND, JobState.FAILED),
        (ScreenshotPolicy.OFF, JobState.SUCCEEDED),
    ),
)
def test_screenshot_model_compatibility_is_typed_and_policy_scoped(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    policy: ScreenshotPolicy,
    expected_state: JobState,
) -> None:
    runtime, calls = runtime_factory(
        capabilities=replace(
            VideoPreflightCapabilities(), screenshot_model_compatible=False
        )
    )
    request = replace(
        valid_request(workspace_root, client_request_id=f"compat-{policy.value}"),
        screenshot_policy=policy,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is expected_state
    if policy is ScreenshotPolicy.ON_DEMAND:
        assert snapshot.error is not None
        assert snapshot.error.code == "screenshot_model_incompatible"
        assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0
    else:
        assert snapshot.error is None
        assert calls.download == calls.transcribe == calls.model == 1
        assert calls.ffmpeg == 0


def test_crash_after_rename_reconciles_without_new_model_work(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(crash_after_commit_once=True)
    submitted = runtime.submit_video(valid_request(workspace_root))

    with pytest.raises(RuntimeError, match="injected crash after portable rename"):
        runtime.wait_job(submitted.job_id)

    assert runtime.job_repository.get_job(submitted.job_id).state is JobState.RUNNING
    assert calls.model == 1
    assert calls.commit == 1
    committed = tuple(
        (workspace_root / "raw" / "personal" / "bundles").iterdir()
    )
    assert len(committed) == 1
    assert (committed[0] / "commit.json").is_file()

    recovered = runtime.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    assert recovered.result.bundle_id == committed[0].name
    assert recovered.result.idempotent is True
    assert calls.model == 1
    assert calls.commit == 1
    _validate_committed_bundle(workspace_root, recovered.result.bundle_id)


def _read_call_log(path: Path) -> tuple[str, ...]:
    return tuple(
        json.loads(line)["operation"]
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_crash_after_rename_recovers_after_runtime_reopen_and_new_fence(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "durable-machine"
    call_log = tmp_path / "external-calls.ndjson"
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        crash_after_commit_once=True,
        owner_id="process-a",
        clock=lambda: 1_000,
    )
    submitted = first.submit_video(
        valid_request(workspace_root, client_request_id="restart-after-rename")
    )

    with pytest.raises(RuntimeError, match="injected crash after portable rename"):
        first.wait_job(submitted.job_id)

    del first
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        owner_id="process-b",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    assert recovered.result.idempotent is True
    operations = _read_call_log(call_log)
    assert operations.count("download") == 1
    assert operations.count("transcribe") == 1
    assert operations.count("model") == 1
    assert operations.count("portable_commit") == 1
    with reopened.job_repository._connect() as connection:
        commit_attempts = connection.execute(
            """
            SELECT state FROM attempts
            WHERE job_id = ? AND step_id = 'commit'
            ORDER BY rowid
            """,
            (submitted.job_id,),
        ).fetchall()
        assembly_attempts = connection.execute(
            """
            SELECT COUNT(*) FROM attempts
            WHERE job_id = ? AND step_id = 'assemble_candidate_bundle'
            """,
            (submitted.job_id,),
        ).fetchone()[0]
    assert tuple(row["state"] for row in commit_attempts) == (
        "interrupted",
        "succeeded",
    )
    assert assembly_attempts == 1


@pytest.mark.parametrize(
    "historical_behavior",
    ("linked-screenshot-draft-v1", "linked-screenshot-draft-v2"),
)
def test_current_runtime_reconciles_historical_v1_candidate_after_portable_rename(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    historical_behavior: str,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "v1-commit-recovery-machine"
    call_log = tmp_path / "v1-commit-recovery-calls.ndjson"
    monkeypatch.setattr(
        video_service_module,
        "_CANDIDATE_ASSEMBLY_BEHAVIOR",
        historical_behavior,
    )
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        crash_after_commit_once=True,
        owner_id="v1-commit-process",
        clock=lambda: 1_000,
    )
    request = valid_request(
        workspace_root,
        client_request_id="v1-commit-recovery",
    )
    submitted = first.submit_video(request)

    with pytest.raises(RuntimeError, match="injected crash after portable rename"):
        first.wait_job(submitted.job_id)

    candidate_metadata = first.job_repository.latest_checkpoint(
        submitted.job_id, "assemble_candidate_bundle"
    )
    assert candidate_metadata is not None
    candidate = _CandidateCheckpoint.decode(
        (
            Path(first.job_repository.database_path).parent.parent
            / "attempts"
            / candidate_metadata.relative_path
        ).read_bytes()
    )
    committed_bundles = tuple(
        (workspace_root / "raw" / "personal" / "bundles").iterdir()
    )
    assert [bundle.name for bundle in committed_bundles] == [candidate.bundle_id]
    assert candidate_metadata.input_hash == VideoService._candidate_assembly_input_hash(
        VideoService._request_hash(request)
    )

    monkeypatch.setattr(
        video_service_module,
        "_CANDIDATE_ASSEMBLY_BEHAVIOR",
        "portable-output-profile-v2",
    )
    del first
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        owner_id="v2-commit-process",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    assert recovered.result.idempotent is True
    assert recovered.result.bundle_id == candidate.bundle_id
    assert recovered.result.manifest_sha256 == candidate.manifest_sha256
    operations = _read_call_log(call_log)
    assert operations.count("model") == 1
    assert operations.count("portable_commit") == 1
    assert [
        bundle.name
        for bundle in (workspace_root / "raw" / "personal" / "bundles").iterdir()
    ] == [candidate.bundle_id]
    with reopened.job_repository._connect() as connection:
        assembly_attempts = connection.execute(
            """
            SELECT COUNT(*) FROM attempts
            WHERE job_id = ? AND step_id = 'assemble_candidate_bundle'
            """,
            (submitted.job_id,),
        ).fetchone()[0]
    assert assembly_attempts == 1


def test_restart_after_draft_failure_reuses_transcript_checkpoint(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "transcript-replay-machine"
    call_log = tmp_path / "transcript-replay-calls.ndjson"
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        crash_operation_once="model",
        owner_id="process-a",
        clock=lambda: 1_000,
    )
    submitted = first.submit_video(
        valid_request(workspace_root, client_request_id="transcript-replay")
    )

    with pytest.raises(RuntimeError, match="injected model crash"):
        first.wait_job(submitted.job_id)

    del first
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        owner_id="process-b",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    operations = _read_call_log(call_log)
    assert operations.count("download") == 1
    assert operations.count("transcribe") == 1
    assert operations.count("model") == 2


def test_restart_after_screenshot_failure_reuses_draft_checkpoint(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "draft-replay-machine"
    call_log = tmp_path / "draft-replay-calls.ndjson"
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        crash_operation_once="screenshots",
        owner_id="process-a",
        clock=lambda: 1_000,
        screenshot_requests=(ScreenshotRequest("seg_000001", 0),),
    )
    request = replace(
        valid_request(workspace_root, client_request_id="draft-replay"),
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )
    submitted = first.submit_video(request)

    with pytest.raises(RuntimeError, match="injected screenshots crash"):
        first.wait_job(submitted.job_id)

    del first
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        owner_id="process-b",
        clock=lambda: 302_000,
        screenshot_requests=(ScreenshotRequest("seg_000001", 0),),
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    operations = _read_call_log(call_log)
    assert operations.count("download") == 1
    assert operations.count("transcribe") == 1
    assert operations.count("model") == 1


def test_recovered_legacy_draft_spaces_citations_without_repeating_model_work(
    tmp_path: Path,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "legacy-citation-spacing-machine"
    call_log = tmp_path / "legacy-citation-spacing-calls.ndjson"
    transcript = TranscriptDocument(
        "zh-CN",
        (
            TranscriptSegment("seg_000001", 0, 1_000, "First claim"),
            TranscriptSegment("seg_000002", 1_000, 2_000, "Second claim"),
        ),
    )
    request = replace(
        valid_request(workspace_root, client_request_id="legacy-citation-spacing"),
        provided_transcript=transcript,
    )
    monkeypatch.setattr(
        video_service_module,
        "_CANDIDATE_ASSEMBLY_BEHAVIOR",
        "linked-screenshot-draft-v1",
    )
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        owner_id="legacy-spacing-first",
        clock=lambda: 1_000,
    )
    service = first.video_service
    delegate = service._operations

    class AdjacentCitationOperations:
        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

        def generate_draft(
            self,
            request_value: VideoProduceRequest,
            transcript_value: TranscriptDocument,
            *,
            execution: object,
        ) -> GeneratedVideoDraft:
            generated = delegate.generate_draft(
                request_value,
                transcript_value,
                execution=execution,
            )
            return replace(
                generated,
                markdown=(
                    "# Video note\n\n## Evidence\n\n"
                    "First and second claims.[^seg_000001][^seg_000002]\n"
                ),
                cited_segment_ids=("seg_000001", "seg_000002"),
            )

    service._operations = AdjacentCitationOperations()
    original_validate = service._validate_candidate

    def crash_after_v1_candidate(*args: object, **kwargs: object) -> object:
        del args, kwargs
        service._validate_candidate = original_validate
        raise RuntimeError("injected after v1 candidate checkpoint")

    service._validate_candidate = crash_after_v1_candidate
    submitted = first.submit_video(request)

    with pytest.raises(RuntimeError, match="injected after v1 candidate checkpoint"):
        first.wait_job(submitted.job_id)

    draft_metadata = first.job_repository.latest_checkpoint(
        submitted.job_id, "generate_draft"
    )
    assert draft_metadata is not None
    request_hash = VideoService._request_hash(request)
    assert draft_metadata.input_hash == request_hash
    draft_path = (
        Path(first.job_repository.database_path).parent.parent
        / "attempts"
        / draft_metadata.relative_path
    )
    durable_draft = decode_draft(draft_path.read_bytes())
    evidence_ids = tuple(
        VideoService._derived_id(submitted.job_id, "ev", segment_id)
        for segment_id in durable_draft.cited_segment_ids
    )
    adjacent = f"[^{evidence_ids[0]}][^{evidence_ids[1]}]"
    spaced = f"[^{evidence_ids[0]}] [^{evidence_ids[1]}]"
    legacy_draft = replace(
        durable_draft,
        markdown=durable_draft.markdown.replace(spaced, adjacent),
    )
    legacy_payload = encode_draft(legacy_draft)
    assert adjacent in legacy_draft.markdown
    draft_path.write_bytes(legacy_payload)
    with first.job_repository._transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE checkpoints SET output_hash = ?, byte_length = ?
            WHERE checkpoint_id = ?
            """,
            (
                sha256_digest(legacy_payload),
                len(legacy_payload),
                draft_metadata.checkpoint_id,
            ),
        )

    old_candidate_hash = sha256_digest(
        json.dumps(
            {"behavior": "linked-screenshot-draft-v1", "request": request_hash},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    v1_candidate_metadata = first.job_repository.latest_checkpoint(
        submitted.job_id, "assemble_candidate_bundle"
    )
    assert v1_candidate_metadata is not None
    assert v1_candidate_metadata.input_hash == old_candidate_hash

    monkeypatch.setattr(
        video_service_module,
        "_CANDIDATE_ASSEMBLY_BEHAVIOR",
        "linked-screenshot-draft-v2",
    )
    assert VideoService._candidate_assembly_input_hash(request_hash) != old_candidate_hash
    del first
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        call_log_path=call_log,
        owner_id="legacy-spacing-second",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    operations = _read_call_log(call_log)
    assert operations.count("model") == 1
    recovered_draft_metadata = reopened.job_repository.latest_checkpoint(
        submitted.job_id, "generate_draft"
    )
    assert recovered_draft_metadata is not None
    assert recovered_draft_metadata.checkpoint_id == draft_metadata.checkpoint_id
    assert recovered_draft_metadata.input_hash == request_hash
    v2_candidate_metadata = reopened.job_repository.latest_checkpoint(
        submitted.job_id, "assemble_candidate_bundle"
    )
    assert v2_candidate_metadata is not None
    assert v2_candidate_metadata.checkpoint_id != v1_candidate_metadata.checkpoint_id
    assert v2_candidate_metadata.input_hash == VideoService._candidate_assembly_input_hash(
        request_hash
    )
    committed = workspace_root / recovered.result.workspace_relative_bundle_path
    final_markdown = (
        committed
        / "drafts"
        / f"{recovered.result.primary_draft_artifact_id}.md"
    ).read_text(encoding="utf-8")
    assert spaced in final_markdown
    assert adjacent not in final_markdown
    assert final_markdown.count(f"[^{evidence_ids[0]}]") == 2
    assert final_markdown.count(f"[^{evidence_ids[1]}]") == 2
    with reopened.job_repository._connect() as connection:
        assembly_attempts = connection.execute(
            """
            SELECT COUNT(*) FROM attempts
            WHERE job_id = ? AND step_id = 'assemble_candidate_bundle'
            """,
            (submitted.job_id,),
        ).fetchone()[0]
    assert assembly_attempts == 2
    _validate_committed_bundle(workspace_root, recovered.result.bundle_id)


def test_unsupported_recipe_version_fails_preflight_without_external_work(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id="unsupported-recipe",
        recipe_version=999,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == "recipe_version_unsupported"
    assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0


def test_second_canonical_source_conflicts_before_new_bundle_commit(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()

    first = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="same-source-first")
        ).job_id
    )
    second = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="same-source-second")
        ).job_id
    )

    assert first.state is JobState.SUCCEEDED
    assert second.state is JobState.FAILED
    assert first.result is not None
    assert second.result is None
    assert second.error is not None
    assert second.error.code == "source_identity_conflict"
    committed = tuple((workspace_root / "raw" / "personal" / "bundles").iterdir())
    assert [item.name for item in committed] == [first.result.bundle_id]
    assert calls.commit == 1


def test_long_external_step_renews_scheduler_lease_cooperatively(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    now_ms = [1_000]

    def long_model_step(heartbeat: Callable[[], None]) -> None:
        now_ms[0] += 200_000
        heartbeat()
        now_ms[0] += 200_000

    calls = runtime_module.FakeCallCounts()
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "heartbeat-machine",
        calls=calls,
        clock=lambda: now_ms[0],
        operation_hooks={"model": long_model_step},
    )

    snapshot = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="long-model")
        ).job_id
    )

    assert snapshot.state is JobState.SUCCEEDED
    assert calls.download == calls.transcribe == calls.model == 1
    assert calls.commit == 1


def test_blocking_checkpoint_action_renews_scheduler_lease_in_background(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    now_ms = [1_000]
    background_heartbeat = threading.Event()

    def block_model_without_cooperative_heartbeat(
        _heartbeat: Callable[[], None],
    ) -> None:
        background_heartbeat.clear()
        now_ms[0] = 200_000
        assert background_heartbeat.wait(timeout=2)
        now_ms[0] = 400_000

    calls = runtime_module.FakeCallCounts()
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "background-heartbeat-machine",
        calls=calls,
        clock=lambda: now_ms[0],
        operation_hooks={"model": block_model_without_cooperative_heartbeat},
    )
    service = runtime.video_service
    service._heartbeat_interval_seconds = 0.01
    repository = runtime.job_repository
    original_heartbeat = repository.heartbeat_job_claim

    def observe_heartbeat(authority: object, *, ttl_seconds: int) -> object:
        renewed = original_heartbeat(authority, ttl_seconds=ttl_seconds)
        if threading.current_thread() is not threading.main_thread():
            background_heartbeat.set()
        return renewed

    repository.heartbeat_job_claim = observe_heartbeat

    snapshot = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="background-heartbeat")
        ).job_id
    )

    assert snapshot.state is JobState.SUCCEEDED
    assert calls.download == calls.transcribe == calls.model == 1
    assert calls.commit == 1
    assert _background_heartbeat_threads() == ()


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt))
def test_checkpoint_heartbeat_worker_stops_when_action_raises(
    tmp_path: Path,
    workspace_root: Path,
    failure_type: type[BaseException],
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    background_heartbeat = threading.Event()

    def fail_model_after_heartbeat(_heartbeat: Callable[[], None]) -> None:
        background_heartbeat.clear()
        assert background_heartbeat.wait(timeout=2)
        raise failure_type("action failed")

    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / f"heartbeat-{failure_type.__name__}",
        operation_hooks={"model": fail_model_after_heartbeat},
    )
    service = runtime.video_service
    service._heartbeat_interval_seconds = 0.01
    repository = runtime.job_repository
    original_heartbeat = repository.heartbeat_job_claim

    def observe_heartbeat(authority: object, *, ttl_seconds: int) -> object:
        renewed = original_heartbeat(authority, ttl_seconds=ttl_seconds)
        if threading.current_thread() is not threading.main_thread():
            background_heartbeat.set()
        return renewed

    repository.heartbeat_job_claim = observe_heartbeat
    submitted = runtime.submit_video(
        valid_request(
            workspace_root,
            client_request_id=f"heartbeat-{failure_type.__name__}",
        )
    )

    with pytest.raises(failure_type, match="action failed"):
        runtime.wait_job(submitted.job_id)

    assert _background_heartbeat_threads() == ()


@pytest.mark.parametrize("action_failure", (None, KeyboardInterrupt))
def test_fenced_background_heartbeat_prevents_checkpoint_and_commit(
    tmp_path: Path,
    workspace_root: Path,
    action_failure: type[BaseException] | None,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    now_ms = [1_000]
    heartbeat_failed = threading.Event()

    def fence_during_model(_heartbeat: Callable[[], None]) -> None:
        now_ms[0] = 302_000
        repository.claim_job(
            submitted.job_id,
            "replacement-owner",
            ttl_seconds=300,
        )
        assert heartbeat_failed.wait(timeout=2)
        if action_failure is not None:
            raise action_failure("control flow interrupted")

    calls = runtime_module.FakeCallCounts()
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "fenced-heartbeat-machine",
        calls=calls,
        clock=lambda: now_ms[0],
        operation_hooks={"model": fence_during_model},
    )
    service = runtime.video_service
    service._heartbeat_interval_seconds = 0.01
    repository = runtime.job_repository
    original_heartbeat = repository.heartbeat_job_claim

    def observe_heartbeat(authority: object, *, ttl_seconds: int) -> object:
        try:
            return original_heartbeat(authority, ttl_seconds=ttl_seconds)
        except BaseException:
            if threading.current_thread() is not threading.main_thread():
                heartbeat_failed.set()
            raise

    repository.heartbeat_job_claim = observe_heartbeat
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="fenced-heartbeat")
    )

    if action_failure is None:
        with pytest.raises(DomainError, match="job_claim_fenced"):
            runtime.wait_job(submitted.job_id)
    else:
        with pytest.raises(action_failure, match="control flow interrupted"):
            runtime.wait_job(submitted.job_id)

    assert repository.latest_checkpoint(submitted.job_id, "generate_draft") is None
    assert calls.commit == 0
    assert _background_heartbeat_threads() == ()


def test_takeover_of_running_generate_draft_leaves_no_running_replacement(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "takeover-machine"
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        owner_id="process-a",
        clock=lambda: 1_000,
    )
    submitted = first.submit_video(
        valid_request(workspace_root, client_request_id="takeover-model")
    )
    repository = first.job_repository
    old_authority = repository.claim_job(
        submitted.job_id,
        "process-a",
        ttl_seconds=300,
    ).authority
    abandoned = repository.start_attempt(
        repository.create_attempt(
            submitted.job_id,
            "generate_draft",
            authority=old_authority,
        ).attempt_id,
        old_authority,
    )

    del first
    calls = runtime_module.FakeCallCounts()
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        calls=calls,
        owner_id="process-b",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    with reopened.job_repository._connect() as connection:
        attempts = connection.execute(
            "SELECT attempt_id, state FROM attempts WHERE job_id = ?",
            (submitted.job_id,),
        ).fetchall()
    states = {row["attempt_id"]: row["state"] for row in attempts}
    assert states[abandoned.attempt_id] == "interrupted"
    assert "running" not in states.values()
    assert calls.model == 1


def test_concurrent_waits_on_one_runtime_execute_job_once(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    model_entered = threading.Event()
    release_model = threading.Event()

    def block_model(heartbeat: Callable[[], None]) -> None:
        heartbeat()
        model_entered.set()
        assert release_model.wait(timeout=5)

    runtime, calls = runtime_factory(operation_hooks={"model": block_model})
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="concurrent-wait")
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(runtime.wait_job, submitted.job_id)
        assert model_entered.wait(timeout=5)
        second = executor.submit(runtime.wait_job, submitted.job_id)
        release_model.set()
        snapshots = (first.result(timeout=10), second.result(timeout=10))

    assert all(item.state is JobState.SUCCEEDED for item in snapshots)
    assert calls.download == calls.transcribe == calls.model == 1
    assert calls.commit == 1


def test_job_store_busy_does_not_fail_video_job(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="job-store-busy")
    )

    def busy(*_args: object, **_kwargs: object) -> object:
        raise DomainError(
            "job_store_busy",
            ErrorCategory.RETRYABLE_RUNTIME,
            "The workspace JobStore is busy; retry the operation",
        )

    runtime.video_service._execute = busy

    with pytest.raises(DomainError, match="job_store_busy"):
        runtime.wait_job(submitted.job_id)

    assert runtime.get_job(submitted.job_id).state is JobState.RUNNING
    assert calls.download == calls.transcribe == calls.model == calls.commit == 0


def test_machine_lease_store_busy_does_not_fail_video_job(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    store = MachineResourceLeaseStore.open(tmp_path / "lease-store-busy")
    calls = runtime_module.FakeCallCounts()
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "lease-store-busy-machine",
        calls=calls,
        resource_lease_store=store,
        resource_owner=ResourceOwner(
            "video-workspace",
            "video-process",
            process_id=101,
        ),
    )
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="lease-store-busy")
    )

    def busy_heartbeat(*_args: object, **_kwargs: object) -> object:
        raise DomainError(
            "machine_lease_store_busy",
            ErrorCategory.RETRYABLE_RUNTIME,
            "The machine resource lease store is busy; retry the operation",
        )

    store._heartbeat = busy_heartbeat  # type: ignore[method-assign]
    runtime.video_service._execute = (
        lambda *_args, **_kwargs: runtime.video_service._heartbeat_resource_lease()
    )

    with pytest.raises(DomainError, match="machine_lease_store_busy"):
        runtime.wait_job(submitted.job_id)

    assert runtime.get_job(submitted.job_id).state is JobState.RUNNING
    assert calls.download == calls.transcribe == calls.model == calls.commit == 0


def test_video_holds_workspace_publish_slot_only_across_atomic_commit(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    store = MachineResourceLeaseStore.open(tmp_path / "publish-machine")
    coordinator = WorkspacePublishCoordinator(
        store,
        ResourceOwner("workspace-id", "video-publisher", process_id=101),
        workspace_root=workspace_root,
    )
    competing = ResourceOwner(
        "workspace-id",
        "other-publisher",
        process_id=202,
    )
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "publish-video-machine",
        workspace_publish_coordinator=coordinator,
    )
    delegate = runtime.video_service._portable

    class ObservePublishBoundary:
        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

        def prepare_candidate(self, *args: object, **kwargs: object) -> object:
            lease = store.acquire(
                coordinator.resource_name,
                competing,
                ttl_seconds=300,
            )
            assert lease.release()
            return delegate.prepare_candidate(*args, **kwargs)

        def commit_prepared(self, *args: object, **kwargs: object) -> object:
            with pytest.raises(DomainError, match="resource_busy"):
                store.acquire(
                    coordinator.resource_name,
                    competing,
                    ttl_seconds=300,
                )
            return delegate.commit_prepared(*args, **kwargs)

    runtime.video_service._portable = ObservePublishBoundary()
    completed = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="publish-boundary")
        ).job_id
    )

    assert completed.state is JobState.SUCCEEDED
    recovered = store.acquire(
        coordinator.resource_name,
        competing,
        ttl_seconds=300,
    )
    assert recovered.release()


def test_workspace_publish_busy_does_not_fail_video_job(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    store = MachineResourceLeaseStore.open(tmp_path / "publish-busy-machine")
    coordinator = WorkspacePublishCoordinator(
        store,
        ResourceOwner("workspace-id", "video-publisher", process_id=101),
        workspace_root=workspace_root,
    )
    competing = store.acquire(
        coordinator.resource_name,
        ResourceOwner("workspace-id", "other-publisher", process_id=202),
        ttl_seconds=300,
    )
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "publish-busy-video-machine",
        workspace_publish_coordinator=coordinator,
    )
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="publish-busy")
    )

    try:
        with pytest.raises(DomainError, match="resource_busy"):
            runtime.wait_job(submitted.job_id)
        assert runtime.get_job(submitted.job_id).state is JobState.RUNNING
    finally:
        assert competing.release()

    assert runtime.wait_job(submitted.job_id).state is JobState.SUCCEEDED


def test_distinct_jobs_on_one_runtime_execute_serially(
    tmp_path: Path,
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    second_workspace = tmp_path / "second-workspace"
    shutil.copytree(workspace_root, second_workspace)
    start = threading.Barrier(3)
    first_model_entered = threading.Event()
    release_first_model = threading.Event()
    active_lock = threading.Lock()
    active = 0
    max_active = 0

    def observe_model(_heartbeat: Callable[[], None]) -> None:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
            is_first = active == 1 and not first_model_entered.is_set()
        if is_first:
            first_model_entered.set()
            assert release_first_model.wait(timeout=5)
        with active_lock:
            active -= 1

    runtime, calls = runtime_factory(operation_hooks={"model": observe_model})
    jobs = (
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="serial-first")
        ),
        runtime.submit_video(
            replace(
                valid_request(
                    second_workspace,
                    client_request_id="serial-second",
                ),
                input_value="fixture://second-course",
            )
        ),
    )

    def wait(job_id: str) -> object:
        start.wait(timeout=5)
        return runtime.wait_job(job_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(wait, job.job_id) for job in jobs)
        start.wait(timeout=5)
        assert first_model_entered.wait(timeout=5)
        release_first_model.set()
        snapshots = tuple(future.result(timeout=15) for future in futures)

    assert all(snapshot.state is JobState.SUCCEEDED for snapshot in snapshots)
    assert calls.download == calls.transcribe == calls.model == 2
    assert calls.commit == 2
    assert max_active == 1


def test_machine_admission_keeps_competing_video_job_queued(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    entered = threading.Event()
    release = threading.Event()

    def block_model(heartbeat: Callable[[], None]) -> None:
        heartbeat()
        entered.set()
        assert release.wait(timeout=5)

    store = MachineResourceLeaseStore.open(tmp_path / "shared-machine")
    first_calls = runtime_module.FakeCallCounts()
    second_calls = runtime_module.FakeCallCounts()
    first = _create_fake_runtime(
        runtime_module,
        tmp_path / "workspace-machine-a",
        calls=first_calls,
        operation_hooks={"model": block_model},
        resource_lease_store=store,
        resource_owner=ResourceOwner("workspace-a", "process-a", process_id=101),
    )
    second = _create_fake_runtime(
        runtime_module,
        tmp_path / "workspace-machine-b",
        calls=second_calls,
        resource_lease_store=store,
        resource_owner=ResourceOwner("workspace-b", "process-b", process_id=202),
    )
    first_job = first.submit_video(
        valid_request(workspace_root, client_request_id="machine-admission-first")
    )
    second_workspace = tmp_path / "second-workspace-machine-admission"
    shutil.copytree(workspace_root, second_workspace)
    second_job = second.submit_video(
        replace(
            valid_request(
                second_workspace,
                client_request_id="machine-admission-second",
            ),
            input_value="fixture://second-course",
        )
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_wait = executor.submit(first.wait_job, first_job.job_id)
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(DomainError, match="resource_busy"):
                second.wait_job(second_job.job_id)
            assert second.get_job(second_job.job_id).state is JobState.QUEUED
            assert (
                second_calls.download
                == second_calls.transcribe
                == second_calls.model
                == second_calls.commit
                == 0
            )
        finally:
            release.set()
        assert first_wait.result(timeout=15).state is JobState.SUCCEEDED

    assert second.wait_job(second_job.job_id).state is JobState.SUCCEEDED
    assert second_calls.download == second_calls.transcribe == second_calls.model == 1
    assert second_calls.commit == 1


def test_live_job_claim_keeps_new_video_job_queued(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "scheduler-busy-machine",
        owner_id="waiting-process",
    )
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="scheduler-busy-queued")
    )
    authority = runtime.job_repository.claim_job(
        submitted.job_id,
        "blocking-process",
        ttl_seconds=300,
    ).authority

    try:
        with pytest.raises(DomainError, match="scheduler_busy"):
            runtime.wait_job(submitted.job_id)
        assert runtime.get_job(submitted.job_id).state is JobState.RUNNING
    finally:
        runtime.job_repository.release_job_claim(authority)


def test_machine_admission_heartbeat_fences_takeover_before_checkpoint(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_now_ms = [1_000]
    store = MachineResourceLeaseStore.open(
        tmp_path / "takeover-shared-machine",
        clock=lambda: machine_now_ms[0],
    )
    calls = runtime_module.FakeCallCounts()

    def lose_machine_lease(_heartbeat: Callable[[], None]) -> None:
        machine_now_ms[0] = 302_000
        store.acquire(
            "produce:heavy:v1",
            ResourceOwner("workspace-b", "process-b", process_id=202),
            ttl_seconds=300,
        )

    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "takeover-workspace-machine",
        calls=calls,
        operation_hooks={"model": lose_machine_lease},
        resource_lease_store=store,
        resource_owner=ResourceOwner("workspace-a", "process-a", process_id=101),
    )
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="machine-takeover")
    )

    failed = runtime.wait_job(submitted.job_id)

    assert failed.state is JobState.FAILED
    assert failed.error is not None
    assert failed.error.code == "resource_lease_lost"
    assert runtime.job_repository.latest_checkpoint(
        submitted.job_id,
        "generate_draft",
    ) is None
    assert calls.model == 1
    assert calls.commit == 0


def test_machine_takeover_after_prepare_fences_video_before_commit(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_now_ms = [1_000]
    store = MachineResourceLeaseStore.open(
        tmp_path / "commit-takeover-shared-machine",
        clock=lambda: machine_now_ms[0],
    )
    competing_owner = ResourceOwner(
        "workspace-b",
        "process-b",
        process_id=202,
    )

    class TakeOverAfterPrepare:
        def __init__(self) -> None:
            self._delegate = IWikiPortableGateway()

        def __getattr__(self, name: str) -> object:
            return getattr(self._delegate, name)

        def prepare_candidate(self, *args: object, **kwargs: object) -> object:
            prepared = self._delegate.prepare_candidate(*args, **kwargs)
            machine_now_ms[0] = 302_000
            store.acquire(
                "produce:heavy:v1",
                competing_owner,
                ttl_seconds=300,
            )
            return prepared

    calls = runtime_module.FakeCallCounts()
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "commit-takeover-workspace-machine",
        calls=calls,
        resource_lease_store=store,
        resource_owner=ResourceOwner(
            "workspace-a",
            "process-a",
            process_id=101,
        ),
    )
    runtime.video_service._portable = TakeOverAfterPrepare()
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="machine-commit-takeover")
    )

    failed = runtime.wait_job(submitted.job_id)

    assert failed.state is JobState.FAILED
    assert failed.result is None
    assert failed.error is not None
    assert failed.error.code == "resource_lease_lost"
    assert calls.commit == 0
    assert not tuple(workspace_root.rglob("commit.json"))


def test_blocking_action_renews_machine_admission_beyond_ttl(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_now_ms = [1_000]
    heartbeat_observed = threading.Event()
    store = MachineResourceLeaseStore.open(
        tmp_path / "heartbeat-shared-machine",
        clock=lambda: machine_now_ms[0],
    )
    competing_store = MachineResourceLeaseStore.open(
        tmp_path / "heartbeat-shared-machine",
        clock=lambda: machine_now_ms[0],
    )
    original_heartbeat = store._heartbeat

    def observe_heartbeat(*args: object, **kwargs: object) -> object:
        renewed = original_heartbeat(*args, **kwargs)
        if threading.current_thread() is not threading.main_thread():
            heartbeat_observed.set()
        return renewed

    store._heartbeat = observe_heartbeat  # type: ignore[method-assign]

    def block_model(_heartbeat: Callable[[], None]) -> None:
        heartbeat_observed.clear()
        machine_now_ms[0] = 200_000
        assert heartbeat_observed.wait(timeout=2)
        machine_now_ms[0] = 400_000
        with pytest.raises(DomainError, match="resource_busy"):
            competing_store.acquire(
                "produce:heavy:v1",
                ResourceOwner("workspace-b", "process-b", process_id=202),
                ttl_seconds=300,
            )

    calls = runtime_module.FakeCallCounts()
    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "heartbeat-workspace-machine",
        calls=calls,
        operation_hooks={"model": block_model},
        resource_lease_store=store,
        resource_owner=ResourceOwner("workspace-a", "process-a", process_id=101),
    )
    runtime.video_service._heartbeat_interval_seconds = 0.01

    completed = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="machine-heartbeat")
        ).job_id
    )

    assert completed.state is JobState.SUCCEEDED
    assert calls.model == calls.commit == 1
    next_lease = competing_store.acquire(
        "produce:heavy:v1",
        ResourceOwner("workspace-b", "process-b", process_id=202),
        ttl_seconds=300,
    )
    assert next_lease.release()


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt))
def test_machine_admission_is_released_after_execution_abort(
    tmp_path: Path,
    workspace_root: Path,
    failure_type: type[BaseException],
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    store = MachineResourceLeaseStore.open(tmp_path / "abort-shared-machine")

    def abort_model(_heartbeat: Callable[[], None]) -> None:
        raise failure_type("injected execution abort")

    runtime = _create_fake_runtime(
        runtime_module,
        tmp_path / "abort-workspace-machine",
        operation_hooks={"model": abort_model},
        resource_lease_store=store,
        resource_owner=ResourceOwner("workspace-a", "process-a", process_id=101),
    )
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="machine-abort")
    )

    with pytest.raises(failure_type, match="execution abort"):
        runtime.wait_job(submitted.job_id)

    recovered = store.acquire(
        "produce:heavy:v1",
        ResourceOwner("workspace-b", "process-b", process_id=202),
        ttl_seconds=300,
    )
    assert recovered.release()


def test_video_worker_consumes_adopted_resource_and_exact_job_authority(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "video-adopted-machine"
    submitted_runtime = _create_fake_runtime(runtime_module, machine_root)
    submitted = submitted_runtime.submit_video(
        valid_request(workspace_root, client_request_id="video-adopted-authority")
    )
    store = MachineResourceLeaseStore.open(tmp_path / "video-adopted-resource")
    supervisor = ResourceOwner("workspace-a", "engine-supervisor", process_id=101)
    worker = ResourceOwner("workspace-a", "engine-worker", process_id=202)
    source_lease = store.acquire("produce:heavy:v1", supervisor, ttl_seconds=300)
    adopted = store.adopt(
        store.handoff(source_lease, worker, ttl_seconds=300),
        ttl_seconds=300,
    )
    authority = submitted_runtime.job_repository.claim_job(
        submitted.job_id,
        worker.process_instance_id,
        ttl_seconds=300,
    ).authority
    calls = runtime_module.FakeCallCounts()
    worker_runtime = _create_fake_runtime(
        runtime_module,
        machine_root,
        calls=calls,
        adopted_resource_lease=adopted,
        expected_job_authority=authority,
    )

    assert worker_runtime.wait_job(submitted.job_id).state is JobState.SUCCEEDED
    assert calls.download == calls.transcribe == calls.model == calls.commit == 1
    next_lease = store.acquire(
        "produce:heavy:v1",
        ResourceOwner("workspace-b", "other-worker", process_id=303),
        ttl_seconds=300,
    )
    assert next_lease.release()


def test_video_runtime_rejects_partial_or_mismatched_machine_admission(
    tmp_path: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    store = MachineResourceLeaseStore.open(tmp_path / "invariant-shared-machine")
    owner = ResourceOwner("workspace-a", "process-a", process_id=101)

    with pytest.raises(ValueError, match="resource_admission_pair_required"):
        _create_fake_runtime(
            runtime_module,
            tmp_path / "missing-owner-machine",
            resource_lease_store=store,
        )
    with pytest.raises(ValueError, match="resource_admission_pair_required"):
        _create_fake_runtime(
            runtime_module,
            tmp_path / "missing-store-machine",
            resource_owner=owner,
        )
    with pytest.raises(ValueError, match="resource_admission_owner_mismatch"):
        _create_fake_runtime(
            runtime_module,
            tmp_path / "mismatched-owner-machine",
            owner_id="different-process",
            resource_lease_store=store,
            resource_owner=owner,
        )


def test_submitted_job_survives_independent_runtime_wait(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "queued-reopen-machine"
    calls = runtime_module.FakeCallCounts()
    first = _create_fake_runtime(
        runtime_module,
        machine_root,
        calls=calls,
        owner_id="submit-process",
    )

    submitted = first.submit_video(
        valid_request(workspace_root, client_request_id="queued-reopen")
    )

    assert submitted.state is JobState.QUEUED
    assert calls.download == calls.transcribe == calls.model == calls.commit == 0

    del first
    reopened = _create_fake_runtime(
        runtime_module,
        machine_root,
        calls=calls,
        owner_id="wait-process",
    )
    completed = reopened.wait_job(submitted.job_id)

    assert completed.state is JobState.SUCCEEDED
    assert calls.download == calls.transcribe == calls.model == 1
    assert calls.commit == 1


def test_candidate_checkpoint_decode_rejects_malformed_control_payloads(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, _ = runtime_factory()
    snapshot = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="strict-candidate")
        ).job_id
    )
    assert snapshot.state is JobState.SUCCEEDED
    metadata = runtime.job_repository.latest_checkpoint(
        snapshot.job_id, "assemble_candidate_bundle"
    )
    assert metadata is not None
    payload_path = (
        Path(runtime.job_repository.database_path).parent.parent
        / "attempts"
        / metadata.relative_path
    )
    original = json.loads(payload_path.read_text(encoding="utf-8"))

    invalid_payloads: list[dict[str, object]] = []
    for key, value in (
        ("extra", "not-allowed"),
        ("publish_eligible", 1),
        ("display_asset_ids", "art_not-a-list"),
        ("warnings", [1]),
        ("usage", {"input_tokens": 1, "cost_micros": 1}),
        ("bundle_id", "bnd_invalid"),
        ("manifest_sha256", "sha256:" + "A" * 64),
        ("staging_relative_path", "../outside"),
    ):
        mutated = dict(original)
        mutated[key] = value
        invalid_payloads.append(mutated)

    for invalid in invalid_payloads:
        with pytest.raises(Exception, match="candidate_checkpoint_invalid"):
            _CandidateCheckpoint.decode(json.dumps(invalid).encode("utf-8"))
