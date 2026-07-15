from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.adapters.screenshots.ffmpeg import FFmpegScreenshotAdapter
from app.adapters.models.legacy_gpt import (
    LegacyKnowledgeModelAdapter,
    LegacyModelBinding,
    ModelChunkResultStore,
    ModelExecutionBinding,
)
from app.adapters.transcription.legacy_transcriber import (
    LegacyTranscriberAdapter,
    normalize_platform_subtitle,
)
from app.core.application.video_acquisition import (
    StoredAssetRole,
    TranscriptProvenance,
    VideoAcquisition,
    transcript_identity,
)
from app.core.application.video_service import (
    CHECKPOINT_SCHEMA,
    VideoPreflightCapabilities,
    VideoRecipeOperations,
    VideoService,
    VideoStepExecutionContext,
)
from app.core.domain.video import (
    GeneratedVideoDraft,
    JobSnapshot,
    ScreenshotPlanItem,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.cancellation import CancellationToken
from app.core.jobs.external_operation import ExternalOperationGuard
from app.core.jobs.model import CheckpointMetadata
from app.core.portable.bundle_assembler import DisplayAssetInput, VideoSourceMetadata
from app.core.ports.model import KnowledgeModelRequest
from app.core.ports.screenshot import ScreenshotPort
from app.core.ports.source import (
    MaterializationPolicy,
    ResolvedVideoSource,
    SubtitleAvailability,
    VideoSourcePort,
)
from app.core.ports.transcript import MediaInput
from app.core.ports.transcript import TranscriptPort
from app.core.sdk import AllToNoteSDK
from iwiki.workspace import open_workspace


@dataclass
class FakeCallCounts:
    download: int = 0
    transcribe: int = 0
    model: int = 0
    ffmpeg: int = 0
    commit: int = 0


_FAKE_SCREENSHOT_WEBP = bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"
)


