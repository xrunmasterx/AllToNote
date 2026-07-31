from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import app.runtime as runtime_module
import app.core.portable.bundle_assembler as bundle_assembler_module
from app.adapters.models.legacy_gpt import (
    LegacyModelBinding,
    LegacyModelCapabilities,
    LegacyModelResponse,
)
from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.screenshots.ffmpeg import FFmpegScreenshotAdapter
from app.adapters.sources.legacy_video import LegacyVideoSourceAdapter
from app.core.application import video_acquisition
from app.core.application.video_checkpoints import (
    CandidateCheckpoint,
    decode_acquired,
    decode_draft,
    decode_screenshots,
    encode_screenshots,
)
from app.core.application.video_checkpoints import decode_source
from app.core.application.video_service import VideoService
from app.core.domain.ids import sha256_digest
from app.core.domain.video import (
    JobState,
    ScreenshotPolicy,
    TranscriptDocument,
    TranscriptSegment,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import ExternalOperationGuard
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.portable.bundle_assembler import DisplayAssetInput
from app.core.portable.quality import evaluate_video_draft
from app.core.ports.transcript import MediaInput


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
WORKSPACE_FIXTURE = FIXTURE_ROOT / "workspace-v2"
LOCAL_METADATA = {
    "title": "Local fixture course",
    "author": "AllToNote",
    "channel": "Local files",
    "duration_ms": 2_000,
    "published_at": None,
    "observed_at": "2026-07-15T00:00:00.000Z",
    "language": "en",
}


@dataclass
class Calls:
    resolve: int = 0
    acquire: int = 0
    transcriber: int = 0
    model: int = 0
    commit: int = 0
    ffmpeg: int = 0


def _record(path: Path | None, operation: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"operation": operation}, separators=(",", ":")))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class _TranscriptFake:
    def __init__(self, calls: Calls, call_log: Path | None = None) -> None:
        self._calls = calls
        self._call_log = call_log

    def transcribe(self, media: MediaInput, token: object) -> TranscriptDocument:
        assert media.media_path is not None
        assert media.provided_transcript is None
        assert media.media_path.is_file()
        token.raise_if_cancelled()
        self._calls.transcriber += 1
        _record(self._call_log, "transcriber")
        return TranscriptDocument(
            language="en",
            segments=(
                TranscriptSegment(
                    "seg_000001",
                    0,
                    2_000,
                    "AllToNote turns local video into cited knowledge.",
                ),
            ),
        )


class _PathLeakingTranscript:
    def transcribe(self, media: MediaInput, token: object) -> TranscriptDocument:
        del token
        assert media.media_path is not None
        leaked = str(media.media_path.resolve())
        raise DomainError(
            "provider_path_leak",
            ErrorCategory.RECIPE_FAILED,
            f"provider failed for {leaked}",
            {"nested": {"snapshot_path": leaked}},
        )


class _SemanticErrorTranscript:
    def __init__(
        self,
        code: str,
        category: ErrorCategory,
        original_path: Path,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.original_path = original_path
        self.owner_id = owner_id
        self.calls = 0
        self.repository: object | None = None

    def bind_repository(self, repository: object) -> None:
        self.repository = repository

    def transcribe(self, media: MediaInput, token: object) -> TranscriptDocument:
        assert media.media_path is not None
        self.calls += 1
        snapshot_path = media.media_path.resolve()
        if self.code == "external_outcome_unknown":
            assert self.repository is not None
            assert self.owner_id is not None
            job_id = token._job_id
            _, attempt, _ = self.repository.get_job_details(job_id)
            assert attempt is not None
            guard = ExternalOperationGuard(
                self.repository,
                ExecutionAuthority(self.owner_id, attempt.fencing_token),
            )
            operation = guard.prepare(
                job_id=job_id,
                step_id=attempt.step_id,
                attempt_id=attempt.attempt_id,
                provider="fixture/transcriber-v1",
                request_hash=sha256_digest(b"local-transcription-request"),
                summary_json="{}",
                max_attempts=1,
            )
            guard.start(operation.operation_id)
            guard.unknown(operation.operation_id, summary_json="{}")
        raise DomainError(
            self.code,
            self.category,
            f"adapter failed for {self.original_path}",
            {
                "nested": {
                    "source_path": str(self.original_path),
                    "snapshot_paths": [str(snapshot_path)],
                }
            },
        )


class _RecordingSource:
    def __init__(
        self,
        delegate: LegacyVideoSourceAdapter,
        calls: Calls,
        call_log: Path | None,
    ) -> None:
        self._delegate = delegate
        self._calls = calls
        self._call_log = call_log

    def resolve(self, input_value: str) -> object:
        self._calls.resolve += 1
        _record(self._call_log, "source_resolve")
        return self._delegate.resolve(input_value)

    def acquire(self, *args: object, **kwargs: object) -> object:
        self._calls.acquire += 1
        _record(self._call_log, "source_acquire")
        return self._delegate.acquire(*args, **kwargs)


class _RecordingPortableGateway:
    def __init__(
        self,
        delegate: IWikiPortableGateway,
        calls: Calls,
        call_log: Path | None,
    ) -> None:
        self._delegate = delegate
        self._calls = calls
        self._call_log = call_log

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def commit_prepared(self, prepared: object) -> object:
        result = self._delegate.commit_prepared(prepared)
        self._calls.commit += 1
        _record(self._call_log, "portable_commit")
        return result


class _Completion:
    def __init__(self, calls: Calls, call_log: Path | None = None) -> None:
        self._calls = calls
        self._call_log = call_log

    def complete_once(self, prompt: str, *, check_cancelled=None) -> LegacyModelResponse:
        if check_cancelled is not None:
            check_cancelled()
        assert "seg_000001" in prompt
        self._calls.model += 1
        _record(self._call_log, "model")
        return LegacyModelResponse(
            markdown=(
                "# Local video note\n\n"
                "The local source becomes cited knowledge.[^seg_000001]\n"
            ),
            provider_request_id="local-fixture-request",
            input_tokens=20,
            output_tokens=10,
            actual_model="fixture/model-v1",
        )


class _ScreenshotCompletion(_Completion):
    def complete_once(self, prompt: str, *, check_cancelled=None) -> LegacyModelResponse:
        response = super().complete_once(
            prompt,
            check_cancelled=check_cancelled,
        )
        return replace(
            response,
            markdown=(
                "# Local video note\n\n"
                "The local source becomes cited knowledge.[^seg_000001]\n\n"
                "[SCREENSHOT:seg_000001]\n"
            ),
        )


class _ScreenshotProcess:
    pid = 4321

    def __init__(self, output: Path) -> None:
        self.output = output

    def poll(self) -> int:
        self.output.write_bytes(
            bytes.fromhex(
                "524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"
            )
        )
        return 0

    def wait(self, timeout: float) -> int:
        del timeout
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


class _ScreenshotProcessFactory:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> _ScreenshotProcess:
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        self.calls.ffmpeg += 1
        self.argv.append(list(argv))
        return _ScreenshotProcess(Path(argv[-1]))


class _CrashOnceScreenshotProcess(_ScreenshotProcess):
    def poll(self) -> int:
        raise SystemExit("injected screenshot process loss")


class _CrashOnceScreenshotProcessFactory(_ScreenshotProcessFactory):
    def __init__(self, calls: Calls) -> None:
        super().__init__(calls)
        self._crashed = False

    def __call__(self, argv: list[str], **kwargs: object) -> _ScreenshotProcess:
        process = super().__call__(argv, **kwargs)
        if not self._crashed:
            self._crashed = True
            return _CrashOnceScreenshotProcess(process.output)
        return process


class _RecordingScreenshotAdapter:
    def __init__(self, delegate: object, plans: list[tuple[object, ...]]) -> None:
        self._delegate = delegate
        self._plans = plans

    def extract(self, plan: tuple[object, ...], *args: object, **kwargs: object) -> object:
        self._plans.append(tuple(plan))
        return self._delegate.extract(plan, *args, **kwargs)


class _TerminateBeforeModel:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def generate_draft(self, *_args: object, **_kwargs: object) -> object:
        os._exit(23)


class _CorruptSnapshotBeforeTranscript:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def acquire(self, *args: object, **kwargs: object) -> object:
        acquired = self._delegate.acquire(*args, **kwargs)
        stored = acquired.stored_media
        assert stored is not None
        target = self._delegate._storage.root / Path(stored.relative_locator)
        target.write_bytes(b"corrupted-after-publication")
        return acquired


class _RuntimeHarness:
    def __init__(self, runtime: object, video_service: VideoService) -> None:
        self.runtime = runtime
        self.video_service = video_service

    def __getattr__(self, name: str) -> object:
        return getattr(self.runtime, name)


def _create_runtime(
    machine_root: Path,
    *,
    calls: Calls | None = None,
    call_log: Path | None = None,
    owner_id: str | None = None,
    now_ms: int | None = None,
    transcriber: object | None = None,
    screenshot_process_factory: _ScreenshotProcessFactory | None = None,
) -> object:
    factory = getattr(runtime_module, "_create_local_video_runtime_components", None)
    assert callable(factory), "Task 16A.1 local runtime composition is missing"
    observed = calls or Calls()
    source = _RecordingSource(
        LegacyVideoSourceAdapter(local_machine_id="fixture-machine"),
        observed,
        call_log,
    )
    model = LegacyModelBinding(
        provider_kind="fixture/provider-v1",
        model_identity="fixture/model-v1",
        bridge=(
            _ScreenshotCompletion(observed, call_log)
            if screenshot_process_factory is not None
            else _Completion(observed, call_log)
        ),
        capabilities=LegacyModelCapabilities(
            screenshot_requests=screenshot_process_factory is not None
        ),
    )
    runtime, service = factory(
        machine_root,
        source=source,
        source_metadata={"local": LOCAL_METADATA},
        transcriber=transcriber or _TranscriptFake(observed, call_log),
        model=model,
        owner_id=owner_id,
        clock=(None if now_ms is None else lambda: now_ms),
        screenshot_adapter_factory=(
            None
            if screenshot_process_factory is None
            else lambda storage, repository: FFmpegScreenshotAdapter(
                storage,
                repository,
                ffmpeg_executable="injected-ffmpeg",
                process_factory=screenshot_process_factory,
                taskkill_factory=lambda *_args, **_kwargs: None,
                platform_name="windows",
            )
        ),
    )
    service._portable = _RecordingPortableGateway(
        service._portable,
        observed,
        call_log,
    )
    return _RuntimeHarness(runtime, service)


def _request(
    source: Path,
    workspace: Path,
    request_id: str,
    *,
    provided_transcript: TranscriptDocument | None = None,
    screenshot_policy: ScreenshotPolicy = ScreenshotPolicy.OFF,
) -> VideoProduceRequest:
    return VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace,
        input_value=str(source),
        client_request_id=request_id,
        provided_transcript=provided_transcript,
        screenshot_policy=screenshot_policy,
    )