class _FakeVideoOperations(VideoRecipeOperations):
    def __init__(
        self,
        calls: FakeCallCounts,
        *,
        capabilities: VideoPreflightCapabilities,
        quality_fail: bool,
        crash_after_commit_once: bool,
        crash_operation_once: str | None,
        call_log_path: Path | None,
        operation_hooks: Mapping[str, Callable[[Callable[[], None]], None]],
        screenshot_requests: tuple[ScreenshotRequest, ...],
    ) -> None:
        self._calls = calls
        self._capabilities = capabilities
        self._quality_fail = quality_fail
        self._crash_after_commit_once = crash_after_commit_once
        self._crash_operation_once = crash_operation_once
        self._call_log_path = call_log_path
        self._operation_hooks = dict(operation_hooks)
        self._screenshot_requests = tuple(screenshot_requests)

    def preflight_capabilities(
        self, request: VideoProduceRequest
    ) -> VideoPreflightCapabilities:
        del request
        return self._capabilities

    def _record(self, operation: str) -> None:
        if self._call_log_path is None:
            return
        with self._call_log_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"operation": operation}, separators=(",", ":")))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _crash_if_requested(self, operation: str) -> None:
        if self._crash_operation_once == operation:
            self._crash_operation_once = None
            raise RuntimeError(f"injected {operation} crash")

    def _run_operation_hook(
        self,
        operation: str,
        heartbeat: Callable[[], None],
    ) -> None:
        hook = self._operation_hooks.get(operation)
        if hook is not None:
            hook(heartbeat)

    def resolve_source(
        self,
        request: VideoProduceRequest,
        *,
        source_id: str,
        source_revision_id: str,
    ) -> ResolvedVideoSource:
        del source_id, source_revision_id
        stable_identity = request.input_value.removeprefix("fixture://")
        if not stable_identity:
            stable_identity = "course"
        return ResolvedVideoSource(
            connector_id="fixture",
            connector_version="1.0.0",
            platform="fixture",
            canonical_identity_scheme="fixture-video",
            stable_video_identity=stable_identity,
            canonical_identity=f"fixture-video:{stable_identity}",
            canonical_uri=f"https://fixtures.alltonote.invalid/{stable_identity}",
            logical_reference=None,
            materialization_policy=MaterializationPolicy.REFERENCE_ONLY,
        )

    def acquire(
        self,
        request: VideoProduceRequest,
        source: ResolvedVideoSource,
        *,
        source_id: str,
        source_revision_id: str,
        execution: VideoStepExecutionContext,
    ) -> VideoAcquisition:
        transcript = request.provided_transcript or TranscriptDocument(
            language="zh-CN",
            segments=(
                TranscriptSegment(
                    segment_id="seg_000001",
                    start_ms=0,
                    end_ms=2_000,
                    text="Knowledge production preserves verifiable source notes.",
                ),
            ),
        )
        self._calls.download += 1
        self._record("download")
        self._crash_if_requested("download")
        self._run_operation_hook("download", execution.heartbeat)
        canonical_uri = source.canonical_uri or "https://fixtures.alltonote.invalid/course"
        metadata = VideoSourceMetadata(
            source_id=source_id,
            source_revision_id=source_revision_id,
            connector_id=source.connector_id,
            connector_version=source.connector_version,
            platform=source.platform,
            canonical_identity_scheme=source.canonical_identity_scheme,
            stable_video_identity=source.stable_video_identity,
            canonical_uri=canonical_uri,
            title="AllToNote Fixture Course",
            author="AllToNote",
            channel="AllToNote",
            duration_ms=2_000,
            published_at="2026-07-14T00:00:00.000Z",
            observed_at="2026-07-14T00:00:00.000Z",
            language=transcript.language,
            subtitle_acquisition=(
                "provided" if request.provided_transcript is not None else "generated"
            ),
            source_link=canonical_uri,
            materialization_reason="remote_video_reference",
            license="unknown",
            privacy="personal",
            freshness="point_in_time",
        )
        return VideoAcquisition(
            metadata=metadata,
            subtitle_availability=SubtitleAvailability.AVAILABLE,
            transcript=transcript,
            transcript_identity=transcript_identity(transcript),
            transcript_provenance=(
                TranscriptProvenance.PROVIDED
                if request.provided_transcript is not None
                else TranscriptProvenance.GENERATED
            ),
        )

    def transcribe(
        self,
        request: VideoProduceRequest,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> TranscriptDocument:
        del acquisition_checkpoint
        self._calls.transcribe += 1
        self._record("transcribe")
        self._crash_if_requested("transcribe")
        self._run_operation_hook("transcribe", execution.heartbeat)
        transcript = request.provided_transcript or acquired.transcript
        assert transcript is not None
        return transcript

    def generate_draft(
        self,
        request: VideoProduceRequest,
        transcript: TranscriptDocument,
        *,
        execution: VideoStepExecutionContext,
    ) -> GeneratedVideoDraft:
        del request, transcript
        self._calls.model += 1
        self._record("model")
        self._crash_if_requested("model")
        self._run_operation_hook("model", execution.heartbeat)
        if self._quality_fail:
            markdown = "# Video note\n\nTODO: add evidence.\n"
            cited_segment_ids: tuple[str, ...] = ()
        else:
            markdown = (
                "# Video note\n\n## Evidence\n\n"
                "Knowledge production preserves verifiable evidence.[^seg_000001]\n"
            )
            cited_segment_ids = ("seg_000001",)
        return GeneratedVideoDraft(
            markdown=markdown,
            cited_segment_ids=cited_segment_ids,
            screenshot_requests=self._screenshot_requests,
            model_identity="fake/model-v1",
            usage={"input_tokens": 12, "output_tokens": 6},
            warnings=(),
        )

    def screenshots(
        self,
        plan: tuple[ScreenshotPlanItem, ...],
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> tuple[DisplayAssetInput, ...]:
        del transcript, acquired, acquisition_checkpoint
        self._crash_if_requested("screenshots")
        if plan:
            self._calls.ffmpeg += 1
            self._record("ffmpeg")
            self._run_operation_hook("ffmpeg", execution.heartbeat)
        return tuple(
            DisplayAssetInput(
                artifact_id=item.artifact_id,
                relative_path=item.relative_path,
                media_type="image/webp",
                payload=_FAKE_SCREENSHOT_WEBP,
            )
            for item in plan
        )

    def after_portable_commit(self, result: object) -> None:
        if not bool(getattr(result, "idempotent", False)):
            self._calls.commit += 1
            self._record("portable_commit")
        if self._crash_after_commit_once:
            self._crash_after_commit_once = False
            raise RuntimeError("injected crash after portable rename")


_SOURCE_METADATA_FIELDS = frozenset(
    {
        "author",
        "channel",
        "duration_ms",
        "language",
        "observed_at",
        "published_at",
        "title",
    }
)


class _PlatformVideoOperations(VideoRecipeOperations):
    def __init__(
        self,
        repository: SqliteJobRepository,
        source: VideoSourcePort,
        source_metadata: Mapping[str, Mapping[str, object]],
        model: LegacyModelBinding,
        result_root: Path,
    ) -> None:
        self._repository = repository
        self._source = source
        self._source_metadata = {
            platform: dict(metadata)
            for platform, metadata in source_metadata.items()
        }
        self._model = model
        self._result_store = ModelChunkResultStore(result_root)
        self._acquisition_root = result_root / "acquisition"

    def preflight_capabilities(
        self, request: VideoProduceRequest
    ) -> VideoPreflightCapabilities:
        del request
        return VideoPreflightCapabilities()

    def resolve_source(
        self,
        request: VideoProduceRequest,
        *,
        source_id: str,
        source_revision_id: str,
    ) -> ResolvedVideoSource:
        del source_id, source_revision_id
        return self._source.resolve(request.input_value)

    def acquire(
        self,
        request: VideoProduceRequest,
        source: ResolvedVideoSource,
        *,
        source_id: str,
        source_revision_id: str,
        execution: VideoStepExecutionContext,
    ) -> VideoAcquisition:
        token = CancellationToken(self._repository, execution.job_id)
        provided = request.provided_transcript
        acquired = self._source.acquire(
            source,
            need_media=False,
            need_subtitles=provided is None,
            output_dir=self._acquisition_root / execution.job_id,
            token=token,
        )
        transcript = provided
        provenance = (
            TranscriptProvenance.PROVIDED
            if provided is not None
            else None
        )
        if (
            transcript is None
            and acquired.subtitle_availability is SubtitleAvailability.AVAILABLE
        ):
            transcript = normalize_platform_subtitle(
                acquired.opaque_subtitle,
                check_cancelled=token.raise_if_cancelled,
            )
            provenance = TranscriptProvenance.PLATFORM
        fixture = self._source_metadata.get(source.platform)
        if fixture is None or frozenset(fixture) != _SOURCE_METADATA_FIELDS:
            raise DomainError(
                "source_metadata_unavailable",
                ErrorCategory.RECIPE_FAILED,
                "Complete source metadata is required for platform composition",
            )
        canonical_uri = source.canonical_uri
        if canonical_uri is None:
            raise DomainError(
                "source_metadata_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Platform source metadata requires a canonical URI",
            )
        try:
            metadata = VideoSourceMetadata(
                source_id=source_id,
                source_revision_id=source_revision_id,
                connector_id=source.connector_id,
                connector_version=source.connector_version,
                platform=source.platform,
                canonical_identity_scheme=source.canonical_identity_scheme,
                stable_video_identity=source.stable_video_identity,
                canonical_uri=canonical_uri,
                title=fixture["title"],
                author=fixture["author"],
                channel=fixture["channel"],
                duration_ms=fixture["duration_ms"],
                published_at=fixture["published_at"],
                observed_at=fixture["observed_at"],
                language=fixture["language"],
                subtitle_acquisition=(
                    provenance.value
                    if provenance is not None
                    else acquired.subtitle_availability.value
                ),
                source_link=canonical_uri,
                materialization_reason="remote_video_reference",
                license="unknown",
                privacy="personal",
                freshness="point_in_time",
            )
        except (DomainError, TypeError, ValueError):
            raise DomainError(
                "source_metadata_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Complete source metadata is invalid",
            ) from None
        return VideoAcquisition(
            metadata=metadata,
            subtitle_availability=(
                SubtitleAvailability.AVAILABLE
                if transcript is not None
                else acquired.subtitle_availability
            ),
            transcript=transcript,
            transcript_identity=(
                transcript_identity(transcript) if transcript is not None else None
            ),
            transcript_provenance=provenance,
        )

    def transcribe(
        self,
        request: VideoProduceRequest,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> TranscriptDocument:
        del acquisition_checkpoint
        token = CancellationToken(self._repository, execution.job_id)
        transcript = acquired.transcript
        if transcript is None:
            raise DomainError(
                "platform_subtitle_unavailable",
                ErrorCategory.RECIPE_FAILED,
                "Platform subtitles are unavailable; media fallback is not enabled",
            )
        return LegacyTranscriberAdapter("fast-whisper").transcribe(
            MediaInput(provided_transcript=transcript), token
        )

    def generate_draft(
        self,
        request: VideoProduceRequest,
        transcript: TranscriptDocument,
        *,
        execution: VideoStepExecutionContext,
    ) -> GeneratedVideoDraft:
        token = CancellationToken(self._repository, execution.job_id)
        adapter = LegacyKnowledgeModelAdapter(
            model=self._model,
            execution=ModelExecutionBinding(
                guard=ExternalOperationGuard(
                    self._repository, execution.authority
                ),
                result_store=self._result_store,
                job_id=execution.job_id,
                step_id=execution.step_id,
                attempt_id=execution.attempt_id,
            ),
            max_prompt_bytes=64 * 1024,
        )
        return adapter.generate(
            KnowledgeModelRequest(
                transcript=transcript,
                recipe_id=request.recipe_id,
                recipe_version=request.recipe_version,
                output_language=request.output_language,
                style=request.style,
                quality_preset=request.quality_preset,
                screenshot_policy=request.screenshot_policy,
            ),
            token,
        )

    def screenshots(
        self,
        plan: tuple[ScreenshotPlanItem, ...],
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> tuple[DisplayAssetInput, ...]:
        del transcript, acquired, acquisition_checkpoint, execution
        if plan:
            raise DomainError(
                "screenshot_capability_unavailable",
                ErrorCategory.POLICY_DENIED,
                "Platform subtitle composition does not provide screenshots",
            )
        return ()

    def after_portable_commit(self, result: object) -> None:
        del result


class _LocalVideoOperations(_PlatformVideoOperations):
    def __init__(
        self,
        repository: SqliteJobRepository,
        storage: FileAttemptStorage,
        source: VideoSourcePort,
        source_metadata: Mapping[str, Mapping[str, object]],
        transcriber: TranscriptPort,
        model: LegacyModelBinding,
        result_root: Path,
        screenshot_adapter: ScreenshotPort,
    ) -> None:
        super().__init__(repository, source, source_metadata, model, result_root)
        self._storage = storage
        self._transcriber = transcriber
        self._screenshot_adapter = screenshot_adapter

    def acquire(
        self,
        request: VideoProduceRequest,
        source: ResolvedVideoSource,
        *,
        source_id: str,
        source_revision_id: str,
        execution: VideoStepExecutionContext,
    ) -> VideoAcquisition:
        token = CancellationToken(self._repository, execution.job_id)
        live_source = source
        if live_source.local_binding is None:
            live_source = self._source.resolve(request.input_value)
            if (
                live_source.connector_id != source.connector_id
                or live_source.connector_version != source.connector_version
                or live_source.canonical_identity != source.canonical_identity
                or live_source.logical_reference != source.logical_reference
                or live_source.content_sha256 != source.content_sha256
            ):
                raise DomainError(
                    "source_local_changed",
                    ErrorCategory.INVALID_REQUEST,
                    "The local source changed after it was resolved",
                )
        binding = live_source.local_binding
        digest = live_source.content_sha256
        fixture = self._source_metadata.get("local")
        if (
            binding is None
            or digest is None
            or fixture is None
            or frozenset(fixture) != _SOURCE_METADATA_FIELDS
        ):
            raise DomainError(
                "source_metadata_unavailable",
                ErrorCategory.RECIPE_FAILED,
                "Complete source metadata is required for local composition",
            )
        provided = request.provided_transcript
        self._source.acquire(
            live_source,
            need_media=False,
            need_subtitles=False,
            output_dir=self._acquisition_root / execution.job_id,
            token=token,
        )
        stored = self._storage.snapshot_asset(
            binding.path,
            job_id=execution.job_id,
            attempt_id=execution.attempt_id,
            role=StoredAssetRole.SOURCE_MEDIA,
            expected_sha256=digest,
            authority=execution.authority,
            token=token,
        )
        try:
            metadata = VideoSourceMetadata(
                source_id=source_id,
                source_revision_id=source_revision_id,
                connector_id=live_source.connector_id,
                connector_version=live_source.connector_version,
                platform="local",
                canonical_identity_scheme=live_source.canonical_identity_scheme,
                stable_video_identity=live_source.stable_video_identity,
                canonical_uri=None,
                title=fixture["title"],
                author=fixture["author"],
                channel=fixture["channel"],
                duration_ms=fixture["duration_ms"],
                published_at=fixture["published_at"],
                observed_at=fixture["observed_at"],
                language=fixture["language"],
                subtitle_acquisition=(
                    TranscriptProvenance.PROVIDED.value
                    if provided is not None
                    else TranscriptProvenance.GENERATED.value
                ),
                source_link=None,
                materialization_reason="external_local_content",
                license="unknown",
                privacy="personal",
                freshness="point_in_time",
                logical_reference=live_source.logical_reference,
                materialization_kind=MaterializationPolicy.EXTERNAL_LOCAL.value,
            )
        except (DomainError, TypeError, ValueError):
            raise DomainError(
                "source_metadata_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Complete source metadata is invalid",
            ) from None
        return VideoAcquisition(
            metadata=metadata,
            subtitle_availability=(
                SubtitleAvailability.AVAILABLE
                if provided is not None
                else SubtitleAvailability.NOT_SUPPORTED
            ),
            transcript=provided,
            transcript_identity=(
                transcript_identity(provided) if provided is not None else None
            ),
            transcript_provenance=(
                TranscriptProvenance.PROVIDED if provided is not None else None
            ),
            stored_media=stored,
        )

    def transcribe(
        self,
        request: VideoProduceRequest,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> TranscriptDocument:
        del request
        token = CancellationToken(self._repository, execution.job_id)
        if acquired.transcript is not None:
            return acquired.transcript
        stored = acquired.stored_media
        if stored is None:
            raise DomainError(
                "source_media_missing",
                ErrorCategory.RECIPE_FAILED,
                "Local source media is unavailable",
            )
        media_path = self._storage.resolve_asset(
            stored,
            expected_job_id=acquisition_checkpoint.job_id,
            expected_attempt_id=acquisition_checkpoint.attempt_id,
        )
        try:
            return self._transcriber.transcribe(MediaInput(media_path=media_path), token)
        except DomainError as error:
            if (
                error.category
                in {ErrorCategory.CANCELLED, ErrorCategory.RETRYABLE_RUNTIME}
                or error.code
                in {"attempt_fenced", "job_cancelled", "external_outcome_unknown"}
            ):
                raise DomainError(
                    error.code,
                    error.category,
                    "Local media transcription failed",
                ) from None
            raise DomainError(
                "local_transcription_failed",
                ErrorCategory.RECIPE_FAILED,
                "Local media transcription failed",
            ) from None

    def screenshots(
        self,
        plan: tuple[ScreenshotPlanItem, ...],
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> tuple[DisplayAssetInput, ...]:
        return self._screenshot_adapter.extract(
            plan,
            transcript,
            acquired,
            acquisition_checkpoint=acquisition_checkpoint,
            execution=execution,
        )


class AllToNoteRuntime:
    def __init__(
        self,
        sdk: AllToNoteSDK,
        job_repository: SqliteJobRepository,
    ) -> None:
        self._sdk = sdk
        self.job_repository = job_repository

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        return self._sdk.submit_video(request)

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        return self._sdk.wait_job(job_id, event_sink)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._sdk.get_job(job_id)


def _checkpoint_payload_is_valid(payload: bytes) -> bool:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return type(value) is dict and type(value.get("step")) is str


def _read_checkpoint(
    storage: FileAttemptStorage,
    metadata: CheckpointMetadata,
) -> bytes:
    relative = Path(metadata.relative_path)
    expected = (
        Path("jobs")
        / metadata.job_id
        / "checkpoints"
        / f"{metadata.checkpoint_id}.payload"
    )
    if relative.parts != expected.parts:
        raise DomainError(
            "checkpoint_content_invalid",
            ErrorCategory.INTERNAL,
            "Candidate checkpoint path is invalid",
        )
    return (storage.root / relative).read_bytes()


def create_platform_video_runtime(
    machine_root: Path,
    *,
    source: VideoSourcePort,
    source_metadata: Mapping[str, Mapping[str, object]],
    model: LegacyModelBinding,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
) -> AllToNoteRuntime:
    resolved_machine_root = Path(machine_root).resolve()
    resolved_machine_root.mkdir(parents=True, exist_ok=True)
    repository = SqliteJobRepository.open(
        resolved_machine_root / "job-store", clock=clock
    )
    storage = FileAttemptStorage(
        resolved_machine_root / "attempts",
        repository,
        validators={CHECKPOINT_SCHEMA: _checkpoint_payload_is_valid},
    )
    operations = _PlatformVideoOperations(
        repository,
        source,
        source_metadata,
        model,
        resolved_machine_root / "external-results",
    )
    service = VideoService(
        repository,
        storage,
        IWikiPortableGateway(),
        operations,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id=owner_id or f"runtime-{uuid4().hex}",
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
    )
    return AllToNoteRuntime(AllToNoteSDK(service), repository)


def create_local_video_runtime(
    machine_root: Path,
    *,
    source: VideoSourcePort,
    source_metadata: Mapping[str, Mapping[str, object]],
    transcriber: TranscriptPort,
    model: LegacyModelBinding,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    screenshot_adapter_factory: Callable[
        [FileAttemptStorage, SqliteJobRepository], ScreenshotPort
    ]
    | None = None,
) -> AllToNoteRuntime:
    resolved_machine_root = Path(machine_root).resolve()
    resolved_machine_root.mkdir(parents=True, exist_ok=True)
    repository = SqliteJobRepository.open(
        resolved_machine_root / "job-store", clock=clock
    )
    storage = FileAttemptStorage(
        resolved_machine_root / "attempts",
        repository,
        validators={CHECKPOINT_SCHEMA: _checkpoint_payload_is_valid},
    )
    operations = _LocalVideoOperations(
        repository,
        storage,
        source,
        source_metadata,
        transcriber,
        model,
        resolved_machine_root / "external-results",
        (
            screenshot_adapter_factory(storage, repository)
            if screenshot_adapter_factory is not None
            else FFmpegScreenshotAdapter(storage, repository)
        ),
    )
    service = VideoService(
        repository,
        storage,
        IWikiPortableGateway(),
        operations,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id=owner_id or f"runtime-{uuid4().hex}",
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
    )
    return AllToNoteRuntime(AllToNoteSDK(service), repository)


def create_fake_runtime(
    machine_root: Path,
    *,
    calls: FakeCallCounts | None = None,
    capabilities: VideoPreflightCapabilities | None = None,
    quality_fail: bool = False,
    crash_after_commit_once: bool = False,
    crash_operation_once: str | None = None,
    call_log_path: Path | None = None,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    operation_hooks: Mapping[str, Callable[[Callable[[], None]], None]] | None = None,
    screenshot_requests: tuple[ScreenshotRequest, ...] = (),
) -> AllToNoteRuntime:
    call_counts = calls or FakeCallCounts()
    resolved_machine_root = Path(machine_root).resolve()
    resolved_machine_root.mkdir(parents=True, exist_ok=True)
    resolved_call_log = Path(call_log_path).resolve() if call_log_path is not None else None
    if resolved_call_log is not None:
        resolved_call_log.parent.mkdir(parents=True, exist_ok=True)
    repository = SqliteJobRepository.open(
        resolved_machine_root / "job-store", clock=clock
    )
    storage = FileAttemptStorage(
        resolved_machine_root / "attempts",
        repository,
        validators={CHECKPOINT_SCHEMA: _checkpoint_payload_is_valid},
    )
    operations = _FakeVideoOperations(
        call_counts,
        capabilities=capabilities or VideoPreflightCapabilities(),
        quality_fail=quality_fail,
        crash_after_commit_once=crash_after_commit_once,
        crash_operation_once=crash_operation_once,
        call_log_path=resolved_call_log,
        operation_hooks=operation_hooks or {},
        screenshot_requests=screenshot_requests,
    )
    service = VideoService(
        repository,
        storage,
        IWikiPortableGateway(),
        operations,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id=owner_id or f"runtime-{uuid4().hex}",
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
    )
    return AllToNoteRuntime(AllToNoteSDK(service), repository)


def create_fake_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
) -> AllToNoteRuntime:
    trusted_root = local_app_data or _default_local_app_data()
    trusted_root.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceInstanceRegistry(
        trusted_root,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace_root)
    return create_fake_runtime(
        instance.machine_root,
        local_instance_id=instance.instance_id,
    )


def _default_local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".local" / "share"


__all__ = [
    "AllToNoteRuntime",
    "FakeCallCounts",
    "VideoPreflightCapabilities",
    "create_fake_runtime",
    "create_fake_runtime_for_workspace",
    "create_local_video_runtime",
    "create_platform_video_runtime",
]