def _checkpoint_payload(runtime: object, job_id: str, step_id: str) -> bytes:
    metadata = runtime.job_repository.latest_checkpoint(job_id, step_id)
    assert metadata is not None
    return (
        runtime.job_repository.machine_root.parent
        / "attempts"
        / metadata.relative_path
    ).read_bytes()


def _terminate_after_transcript(
    machine_root: str,
    job_id: str,
    call_log: str,
) -> None:
    runtime = _create_runtime(
        Path(machine_root),
        call_log=Path(call_log),
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


@pytest.fixture
def local_video(tmp_path: Path) -> Path:
    path = tmp_path / "private course.mp4"
    path.write_bytes(b"deterministic-local-media-fixture")
    return path


def test_local_runtime_and_stored_media_contract_are_available() -> None:
    assert callable(getattr(runtime_module, "create_local_video_runtime", None))
    assert callable(getattr(video_acquisition, "AttemptStoredAsset", None))


def test_local_video_copies_and_transcribes_once_then_commits_private_safe_bundle(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    runtime = _create_runtime(tmp_path / "machine", calls=calls)
    submitted = runtime.submit_video(_request(local_video, workspace_root, "local-success"))

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert calls.transcriber == 1
    assert calls.model == 1
    assets = tuple((tmp_path / "machine" / "attempts").rglob("source_media.*"))
    assert len(assets) == 1
    acquisition_payload = _checkpoint_payload(runtime, submitted.job_id, "acquire")
    source_payload = _checkpoint_payload(runtime, submitted.job_id, "resolve_source")
    original_absolute = str(local_video.resolve()).encode()
    snapshot_absolute = str(assets[0].resolve()).encode()
    assert original_absolute not in acquisition_payload
    assert snapshot_absolute not in acquisition_payload
    assert original_absolute not in source_payload
    assert snapshot_absolute not in source_payload
    resolved_checkpoint = decode_source(source_payload)
    assert resolved_checkpoint.local_binding is None
    assert resolved_checkpoint.logical_reference is not None
    acquired = decode_acquired(acquisition_payload)
    stored = acquired.stored_media
    assert stored is not None
    acquisition_metadata = runtime.job_repository.latest_checkpoint(
        submitted.job_id, "acquire"
    )
    assert acquisition_metadata is not None
    assert stored.relative_locator.startswith(
        f"jobs/{submitted.job_id}/attempts/{acquisition_metadata.attempt_id}/assets/"
    )
    assert stored.sha256.startswith("sha256:")
    assert stored.byte_length == local_video.stat().st_size
    assert stored.role.value == "source_media"
    acquisition_json = json.loads(acquisition_payload)
    assert set(acquisition_json["stored_media"]) == {
        "relative_locator",
        "sha256",
        "byte_length",
        "role",
    }
    nested_unknown = json.loads(acquisition_payload)
    nested_unknown["stored_media"]["unexpected"] = "rejected"
    with pytest.raises(DomainError, match="Checkpoint content is invalid"):
        decode_acquired(
            json.dumps(nested_unknown, separators=(",", ":"), sort_keys=True).encode()
        )

    bundle = workspace_root / result.result.workspace_relative_bundle_path
    portable_bytes = b"\n".join(
        path.read_bytes() for path in bundle.rglob("*") if path.is_file()
    )
    assert original_absolute not in portable_bytes
    assert snapshot_absolute not in portable_bytes
    assert b'"kind":"external_local"' in portable_bytes
    assert b"urn:alltonote:local-content:sha256:" in portable_bytes
    assert b'"kind":"reference_only"' not in (
        bundle / "sources" / "video-metadata.json"
    ).read_bytes()
    manifest = json.loads((bundle / "bundle.json").read_text("utf-8"))
    source_id = manifest["sources"][0]["source_id"]
    materialization = manifest["source_revisions"][0]["materialization"]
    assert materialization == {
        "kind": "external_local",
        "external_ref_id": f"ext_{source_id.removeprefix('src_')}",
    }


def test_valid_local_screenshot_uses_snapshot_once_and_checkpoints_verified_webp(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    process_factory = _ScreenshotProcessFactory(calls)
    runtime = _create_runtime(
        tmp_path / "screenshot-machine",
        calls=calls,
        screenshot_process_factory=process_factory,
    )
    storage = runtime.video_service._attempt_storage
    original_resolve = storage.resolve_asset
    resolves = 0

    def resolve_once(*args: object, **kwargs: object) -> Path:
        nonlocal resolves
        resolves += 1
        return original_resolve(*args, **kwargs)

    storage.resolve_asset = resolve_once
    submitted = runtime.submit_video(
        _request(
            local_video,
            workspace_root,
            "local-screenshot",
            provided_transcript=TranscriptDocument(
                language="en",
                segments=(
                    TranscriptSegment(
                        "seg_000001", 0, 2_000, "provided screenshot text"
                    ),
                ),
            ),
            screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        )
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert calls.ffmpeg == 1
    assert resolves == 1
    assets = decode_screenshots(
        _checkpoint_payload(runtime, submitted.job_id, "optional_screenshots")
    )
    assert len(assets) == 1
    assert assets[0].payload == bytes.fromhex(
        "524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"
    )
    assert result.result.display_asset_ids == (assets[0].artifact_id,)
    bundle = workspace_root / result.result.workspace_relative_bundle_path
    draft_path = (
        bundle
        / "drafts"
        / f"{result.result.primary_draft_artifact_id}.md"
    )
    draft_bytes = draft_path.read_bytes()
    assert (
        f"![Video screenshot 1 at 00:00.000](../{assets[0].relative_path})\n".encode()
        in draft_bytes
    )
    assert b"[SCREENSHOT:" not in draft_bytes
    assert b"/static/screenshots" not in draft_bytes
    assert str(local_video.resolve()).encode() not in draft_bytes
    manifest = json.loads((bundle / "bundle.json").read_bytes())
    asset_documents = [
        artifact
        for artifact in manifest["artifacts"]
        if artifact["artifact_id"] == assets[0].artifact_id
    ]
    assert len(asset_documents) == 1
    assert asset_documents[0]["artifact_type"] == "evidence.asset.v1"
    assert asset_documents[0]["payload"]["path"] == assets[0].relative_path
    assert (bundle / assets[0].relative_path).read_bytes() == assets[0].payload
    quality = json.loads(
        (
            bundle
            / "quality"
            / f"{result.result.quality_report_artifact_id}.json"
        ).read_bytes()
    )
    assert quality["subject"]["sha256"] == sha256_digest(draft_bytes)
    assert not tuple((tmp_path / "screenshot-machine" / "attempts").rglob("*.partial.webp"))


def test_screenshot_cleanup_failure_never_checkpoints_accepted_asset(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    process_factory = _ScreenshotProcessFactory(calls)
    runtime = _create_runtime(
        tmp_path / "screenshot-cleanup-failure-machine",
        calls=calls,
        screenshot_process_factory=process_factory,
    )
    storage = runtime.video_service._operations._storage

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("private cleanup path")

    storage.cleanup_screenshot_output = fail_cleanup
    submitted = runtime.submit_video(
        _request(
            local_video,
            workspace_root,
            "local-screenshot-cleanup-failure",
            screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        )
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "screenshot_io_failed"
    assert runtime.job_repository.latest_checkpoint(
        submitted.job_id, "optional_screenshots"
    ) is None


def test_restart_after_screenshot_checkpoint_does_not_run_ffmpeg_again(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    process_factory = _ScreenshotProcessFactory(calls)
    machine = tmp_path / "screenshot-replay-machine"
    first = _create_runtime(
        machine,
        calls=calls,
        owner_id="first-screenshot-owner",
        now_ms=1_000,
        screenshot_process_factory=process_factory,
    )
    submitted = first.submit_video(
        _request(
            local_video,
            workspace_root,
            "local-screenshot-replay",
            screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        )
    )
    service = first.video_service
    original_assemble = service._assemble
    linked_drafts: list[bytes] = []
    linked_asset_ids: list[tuple[str, ...]] = []

    def crash_after_screenshot_checkpoint(value: object) -> object:
        service._assemble = original_assemble
        linked_drafts.append(value.quality.final_draft)
        linked_asset_ids.append(tuple(asset.artifact_id for asset in value.display_assets))
        raise RuntimeError("injected post-screenshot crash")

    service._assemble = crash_after_screenshot_checkpoint

    with pytest.raises(RuntimeError, match="post-screenshot crash"):
        first.wait_job(submitted.job_id)

    assert first.job_repository.latest_checkpoint(
        submitted.job_id, "optional_screenshots"
    ) is not None
    assert calls.ffmpeg == 1
    recovered = _create_runtime(
        machine,
        calls=calls,
        owner_id="second-screenshot-owner",
        now_ms=302_001,
        screenshot_process_factory=process_factory,
    )

    result = recovered.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert calls.ffmpeg == 1
    assert result.result.display_asset_ids == linked_asset_ids[0]
    recovered_bundle = workspace_root / result.result.workspace_relative_bundle_path
    recovered_draft = (
        recovered_bundle
        / "drafts"
        / f"{result.result.primary_draft_artifact_id}.md"
    ).read_bytes()
    assert recovered_draft == linked_drafts[0]
    assert f"../assets/{linked_asset_ids[0][0]}.webp".encode() in recovered_draft


def test_plan_mismatched_screenshot_checkpoint_fails_before_candidate_commit(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    process_factory = _ScreenshotProcessFactory(calls)
    runtime = _create_runtime(
        tmp_path / "screenshot-binding-mismatch-machine",
        calls=calls,
        screenshot_process_factory=process_factory,
    )
    submitted = runtime.submit_video(
        _request(
            local_video,
            workspace_root,
            "local-screenshot-binding-mismatch",
            screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        )
    )
    service = runtime.video_service
    original_assemble = service._assemble

    def crash_after_screenshot_checkpoint(value: object) -> object:
        del value
        service._assemble = original_assemble
        raise RuntimeError("injected post-screenshot crash")

    service._assemble = crash_after_screenshot_checkpoint
    with pytest.raises(RuntimeError, match="post-screenshot crash"):
        runtime.wait_job(submitted.job_id)

    checkpoint = runtime.job_repository.latest_checkpoint(
        submitted.job_id, "optional_screenshots"
    )
    assert checkpoint is not None
    original_reader = service._checkpoint_reader
    verified = decode_screenshots(original_reader(checkpoint))
    assert len(verified) == 1
    private_id = "art_018f0000-0000-7000-8000-000000000999"
    forged = DisplayAssetInput(
        artifact_id=private_id,
        relative_path=f"assets/{private_id}.webp",
        media_type="image/webp",
        payload=verified[0].payload,
    )

    def forged_reader(metadata: object) -> bytes:
        if metadata.step_id == "optional_screenshots":
            return encode_screenshots((forged,))
        return original_reader(metadata)

    service._checkpoint_reader = forged_reader

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.result is None
    assert result.error is not None
    assert result.error.code == "screenshot_asset_binding_invalid"
    assert private_id not in result.error.message
    assert dict(result.error.details) == {}
    assert calls.ffmpeg == 1
    assert runtime.job_repository.latest_checkpoint(
        submitted.job_id, "assemble_candidate_bundle"
    ) is None
    assert tuple((workspace_root / "raw" / "personal" / ".staging").iterdir()) == ()
    bundle_id = VideoService._ids(submitted.job_id)["bundle"]
    assert not (
        workspace_root / "raw" / "personal" / "bundles" / bundle_id
    ).exists()


def test_pre16a3_candidate_checkpoint_is_rebuilt_without_repeating_paid_work(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    process_factory = _ScreenshotProcessFactory(calls)
    machine = tmp_path / "candidate-version-machine"
    request = _request(
        local_video,
        workspace_root,
        "pre16a3-candidate-recovery",
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )
    first = _create_runtime(
        machine,
        calls=calls,
        owner_id="candidate-version-first",
        now_ms=1_000,
        screenshot_process_factory=process_factory,
    )
    submitted = first.submit_video(request)
    service = first.video_service
    original_assemble = service._assemble
    original_validate = service._validate_candidate

    def assemble_pre16a3_candidate(bundle_input: object) -> CandidateCheckpoint:
        service._assemble = original_assemble
        durable_draft = decode_draft(
            _checkpoint_payload(first, submitted.job_id, "generate_draft")
        )
        quality = evaluate_video_draft(
            durable_draft,
            bundle_input.evidence_set,
            draft_bundle_id=bundle_input.bundle_id,
            draft_artifact_id=bundle_input.artifact_ids.primary_draft,
        )
        old_location = service._portable.candidate_location(
            workspace_root,
            local_instance_id=service._local_instance_id,
            nonce=submitted.job_id.removeprefix("job_").replace("-", ""),
        )
        old_input = replace(
            bundle_input,
            quality=quality,
            location=old_location,
        )
        original_collector = bundle_assembler_module.rendered_image_bundle_paths
        bundle_assembler_module.rendered_image_bundle_paths = (
            lambda *_args, **_kwargs: tuple(
                asset.relative_path for asset in old_input.display_assets
            )
        )
        try:
            return original_assemble(old_input)
        finally:
            bundle_assembler_module.rendered_image_bundle_paths = original_collector

    def crash_after_old_candidate(
        _request_value: object,
        _checkpoint: object,
    ) -> object:
        service._validate_candidate = original_validate
        raise RuntimeError("injected after pre16a3 candidate checkpoint")

    service._assemble = assemble_pre16a3_candidate
    service._validate_candidate = crash_after_old_candidate

    with pytest.raises(RuntimeError, match="pre16a3 candidate checkpoint"):
        first.wait_job(submitted.job_id)

    old_metadata = first.job_repository.latest_checkpoint(
        submitted.job_id, "assemble_candidate_bundle"
    )
    assert old_metadata is not None
    old_checkpoint = CandidateCheckpoint.decode(
        _checkpoint_payload(first, submitted.job_id, "assemble_candidate_bundle")
    )
    old_candidate = workspace_root / old_checkpoint.staging_relative_path
    old_draft = (
        old_candidate
        / "drafts"
        / f"{old_checkpoint.primary_draft_artifact_id}.md"
    ).read_bytes()
    assert b"## Screenshots" not in old_draft
    original_assets = decode_screenshots(
        _checkpoint_payload(first, submitted.job_id, "optional_screenshots")
    )
    assert len(original_assets) == 1
    old_request_hash = VideoService._request_hash(request)
    with first.job_repository._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE checkpoints SET input_hash = ? WHERE checkpoint_id = ?",
            (old_request_hash, old_metadata.checkpoint_id),
        )

    del first
    second = _create_runtime(
        machine,
        calls=calls,
        owner_id="candidate-version-second",
        now_ms=302_001,
        screenshot_process_factory=process_factory,
    )
    second_service = second.video_service
    original_after_commit = second_service._operations.after_portable_commit

    def crash_after_rename(result: object) -> None:
        second_service._operations.after_portable_commit = original_after_commit
        original_after_commit(result)
        raise RuntimeError("injected after rebuilt candidate rename")

    second_service._operations.after_portable_commit = crash_after_rename

    try:
        unexpected = second.wait_job(submitted.job_id)
    except RuntimeError as error:
        assert "rebuilt candidate rename" in str(error)
    else:
        pytest.fail(f"rebuilt candidate did not reach rename: {unexpected!r}")

    current_metadata = second.job_repository.latest_checkpoint(
        submitted.job_id, "assemble_candidate_bundle"
    )
    assert current_metadata is not None
    assert current_metadata.checkpoint_id != old_metadata.checkpoint_id
    assert current_metadata.input_hash != old_request_hash
    committed = workspace_root / "raw" / "personal" / "bundles" / old_checkpoint.bundle_id
    linked_draft_path = (
        committed
        / "drafts"
        / f"{old_checkpoint.primary_draft_artifact_id}.md"
    )
    linked_draft = linked_draft_path.read_bytes()
    assert b"## Screenshots\n\n![Video screenshot 1 at 00:00.000]" in linked_draft
    quality = json.loads(
        (
            committed
            / "quality"
            / f"{old_checkpoint.quality_report_artifact_id}.json"
        ).read_bytes()
    )
    assert quality["subject"]["sha256"] == sha256_digest(linked_draft)
    assert (
        committed / original_assets[0].relative_path
    ).read_bytes() == original_assets[0].payload

    del second
    third = _create_runtime(
        machine,
        calls=calls,
        owner_id="candidate-version-third",
        now_ms=603_002,
        screenshot_process_factory=process_factory,
    )
    recovered = third.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    assert recovered.result.display_asset_ids == (original_assets[0].artifact_id,)
    assert calls.resolve == 1
    assert calls.acquire == 1
    assert calls.transcriber == 1
    assert calls.model == 1
    assert calls.ffmpeg == 1
    with third.job_repository._connect() as connection:
        candidate_attempts = connection.execute(
            """
            SELECT COUNT(*) FROM attempts
            WHERE job_id = ? AND step_id = 'assemble_candidate_bundle'
            """,
            (submitted.job_id,),
        ).fetchone()[0]
    assert candidate_attempts == 2
    for step_id in (
        "resolve_source",
        "acquire",
        "normalize_transcript",
        "create_source_revision",
        "generate_draft",
        "optional_screenshots",
        "quality_and_portable_validation",
    ):
        metadata = third.job_repository.latest_checkpoint(submitted.job_id, step_id)
        assert metadata is not None
        assert metadata.input_hash == old_request_hash


def test_restart_before_screenshot_checkpoint_rebuilds_identical_plan(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    machine = tmp_path / "screenshot-plan-replay-machine"
    calls = Calls()
    process_factory = _CrashOnceScreenshotProcessFactory(calls)
    first = _create_runtime(
        machine,
        calls=calls,
        owner_id="first-plan-owner",
        now_ms=1_000,
        screenshot_process_factory=process_factory,
    )
    first_plans: list[tuple[object, ...]] = []
    first_operations = first.video_service._operations
    first_operations._screenshot_adapter = _RecordingScreenshotAdapter(
        first_operations._screenshot_adapter, first_plans
    )
    submitted = first.submit_video(
        _request(
            local_video,
            workspace_root,
            "local-screenshot-plan-replay",
            screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        )
    )

    with pytest.raises(SystemExit, match="screenshot process loss"):
        first.wait_job(submitted.job_id)

    assert first.job_repository.latest_checkpoint(
        submitted.job_id, "generate_draft"
    ) is not None
    assert first.job_repository.latest_checkpoint(
        submitted.job_id, "optional_screenshots"
    ) is None
    recovered = _create_runtime(
        machine,
        calls=calls,
        owner_id="second-plan-owner",
        now_ms=302_001,
        screenshot_process_factory=process_factory,
    )
    recovered_plans: list[tuple[object, ...]] = []
    recovered_operations = recovered.video_service._operations
    recovered_operations._screenshot_adapter = _RecordingScreenshotAdapter(
        recovered_operations._screenshot_adapter, recovered_plans
    )

    result = recovered.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert first_plans == recovered_plans
    assert len(first_plans) == len(recovered_plans) == 1


def test_pre16a_acquisition_json_shape_decodes_to_no_stored_media(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    runtime = _create_runtime(tmp_path / "legacy-codec-machine")
    submitted = runtime.submit_video(_request(local_video, workspace_root, "legacy-codec"))
    runtime.wait_job(submitted.job_id)
    current = json.loads(_checkpoint_payload(runtime, submitted.job_id, "acquire"))
    current.pop("stored_media")

    decoded = decode_acquired(
        json.dumps(current, separators=(",", ":"), sort_keys=True).encode()
    )

    assert decoded.stored_media is None


def test_provided_transcript_still_snapshots_once_without_media_transcriber_call(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    runtime = _create_runtime(tmp_path / "provided-machine", calls=calls)
    provided = TranscriptDocument(
        language="en",
        segments=(TranscriptSegment("seg_000001", 0, 2_000, "provided local text"),),
    )
    submitted = runtime.submit_video(
        _request(
            local_video,
            workspace_root,
            "local-provided",
            provided_transcript=provided,
        )
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert calls.resolve == 1
    assert calls.acquire == 1
    assert calls.transcriber == 0
    assert len(tuple((tmp_path / "provided-machine" / "attempts").rglob("source_media.*"))) == 1
    assert decode_acquired(
        _checkpoint_payload(runtime, submitted.job_id, "acquire")
    ).stored_media is not None


def test_corrupted_snapshot_is_rejected_before_transcriber_model_or_public_path_leak(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    runtime = _create_runtime(tmp_path / "corrupt-machine", calls=calls)
    service = runtime.video_service
    service._operations = _CorruptSnapshotBeforeTranscript(service._operations)
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "local-corrupt-snapshot")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "attempt_stored_asset_invalid"
    assert calls.transcriber == 0
    assert calls.model == 0
    assert str(local_video.resolve()) not in result.error.message
    events = runtime.job_repository.list_events(submitted.job_id)
    assert all(str(local_video.resolve()) not in event.payload_json for event in events)


def test_path_bearing_transcriber_error_is_sanitized_from_all_public_state(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    runtime = _create_runtime(
        tmp_path / "leaking-transcriber-machine",
        transcriber=_PathLeakingTranscript(),
    )
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "path-leaking-transcriber")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assets = tuple(
        (tmp_path / "leaking-transcriber-machine" / "attempts").rglob("source_media.*")
    )
    assert len(assets) == 1
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_error = repr(
        {
            "code": result.error.code,
            "message": result.error.message,
            "details": result.error.details,
        }
    )
    public_events = repr(
        [vars(event) for event in runtime.job_repository.list_events(submitted.job_id)]
    )
    assert result.error.code == "local_transcription_failed"
    assert all(value not in public_error for value in private_values)
    assert all(value not in public_events for value in private_values)


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("provider_cancelled", ErrorCategory.CANCELLED),
        ("job_cancelled", ErrorCategory.CANCELLED),
        ("attempt_fenced", ErrorCategory.CONFLICT),
    ],
)
def test_control_flow_transcriber_errors_preserve_semantics_without_path_leak(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
    code: str,
    category: ErrorCategory,
) -> None:
    transcriber = _SemanticErrorTranscript(code, category, local_video)
    runtime = _create_runtime(
        tmp_path / f"{code}-machine",
        transcriber=transcriber,
    )
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, f"semantic-{code}")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == code
    assert result.error.category is category
    assert result.error.message == "Local media transcription failed"
    assert dict(result.error.details) == {}
    assets = tuple((tmp_path / f"{code}-machine" / "attempts").rglob("source_media.*"))
    assert len(assets) == 1
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_state = repr(
        {
            "error": result.error,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_retryable_transcription_error_remains_retryable_and_path_free(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    transcriber = _SemanticErrorTranscript(
        "transcription_failed",
        ErrorCategory.RETRYABLE_RUNTIME,
        local_video,
    )
    runtime = _create_runtime(
        tmp_path / "retryable-transcriber-machine",
        transcriber=transcriber,
    )
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "retryable-transcriber")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "transcription_failed"
    assert result.error.category is ErrorCategory.RETRYABLE_RUNTIME
    assert result.error.message == "Local media transcription failed"
    assert dict(result.error.details) == {}
    assets = tuple(
        (tmp_path / "retryable-transcriber-machine" / "attempts").rglob(
            "source_media.*"
        )
    )
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_state = repr(
        {
            "error": result.error,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_unknown_transcription_outcome_pauses_once_without_path_leak_or_resend(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    owner_id = "unknown-transcriber-owner"
    transcriber = _SemanticErrorTranscript(
        "external_outcome_unknown",
        ErrorCategory.CONFLICT,
        local_video,
        owner_id=owner_id,
    )
    runtime = _create_runtime(
        tmp_path / "unknown-transcriber-machine",
        owner_id=owner_id,
        transcriber=transcriber,
    )
    transcriber.bind_repository(runtime.job_repository)
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "unknown-transcriber")
    )

    first = runtime.wait_job(submitted.job_id)
    second = runtime.wait_job(submitted.job_id)

    assert first.state is second.state is JobState.WAITING_FOR_INPUT
    assert first.challenge_id is not None
    assert second.challenge_id == first.challenge_id
    assert first.error is second.error is None
    assert transcriber.calls == 1
    _, _, challenge = runtime.job_repository.get_job_details(submitted.job_id)
    assert challenge is not None
    prompt = json.loads(challenge.prompt_json)
    assert prompt["code"] == "external_outcome_unknown"
    assert len(prompt["operation_ids"]) == 1
    assets = tuple(
        (tmp_path / "unknown-transcriber-machine" / "attempts").rglob(
            "source_media.*"
        )
    )
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_state = repr(
        {
            "prompt": prompt,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_source_inspection_os_error_is_terminal_and_private_paths_are_redacted(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_root = tmp_path / "source-inspection-machine"
    runtime = _create_runtime(machine_root)
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "source-inspection-error")
    )
    original_is_reparse = FileAttemptStorage._is_reparse_point

    def failing_is_reparse(path: Path) -> bool:
        if path == local_video:
            raise PermissionError(f"cannot inspect {local_video}")
        return original_is_reparse(path)

    monkeypatch.setattr(
        FileAttemptStorage,
        "_is_reparse_point",
        staticmethod(failing_is_reparse),
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "attempt_storage_io_failed"
    private_values = (
        str(local_video.resolve()),
        str((machine_root / "attempts").resolve()),
    )
    public_state = repr(
        {
            "error": result.error,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_asset_replace_failure_is_terminal_and_path_free(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = os.replace

    def fail_asset_replace(source: object, target: object) -> None:
        if Path(target).name.startswith("source_media"):
            raise OSError(f"replace failed for {target}")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_asset_replace)
    runtime = _create_runtime(tmp_path / "replace-failure-machine")
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "replace-failure")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "attempt_storage_io_failed"
    assert str(local_video.resolve()) not in repr(result.error)


def test_process_loss_after_transcript_reuses_snapshot_without_original(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    machine_root = tmp_path / "recovery-machine"
    call_log = tmp_path / "calls.jsonl"
    initial = _create_runtime(
        machine_root,
        owner_id="submitting-process",
        now_ms=1_000,
    )
    submitted = initial.submit_video(
        _request(local_video, workspace_root, "local-recovery")
    )
    process = multiprocessing.get_context("spawn").Process(
        target=_terminate_after_transcript,
        args=(str(machine_root), submitted.job_id, str(call_log)),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23
    assert runtime_module._read_checkpoint is not None
    assert initial.job_repository.latest_checkpoint(
        submitted.job_id, "normalize_transcript"
    ) is not None
    local_video.unlink()

    recovered_calls = Calls()
    recovered = _create_runtime(
        machine_root,
        calls=recovered_calls,
        call_log=call_log,
        owner_id="recovery-process",
        now_ms=302_001,
    )
    result = recovered.wait_job(submitted.job_id)

    operations = [json.loads(line)["operation"] for line in call_log.read_text().splitlines()]
    assert result.state is JobState.SUCCEEDED
    assert operations.count("transcriber") == 1
    assert operations.count("model") == 1
    assert operations.count("source_resolve") == 1
    assert operations.count("source_acquire") == 1
    assert operations.count("portable_commit") == 1
    assert recovered_calls.transcriber == 0
    assert recovered_calls.model == 1
    assert len(tuple((machine_root / "attempts").rglob("source_media.*"))) == 1
    assert recovered_calls.commit == 1
