from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.documents.docling_worker_parser import (
    DoclingWorkerConfig,
    DoclingWorkerParser,
)
from app.adapters.documents.document_basic_pack import (
    PACK_ID,
    PACK_VERSION,
    resolve_document_basic_pack_paths,
)
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import (
    WorkspaceInstance,
    WorkspaceInstanceRegistry,
)
from app.adapters.screenshots.ffmpeg import FFmpegScreenshotAdapter
from app.adapters.models.legacy_gpt import (
    LegacyKnowledgeModelAdapter,
    LegacyModelBinding,
    LegacyModelCapabilities,
    ModelChunkResultStore,
    ModelExecutionBinding as LegacyModelExecutionBinding,
)
from app.adapters.models.codex_app_server_bridge import (
    CodexAppServerCompletionBridge,
)
from app.adapters.models.legacy_model_executor import LegacyModelExecutor
from app.adapters.models.model_result_store import ModelOperationResultStore
from app.adapters.sources.legacy_video import LegacyVideoSourceAdapter
from app.adapters.sources.legacy_video import VerifiedSourceIdentityRegistry
from app.adapters.transcription.legacy_transcriber import (
    LegacyTranscriberAdapter,
    normalize_platform_subtitle,
)
from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    TRANSCRIBE_CPU,
)
from app.adapters.video_packs.official_video_pack_resolver import (
    OfficialVideoPackResolver,
    ResolvedOfficialVideoPack,
)
from app.adapters.video_packs.official_video_pack_trust import (
    official_video_pack_trust_keys,
)
from app.adapters.video_packs.packed_bilibili_source import (
    PackedBilibiliVideoSourceAdapter,
)
from app.adapters.video_packs.packed_cpu_transcriber import (
    PackedCpuTranscriber,
)
from app.core.application.produce_service import ProduceService
from app.core.application.document_service import (
    CHECKPOINT_SCHEMA as DOCUMENT_CHECKPOINT_SCHEMA,
    DocumentKnowledgeCompilationInput,
    DocumentKnowledgeVerificationInput,
    DocumentService,
)
from app.core.application.document_knowledge_compiler import (
    CompiledDocumentKnowledgeNoteV1,
    DocumentCompilationContext,
    DocumentKnowledgeCompilationRequestV1,
    DocumentKnowledgeCompiler,
)
from app.core.application.document_knowledge_verifier import (
    DocumentKnowledgeVerificationRequestV1,
    DocumentKnowledgeVerificationV1,
    DocumentKnowledgeVerifier,
)
from app.core.application.job_execution_router import JobExecutionRouter
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
    VideoFaithfulCompilationInput,
    VideoKnowledgeCompilationInput,
    VideoStepExecutionContext,
)
from app.core.application.faithful_edition_compiler import (
    FaithfulCompiledVideoDocument,
    FaithfulEditionCompiler,
)
from app.core.application.model_call_coordinator import (
    ModelCallCoordinator,
    ModelCallExecution,
)
from app.core.application.video_compiler import (
    CompiledVideoDocument,
    KnowledgeCompilationRequestV1,
    VideoCompilationContext,
    VideoKnowledgeCompiler,
)
from app.core.config.model import JobConfigSnapshot
from app.core.domain.ids import sha256_digest
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    GeneratedVideoDraft,
    JobSnapshot,
    ScreenshotPlanItem,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
    VideoDocumentKind,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.cancellation import CancellationToken
from app.core.jobs.external_operation import ExternalOperationGuard
from app.core.jobs.model import (
    CheckpointMetadata,
    JobExecutionBinding,
    JobExecutionOwner,
)
from app.core.jobs.resource_lease import ResourceLeaseStorePort, ResourceOwner
from app.core.packs.events import (
    ExecutionPackIdentity,
    JobPackEnvironmentSnapshot,
)
from app.core.portable.bundle_assembler import DisplayAssetInput, VideoSourceMetadata
from app.core.ports.jobs import SourceIdentityBinding
from app.core.ports.model import KnowledgeModelRequest
from app.core.ports.model_executor import ModelExecutionBinding
from app.core.ports.screenshot import ScreenshotPort
from app.core.ports.source import (
    MaterializationPolicy,
    ResolvedVideoSource,
    SubtitleAvailability,
    VideoSourcePort,
)
from app.core.ports.transcript import MediaInput
from app.core.ports.transcript import TranscriptPort
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission
from app.core.recipes.registry import RecipeRegistry
from app.core.recipes.document.adapter import DocumentRecipeAdapter
from app.core.recipes.document.descriptor import DOCUMENT_NOTE_V1
from app.core.recipes.video.adapter import (
    VideoRecipeAdapter,
    adapt_video_produce_request,
)
from app.core.recipes.video.descriptor import VIDEO_DESCRIPTORS
from app.core.recipes.video.compilation.contracts import (
    CompilationQualityProfile,
    ComposerParserLimitsV1,
    KnowledgeMapParserLimitsV1,
    TranscriptBasis,
    TranscriptQualityInputV1,
    VideoCompilationPlanningRequestV1,
)
from app.core.recipes.video.compilation.pipeline import assess_transcript_quality
from app.core.recipes.video.faithful_edition.contracts import (
    FaithfulEditionParserLimitsV1,
    FaithfulEditionRequestV1,
)
from app.core.sdk import AllToNoteSDK
from app.services.codex_app_server import CodexAppServerStatusService
from app.runtime_paths import RuntimePaths, resolve_runtime_paths
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
        storage: FileAttemptStorage | None = None,
        transcriber: TranscriptPort | None = None,
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
        self._storage = storage
        self._transcriber = transcriber

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
        if source.platform == "local":
            return self._acquire_local(
                request,
                source,
                source_id=source_id,
                source_revision_id=source_revision_id,
                execution=execution,
            )
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
        subtitle_availability = acquired.subtitle_availability
        if (
            transcript is None
            and subtitle_availability is SubtitleAvailability.UNKNOWN
        ):
            raise DomainError(
                "platform_subtitle_status_unknown",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Platform subtitle availability could not be determined; "
                "media fallback was not started",
            )
        stored_media = None
        if (
            transcript is None
            and self._storage is not None
            and self._transcriber is not None
        ):
            acquired = self._source.acquire(
                source,
                need_media=True,
                need_subtitles=False,
                output_dir=self._acquisition_root / execution.job_id,
                token=token,
            )
            media_path = acquired.media_path
            if media_path is None:
                raise DomainError(
                    "source_media_missing",
                    ErrorCategory.RECIPE_FAILED,
                    "Platform media fallback did not produce media",
                )
            digest = hashlib.sha256()
            with media_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    token.raise_if_cancelled()
                    digest.update(chunk)
            stored_media = self._storage.snapshot_asset(
                media_path,
                job_id=execution.job_id,
                attempt_id=execution.attempt_id,
                role=StoredAssetRole.SOURCE_MEDIA,
                expected_sha256=f"sha256:{digest.hexdigest()}",
                authority=execution.authority,
                token=token,
            )
        fixture = self._source_metadata.get(source.platform)
        if fixture is not None and frozenset(fixture) != _SOURCE_METADATA_FIELDS:
            raise DomainError(
                "source_metadata_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Configured source metadata is invalid for platform composition",
            )
        canonical_uri = source.canonical_uri
        if canonical_uri is None:
            raise DomainError(
                "source_metadata_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Platform source metadata requires a canonical URI",
            )
        if fixture is None:
            duration_ms = acquired.duration_ms or (
                transcript.segments[-1].end_ms if transcript is not None else 0
            )
            language = transcript.language if transcript is not None else "und"
            metadata_values: Mapping[str, object] = {
                "title": acquired.title or source.stable_video_identity,
                "author": "unknown",
                "channel": "unknown",
                "duration_ms": duration_ms,
                "published_at": None,
                "observed_at": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "language": language,
            }
        else:
            metadata_values = fixture
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
                title=metadata_values["title"],
                author=metadata_values["author"],
                channel=metadata_values["channel"],
                duration_ms=metadata_values["duration_ms"],
                published_at=metadata_values["published_at"],
                observed_at=metadata_values["observed_at"],
                language=metadata_values["language"],
                subtitle_acquisition=(
                    provenance.value
                    if provenance is not None
                    else (
                        TranscriptProvenance.GENERATED.value
                        if stored_media is not None
                        else subtitle_availability.value
                    )
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
                else subtitle_availability
            ),
            transcript=transcript,
            transcript_identity=(
                transcript_identity(transcript) if transcript is not None else None
            ),
            transcript_provenance=provenance,
            stored_media=stored_media,
        )

    def _acquire_local(
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
        storage = self._storage
        fixture = self._source_metadata.get("local")
        if (
            binding is None
            or digest is None
            or storage is None
            or (fixture is not None and frozenset(fixture) != _SOURCE_METADATA_FIELDS)
        ):
            raise DomainError(
                "source_metadata_unavailable",
                ErrorCategory.RECIPE_FAILED,
                "Complete source metadata is required for local composition",
            )
        provided = request.provided_transcript
        acquired = self._source.acquire(
            live_source,
            need_media=False,
            need_subtitles=False,
            output_dir=self._acquisition_root / execution.job_id,
            token=token,
        )
        stored = storage.snapshot_asset(
            binding.path,
            job_id=execution.job_id,
            attempt_id=execution.attempt_id,
            role=StoredAssetRole.SOURCE_MEDIA,
            expected_sha256=digest,
            authority=execution.authority,
            token=token,
        )
        if fixture is None:
            metadata_values: Mapping[str, object] = {
                "title": acquired.title or binding.path.stem,
                "author": "unknown",
                "channel": "local",
                "duration_ms": (
                    acquired.duration_ms
                    if acquired.duration_ms is not None
                    else (
                        provided.segments[-1].end_ms
                        if provided is not None and provided.segments
                        else None
                    )
                ),
                "published_at": None,
                "observed_at": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "language": provided.language if provided is not None else "und",
            }
        else:
            metadata_values = fixture
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
                title=metadata_values["title"],
                author=metadata_values["author"],
                channel=metadata_values["channel"],
                duration_ms=metadata_values["duration_ms"],
                published_at=metadata_values["published_at"],
                observed_at=metadata_values["observed_at"],
                language=metadata_values["language"],
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
        token = CancellationToken(self._repository, execution.job_id)
        transcript = acquired.transcript
        if transcript is not None:
            return LegacyTranscriberAdapter("fast-whisper").transcribe(
                MediaInput(provided_transcript=transcript), token
            )
        storage = self._storage
        transcriber = self._transcriber
        stored_media = acquired.stored_media
        if storage is None or transcriber is None or stored_media is None:
            raise DomainError(
                "platform_subtitle_unavailable",
                ErrorCategory.RECIPE_FAILED,
                "Platform subtitles are unavailable; media fallback is not enabled",
            )
        media_path = storage.resolve_asset(
            stored_media,
            expected_job_id=acquisition_checkpoint.job_id,
            expected_attempt_id=acquisition_checkpoint.attempt_id,
        )
        return transcriber.transcribe(MediaInput(media_path=media_path), token)

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
            execution=LegacyModelExecutionBinding(
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
        return self._acquire_local(
            request,
            source,
            source_id=source_id,
            source_revision_id=source_revision_id,
            execution=execution,
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


@dataclass(frozen=True)
class _RuntimeCompilationProfile:
    repository: SqliteJobRepository
    binding: ModelExecutionBinding
    provider_profile: str
    provider_execution_policy: str

    def validate_selection(
        self,
        *,
        provider_profile: str,
        model_override: str | None,
    ) -> None:
        if (
            provider_profile != self.provider_profile
            or (
                model_override is not None
                and model_override != self.binding.model_identity
            )
        ):
            raise DomainError(
                "model_execution_binding_mismatch",
                ErrorCategory.INVALID_REQUEST,
                "The request does not match the frozen model execution binding",
            )

    def compilation_context(
        self,
        execution: VideoStepExecutionContext,
    ) -> VideoCompilationContext:
        return VideoCompilationContext(
            execution=ModelCallExecution(
                job_id=execution.job_id,
                step_id=execution.step_id,
                attempt_id=execution.attempt_id,
                authority=execution.authority,
                heartbeat=execution.heartbeat,
            ),
            cancellation_token=CancellationToken(
                self.repository, execution.job_id
            ),
        )

    @staticmethod
    def transcript_basis(value: str) -> TranscriptBasis:
        mapping = {
            TranscriptProvenance.PROVIDED.value: TranscriptBasis.HUMAN_TRANSCRIPT,
            TranscriptProvenance.PLATFORM.value: TranscriptBasis.PLATFORM_CAPTION,
            TranscriptProvenance.GENERATED.value: TranscriptBasis.ASR_TRANSCRIPT,
        }
        return mapping.get(value, TranscriptBasis.UNKNOWN)

    def compiler_identity(
        self,
        *,
        document_kind: VideoDocumentKind,
        behavior: Mapping[str, object],
    ) -> str:
        binding = self.binding
        payload = {
            "behavior": dict(behavior),
            "binding": {
                "context_window_tokens": binding.context_window_tokens,
                "credential_profile_ref": binding.credential_profile_ref,
                "max_concurrency": binding.max_concurrency,
                "max_output_tokens": binding.max_output_tokens,
                "model_identity": binding.model_identity,
                "provider_type": binding.provider_type,
                "schema_version": binding.schema_version,
                "supports_structured_output": binding.supports_structured_output,
                "supports_temperature": binding.supports_temperature,
                "timeout_seconds": binding.timeout_seconds,
            },
            "document_kind": document_kind.value,
            "provider_profile": self.provider_profile,
            "provider_execution_policy": self.provider_execution_policy,
            "schema_version": 1,
        }
        return sha256_digest(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )


@dataclass(frozen=True)
class _RuntimeDocumentKnowledgeCompiler:
    profile: _RuntimeCompilationProfile
    verifier_profile: _RuntimeCompilationProfile
    compiler: DocumentKnowledgeCompiler
    self_verifier: DocumentKnowledgeVerifier
    verifier: DocumentKnowledgeVerifier

    def model_identity(self) -> str:
        return self.profile.binding.model_identity

    def compilation_identity(self) -> str:
        binding = self.profile.binding
        verifier_binding = self.verifier_profile.binding
        return sha256_digest(
            json.dumps(
                {
                    "behavior": self.compiler.behavior_identity(),
                    "verification_behavior": self.verifier.behavior_identity(),
                    "composer_binding": {
                        "context_window_tokens": binding.context_window_tokens,
                        "credential_profile_ref": binding.credential_profile_ref,
                        "max_concurrency": binding.max_concurrency,
                        "max_output_tokens": binding.max_output_tokens,
                        "model_identity": binding.model_identity,
                        "provider_type": binding.provider_type,
                        "schema_version": binding.schema_version,
                        "supports_structured_output": (
                            binding.supports_structured_output
                        ),
                        "supports_temperature": binding.supports_temperature,
                        "timeout_seconds": binding.timeout_seconds,
                    },
                    "composer_provider_execution_policy": (
                        self.profile.provider_execution_policy
                    ),
                    "composer_provider_profile": self.profile.provider_profile,
                    "verifier_binding": {
                        "context_window_tokens": (
                            verifier_binding.context_window_tokens
                        ),
                        "credential_profile_ref": (
                            verifier_binding.credential_profile_ref
                        ),
                        "max_concurrency": verifier_binding.max_concurrency,
                        "max_output_tokens": verifier_binding.max_output_tokens,
                        "model_identity": verifier_binding.model_identity,
                        "provider_type": verifier_binding.provider_type,
                        "schema_version": verifier_binding.schema_version,
                        "supports_structured_output": (
                            verifier_binding.supports_structured_output
                        ),
                        "supports_temperature": (
                            verifier_binding.supports_temperature
                        ),
                        "timeout_seconds": verifier_binding.timeout_seconds,
                    },
                    "verifier_provider_execution_policy": (
                        self.verifier_profile.provider_execution_policy
                    ),
                    "verifier_provider_profile": (
                        self.verifier_profile.provider_profile
                    ),
                    "schema_version": 2,
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def compile(
        self,
        request: DocumentKnowledgeCompilationInput,
        *,
        execution: object,
    ) -> CompiledDocumentKnowledgeNoteV1:
        self.profile.validate_selection(
            provider_profile=request.provider_profile,
            model_override=request.model_override,
        )
        try:
            job_id = execution.job_id
            step_id = execution.step_id
            attempt_id = execution.attempt_id
            authority = execution.authority
            heartbeat = execution.heartbeat
        except AttributeError:
            raise DomainError(
                "document_knowledge_compilation_contract_invalid",
                ErrorCategory.INTERNAL,
                "Document execution context is invalid",
            ) from None
        return self.compiler.compile(
            DocumentKnowledgeCompilationRequestV1(
                schema_version=1,
                parsed=request.parsed,
                output_language=request.output_language,
                model_binding=self.profile.binding,
            ),
            DocumentCompilationContext(
                execution=ModelCallExecution(
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    authority=authority,
                    heartbeat=heartbeat,
                ),
                cancellation_token=CancellationToken(
                    self.profile.repository,
                    job_id,
                ),
            ),
        )

    def verify(
        self,
        request: DocumentKnowledgeVerificationInput,
        *,
        execution: object,
    ) -> DocumentKnowledgeVerificationV1:
        self_review = (
            request.verifier_provider_profile == self.profile.provider_profile
            and request.verifier_model_override
            == self.profile.binding.model_identity
        )
        selected_profile = self.profile if self_review else self.verifier_profile
        selected_verifier = self.self_verifier if self_review else self.verifier
        if (
            not self_review
            and selected_profile.binding.model_identity
            == self.profile.binding.model_identity
        ):
            raise DomainError(
                "document_knowledge_verifier_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The frozen independent Document verifier is unavailable",
            )
        selected_profile.validate_selection(
            provider_profile=request.verifier_provider_profile,
            model_override=request.verifier_model_override,
        )
        try:
            job_id = execution.job_id
            step_id = execution.step_id
            attempt_id = execution.attempt_id
            authority = execution.authority
            heartbeat = execution.heartbeat
        except AttributeError:
            raise DomainError(
                "document_knowledge_verification_contract_invalid",
                ErrorCategory.INTERNAL,
                "Document execution context is invalid",
            ) from None
        return selected_verifier.verify(
            DocumentKnowledgeVerificationRequestV1(
                schema_version=1,
                parsed=request.parsed,
                compiled=request.compiled,
                model_binding=selected_profile.binding,
            ),
            DocumentCompilationContext(
                execution=ModelCallExecution(
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    authority=authority,
                    heartbeat=heartbeat,
                ),
                cancellation_token=CancellationToken(
                    self.profile.repository,
                    job_id,
                ),
            ),
        )


class _RuntimeVideoKnowledgeCompiler:
    """Builds the frozen Core request around the shared knowledge compiler."""

    _MAX_CHUNK_DURATION_MS = 10 * 60 * 1_000
    _MAP_OUTPUT_TOKENS = 2_048
    _MAP_OUTPUT_BYTES_PER_TOKEN = 8
    _MAP_ITEM_LIMIT = 12
    _MAP_TITLE_CHARACTERS = 160
    _MAP_STATEMENT_CHARACTERS = 500
    _MAP_SEGMENT_REFS_PER_ITEM = 64
    _MAP_TERM_CANDIDATES = 32
    _MAP_WARNINGS = 16

    def __init__(
        self,
        *,
        profile: _RuntimeCompilationProfile,
        compiler: VideoKnowledgeCompiler,
    ) -> None:
        self._profile = profile
        self._compiler = compiler

    def compilation_identity(self) -> str:
        return self._profile.compiler_identity(
            document_kind=VideoDocumentKind.KNOWLEDGE_NOTE,
            behavior={
                "balanced_profile": {
                    "map_item_limit": self._MAP_ITEM_LIMIT,
                    "map_output_bytes_per_token": self._MAP_OUTPUT_BYTES_PER_TOKEN,
                    "map_output_tokens": self._MAP_OUTPUT_TOKENS,
                    "max_chunk_duration_ms": self._MAX_CHUNK_DURATION_MS,
                },
                "compiler_behavior": self._compiler.behavior_identity(),
                "knowledge_map_parser": 2,
                "knowledge_map_prompt": 4,
                "knowledge_map_stage": 2,
                "planner": 2,
            },
        )

    def compile(
        self,
        request: VideoKnowledgeCompilationInput,
        *,
        execution: VideoStepExecutionContext,
    ) -> CompiledVideoDocument:
        self._profile.validate_selection(
            provider_profile=request.provider_profile,
            model_override=request.model_override,
        )
        transcript_quality = assess_transcript_quality(
            TranscriptQualityInputV1(
                schema_version=1,
                transcript=request.transcript,
                transcript_basis=self._profile.transcript_basis(
                    request.transcript_basis
                ),
                source_duration_ms=request.source_duration_ms,
                detected_languages=tuple(
                    dict.fromkeys(
                        (request.source_language, request.transcript.language)
                    )
                ),
            )
        )
        binding = self._profile.binding
        map_output_tokens = min(self._MAP_OUTPUT_TOKENS, binding.max_output_tokens)
        max_request_bytes = binding.context_window_tokens
        map_output_bytes = min(
            map_output_tokens * self._MAP_OUTPUT_BYTES_PER_TOKEN,
            max_request_bytes // 4,
        )
        if map_output_bytes < 1:
            raise DomainError(
                "model_context_insufficient",
                ErrorCategory.INVALID_REQUEST,
                "The frozen model binding has no safe compilation budget",
            )
        map_item_limit = max(
            1,
            min(self._MAP_ITEM_LIMIT, map_output_tokens // 128),
        )
        planning_request = VideoCompilationPlanningRequestV1(
            schema_version=1,
            recipe_id=request.output.recipe_id,
            recipe_version=request.output.recipe_version,
            quality_profile=CompilationQualityProfile.BALANCED,
            transcript=request.transcript,
            transcript_quality=transcript_quality,
            model_binding=binding,
            stage_id="knowledge-map",
            stage_version=2,
            prompt_id="knowledge-map-balanced",
            prompt_version=4,
            prompt_overhead_tokens=1_400,
            prompt_overhead_bytes=1_400,
            reserved_output_tokens=binding.max_output_tokens,
            max_request_bytes=max_request_bytes,
            max_chunk_duration_ms=self._MAX_CHUNK_DURATION_MS,
            estimated_map_output_tokens_per_chunk=map_output_tokens,
            map_output_byte_budget_per_chunk=map_output_bytes,
            max_repair_attempts=1,
        )
        compilation_request = KnowledgeCompilationRequestV1(
            schema_version=1,
            planning_request=planning_request,
            source_title=request.source_title,
            output_language=request.output_language,
            style=request.style,
            screenshot_policy=request.screenshot_policy,
            map_parser_limits=KnowledgeMapParserLimitsV1(
                max_response_bytes=map_output_bytes,
                max_items=map_item_limit,
                max_title_characters=self._MAP_TITLE_CHARACTERS,
                max_statement_characters=self._MAP_STATEMENT_CHARACTERS,
                max_segment_refs_per_item=self._MAP_SEGMENT_REFS_PER_ITEM,
                max_term_candidates=self._MAP_TERM_CANDIDATES,
                max_term_characters=200,
                max_warnings=self._MAP_WARNINGS,
                max_warning_characters=500,
            ),
            composer_parser_limits=ComposerParserLimitsV1(
                max_response_bytes=min(
                    binding.max_output_tokens * 4,
                    128 * 1024,
                ),
                max_markdown_characters=min(
                    binding.max_output_tokens * 4,
                    128 * 1024,
                ),
                max_coverage_items=8_192,
                max_omissions=8_192,
                max_omission_reason_characters=500,
                max_warnings=128,
                max_warning_characters=500,
            ),
        )
        return self._compiler.compile(
            compilation_request,
            self._profile.compilation_context(execution),
        )


class _RuntimeFaithfulEditionCompiler:
    """Builds the frozen Core request around the faithful compiler."""

    def __init__(
        self,
        *,
        profile: _RuntimeCompilationProfile,
        compiler: FaithfulEditionCompiler,
    ) -> None:
        self._profile = profile
        self._compiler = compiler

    def compilation_identity(self) -> str:
        return self._profile.compiler_identity(
            document_kind=VideoDocumentKind.FAITHFUL_EDITION,
            behavior={
                "parser": 1,
                "planner": 1,
                "prompt": 1,
                "quality": 1,
                "repair_prompt": 1,
                "repair_stage": 1,
                "section_stage": 1,
            },
        )

    def compile(
        self,
        request: VideoFaithfulCompilationInput,
        *,
        execution: VideoStepExecutionContext,
    ) -> FaithfulCompiledVideoDocument:
        self._profile.validate_selection(
            provider_profile=request.provider_profile,
            model_override=request.model_override,
        )
        basis = self._profile.transcript_basis(request.transcript_basis)
        transcript_quality = assess_transcript_quality(
            TranscriptQualityInputV1(
                schema_version=1,
                transcript=request.transcript,
                transcript_basis=basis,
                source_duration_ms=request.source_duration_ms,
                detected_languages=tuple(
                    dict.fromkeys(
                        (request.source_language, request.transcript.language)
                    )
                ),
            )
        )
        binding = self._profile.binding
        max_request_bytes = (
            binding.context_window_tokens - binding.max_output_tokens
        )
        section_input_byte_budget = max_request_bytes // 2
        max_response_bytes = min(
            binding.max_output_tokens * 4,
            max_request_bytes,
            128 * 1024,
        )
        if section_input_byte_budget < 1 or max_response_bytes < 1:
            raise DomainError(
                "model_context_insufficient",
                ErrorCategory.INVALID_REQUEST,
                "The frozen model binding has no safe faithful editing budget",
            )
        target_language = (
            request.output_language
            if request.language_policy
            is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
            else None
        )
        faithful_request = FaithfulEditionRequestV1(
            schema_version=1,
            recipe_id=request.output.recipe_id,
            recipe_version=request.output.recipe_version,
            quality_profile=CompilationQualityProfile.BALANCED,
            transcript=request.transcript,
            transcript_quality=transcript_quality,
            transcript_basis=basis,
            source_title=request.source_title,
            source_language=request.source_language,
            language_policy=request.language_policy,
            target_language=target_language,
            model_binding=binding,
            max_request_bytes=max_request_bytes,
            section_input_byte_budget=section_input_byte_budget,
            reserved_output_tokens=binding.max_output_tokens,
            parser_limits=FaithfulEditionParserLimitsV1(
                max_response_bytes=max_response_bytes,
                max_title_characters=200,
                max_paragraphs=64,
                max_paragraph_characters=min(max_response_bytes, 32 * 1024),
                max_segment_refs_per_paragraph=256,
                max_key_points=64,
                max_uncertainties=64,
                max_auxiliary_text_characters=min(
                    max_response_bytes, 8 * 1024
                ),
                max_warnings=64,
            ),
            max_repair_attempts=1,
        )
        return self._compiler.compile(
            faithful_request,
            self._profile.compilation_context(execution),
        )


def _create_runtime_model_services(
    *,
    repository: SqliteJobRepository,
    model: LegacyModelBinding,
    binding: ModelExecutionBinding,
    provider_profile: str,
    result_root: Path,
) -> tuple[ModelCallCoordinator, _RuntimeCompilationProfile]:
    if type(provider_profile) is not str or not provider_profile.strip():
        raise DomainError(
            "model_execution_profile_required",
            ErrorCategory.INVALID_REQUEST,
            "A v2 provider profile must be explicitly bound",
        )
    if (
        not isinstance(binding, ModelExecutionBinding)
        or binding.provider_type != model.provider_kind
        or binding.model_identity != model.model_identity
    ):
        raise DomainError(
            "model_execution_binding_mismatch",
            ErrorCategory.INVALID_REQUEST,
            "The frozen Core binding does not match the legacy provider binding",
        )
    bridge = model.bridge
    if bridge is None or not callable(getattr(bridge, "complete_request", None)):
        raise DomainError(
            "model_bridge_required",
            ErrorCategory.POLICY_DENIED,
            "A frozen-request model bridge is required for v2 compilation",
        )
    execution_policy = getattr(bridge, "execution_policy_identity", None)
    provider_execution_policy = (
        execution_policy()
        if callable(execution_policy)
        else "provider-controlled-v1"
    )
    if (
        type(provider_execution_policy) is not str
        or not provider_execution_policy.strip()
    ):
        raise DomainError(
            "model_bridge_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The model bridge execution policy identity must not be empty",
        )
    executor = LegacyModelExecutor(binding=binding, bridge=bridge)
    coordinator = ModelCallCoordinator(
        operation_store=repository,
        result_store=ModelOperationResultStore(result_root / "model-operations"),
        executor=executor,
    )
    profile = _RuntimeCompilationProfile(
        repository=repository,
        binding=binding,
        provider_profile=provider_profile,
        provider_execution_policy=provider_execution_policy,
    )
    return coordinator, profile


def _create_runtime_compilers(
    *,
    repository: SqliteJobRepository,
    model: LegacyModelBinding,
    binding: ModelExecutionBinding | None,
    provider_profile: str | None,
    result_root: Path,
) -> tuple[
    _RuntimeVideoKnowledgeCompiler | None,
    _RuntimeFaithfulEditionCompiler | None,
]:
    if binding is None:
        if provider_profile is not None:
            raise DomainError(
                "model_execution_profile_invalid",
                ErrorCategory.INVALID_REQUEST,
                "A v2 provider profile requires a frozen Core binding",
            )
        return None, None
    if provider_profile is None:
        raise DomainError(
            "model_execution_profile_required",
            ErrorCategory.INVALID_REQUEST,
            "A v2 provider profile must be explicitly bound",
        )
    coordinator, profile = _create_runtime_model_services(
        repository=repository,
        model=model,
        binding=binding,
        provider_profile=provider_profile,
        result_root=result_root,
    )
    return (
        _RuntimeVideoKnowledgeCompiler(
            profile=profile,
            compiler=VideoKnowledgeCompiler(coordinator),
        ),
        _RuntimeFaithfulEditionCompiler(
            profile=profile,
            compiler=FaithfulEditionCompiler(coordinator),
        ),
    )


class AllToNoteRuntime:
    def __init__(
        self,
        sdk: AllToNoteSDK,
        job_repository: SqliteJobRepository,
        *,
        workspace_instance_id: str | None = None,
    ) -> None:
        self._sdk = sdk
        self.job_repository = job_repository
        self._workspace_instance_id = workspace_instance_id

    @property
    def workspace_instance_id(self) -> str | None:
        return self._workspace_instance_id

    def submit(
        self,
        request: ProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> ProduceSubmission:
        return self._sdk.submit(request, execution_owner=execution_owner)

    def submit_video(
        self,
        request: VideoProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> JobSnapshot:
        return self._sdk.submit_video(
            request,
            execution_owner=execution_owner,
        )

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        return self._sdk.wait_job(job_id, event_sink)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._sdk.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._sdk.cancel_job(job_id)


def _create_video_runtime(
    service: VideoService,
    repository: SqliteJobRepository,
    *,
    workspace_instance_id: str | None = None,
) -> AllToNoteRuntime:
    endpoint = VideoRecipeAdapter(service)
    registry = RecipeRegistry(
        (descriptor, endpoint) for descriptor in VIDEO_DESCRIPTORS
    )
    job_control = JobExecutionRouter(
        repository,
        (*(
            (
                service.execution_binding(
                    descriptor.key.recipe_id,
                    descriptor.key.recipe_version,
                ),
                service,
            )
            for descriptor in VIDEO_DESCRIPTORS
        ), (
            JobExecutionBinding(
                recipe_id="alltonote.legacy",
                recipe_version=1,
                executor_id="alltonote.video",
                executor_version=1,
                pack_id="media-basic",
                pack_version="legacy-v1",
            ),
            service,
        )),
    )
    sdk = AllToNoteSDK(
        ProduceService(registry),
        job_control,
        adapt_video_produce_request,
    )
    return AllToNoteRuntime(
        sdk,
        repository,
        workspace_instance_id=workspace_instance_id,
    )


def _create_document_runtime(
    service: DocumentService,
    repository: SqliteJobRepository,
) -> AllToNoteRuntime:
    registry = RecipeRegistry(
        ((DOCUMENT_NOTE_V1, DocumentRecipeAdapter(service)),)
    )
    job_control = JobExecutionRouter(
        repository,
        ((service.execution_binding, service),),
    )
    return AllToNoteRuntime(
        AllToNoteSDK(
            ProduceService(registry),
            job_control,
            adapt_video_produce_request,
        ),
        repository,
    )


def _checkpoint_payload_is_valid(payload: bytes) -> bool:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return type(value) is dict and type(value.get("step")) is str


def _document_checkpoint_payload_is_valid(payload: bytes) -> bool:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return type(value) is dict


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


def _resolve_execution_owner_id(
    owner_id: str | None,
    resource_owner: ResourceOwner | None,
) -> str:
    if owner_id is not None:
        return owner_id
    if resource_owner is not None:
        return resource_owner.process_instance_id
    return f"runtime-{uuid4().hex}"


_PackPortResolver = Callable[
    [JobPackEnvironmentSnapshot],
    tuple[VideoSourcePort, TranscriptPort | None, str],
]


def _create_platform_video_runtime_components(
    machine_root: Path,
    *,
    source: VideoSourcePort,
    source_metadata: Mapping[str, Mapping[str, object]],
    transcriber: TranscriptPort | None = None,
    generated_transcriber_identity: str | None = None,
    model: LegacyModelBinding,
    model_execution_binding: ModelExecutionBinding | None = None,
    model_execution_profile: str | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    pack_environment: JobPackEnvironmentSnapshot | None = None,
    pack_port_resolver: _PackPortResolver | None = None,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    workspace_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    resource_lease_store: ResourceLeaseStorePort | None = None,
    resource_owner: ResourceOwner | None = None,
    require_existing_job_store: bool = False,
) -> tuple[AllToNoteRuntime, VideoService]:
    resolved_machine_root = Path(machine_root).resolve(
        strict=require_existing_job_store
    )
    if not require_existing_job_store:
        resolved_machine_root.mkdir(parents=True, exist_ok=True)
    repository_factory = (
        SqliteJobRepository.open_existing
        if require_existing_job_store
        else SqliteJobRepository.open
    )
    repository = repository_factory(resolved_machine_root / "job-store", clock=clock)
    storage = FileAttemptStorage(
        resolved_machine_root / "attempts",
        repository,
        validators={CHECKPOINT_SCHEMA: _checkpoint_payload_is_valid},
    )
    result_root = resolved_machine_root / "external-results"
    operations = _PlatformVideoOperations(
        repository,
        source,
        source_metadata,
        model,
        result_root,
        storage=storage,
        transcriber=transcriber,
    )
    portable = IWikiPortableGateway()

    def resolve_source_identity(
        workspace_root: Path,
        resolved_source: ResolvedVideoSource,
    ):
        return VerifiedSourceIdentityRegistry(
            workspace_root,
            cache=repository,
            truth=portable,
        ).resolve_verified(
            resolved_source.connector_id,
            resolved_source.canonical_identity,
        )

    pack_environment_activator = None
    if pack_port_resolver is not None:
        def activate_pack_environment(
            snapshot: JobPackEnvironmentSnapshot,
        ) -> tuple[VideoRecipeOperations, str]:
            resolved_source, resolved_transcriber, identity = (
                pack_port_resolver(snapshot)
            )
            return (
                _PlatformVideoOperations(
                    repository,
                    resolved_source,
                    source_metadata,
                    model,
                    result_root,
                    storage=storage,
                    transcriber=resolved_transcriber,
                ),
                identity,
            )

        pack_environment_activator = activate_pack_environment
    knowledge_compiler, faithful_compiler = _create_runtime_compilers(
        repository=repository,
        model=model,
        binding=model_execution_binding,
        provider_profile=model_execution_profile,
        result_root=result_root,
    )
    service = VideoService(
        repository,
        storage,
        portable,
        operations,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id=_resolve_execution_owner_id(owner_id, resource_owner),
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
        knowledge_compiler=knowledge_compiler,
        faithful_compiler=faithful_compiler,
        current_config_snapshot=current_config_snapshot,
        generated_transcriber_identity=(
            generated_transcriber_identity or "fake/transcriber-v1"
        ),
        pack_environment=pack_environment,
        pack_environment_activator=pack_environment_activator,
        source_identity_resolver=resolve_source_identity,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
    )
    return (
        _create_video_runtime(
            service,
            repository,
            workspace_instance_id=workspace_instance_id,
        ),
        service,
    )


def create_platform_video_runtime(
    machine_root: Path,
    *,
    source: VideoSourcePort,
    source_metadata: Mapping[str, Mapping[str, object]],
    transcriber: TranscriptPort | None = None,
    generated_transcriber_identity: str | None = None,
    model: LegacyModelBinding,
    model_execution_binding: ModelExecutionBinding | None = None,
    model_execution_profile: str | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    pack_environment: JobPackEnvironmentSnapshot | None = None,
    pack_port_resolver: _PackPortResolver | None = None,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    workspace_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    resource_lease_store: ResourceLeaseStorePort | None = None,
    resource_owner: ResourceOwner | None = None,
    require_existing_job_store: bool = False,
) -> AllToNoteRuntime:
    runtime, _ = _create_platform_video_runtime_components(
        machine_root,
        source=source,
        source_metadata=source_metadata,
        transcriber=transcriber,
        generated_transcriber_identity=generated_transcriber_identity,
        model=model,
        model_execution_binding=model_execution_binding,
        model_execution_profile=model_execution_profile,
        current_config_snapshot=current_config_snapshot,
        pack_environment=pack_environment,
        pack_port_resolver=pack_port_resolver,
        owner_id=owner_id,
        local_instance_id=local_instance_id,
        workspace_instance_id=workspace_instance_id,
        clock=clock,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
        require_existing_job_store=require_existing_job_store,
    )
    return runtime


def create_document_runtime(
    machine_root: Path,
    *,
    worker_config: DoclingWorkerConfig,
    model: LegacyModelBinding | None = None,
    model_execution_binding: ModelExecutionBinding | None = None,
    model_execution_profile: str | None = None,
    verifier_model: LegacyModelBinding | None = None,
    verifier_model_execution_binding: ModelExecutionBinding | None = None,
    verifier_model_execution_profile: str | None = None,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    resource_lease_store: ResourceLeaseStorePort | None = None,
    resource_owner: ResourceOwner | None = None,
    require_existing_job_store: bool = False,
) -> AllToNoteRuntime:
    verifier_options = (
        verifier_model,
        verifier_model_execution_binding,
        verifier_model_execution_profile,
    )
    if model is None and (
        model_execution_binding is not None
        or model_execution_profile is not None
        or any(value is not None for value in verifier_options)
    ):
        raise DomainError(
            "model_execution_binding_mismatch",
            ErrorCategory.INVALID_REQUEST,
            "Document model bindings require a provider model",
        )
    if model is not None and any(value is not None for value in verifier_options) and not all(
        value is not None for value in verifier_options
    ):
        raise DomainError(
            "model_execution_binding_mismatch",
            ErrorCategory.INVALID_REQUEST,
            "Document verifier requires one complete frozen model profile",
        )
    parser = DoclingWorkerParser(worker_config)
    parser.doctor()
    resolved_machine_root = Path(machine_root).resolve(
        strict=require_existing_job_store
    )
    if not require_existing_job_store:
        resolved_machine_root.mkdir(parents=True, exist_ok=True)
    repository_factory = (
        SqliteJobRepository.open_existing
        if require_existing_job_store
        else SqliteJobRepository.open
    )
    repository = repository_factory(
        resolved_machine_root / "job-store", clock=clock
    )
    storage = FileAttemptStorage(
        resolved_machine_root / "attempts",
        repository,
        validators={
            DOCUMENT_CHECKPOINT_SCHEMA: _document_checkpoint_payload_is_valid,
        },
    )
    portable = IWikiPortableGateway()
    knowledge_compiler = None
    if model is None:
        pass
    else:
        if model_execution_binding is None or model_execution_profile is None:
            raise DomainError(
                "model_execution_profile_required",
                ErrorCategory.INVALID_REQUEST,
                "Document knowledge compilation requires a frozen model profile",
            )
        coordinator, profile = _create_runtime_model_services(
            repository=repository,
            model=model,
            binding=model_execution_binding,
            provider_profile=model_execution_profile,
            result_root=storage.root,
        )
        if any(value is not None for value in verifier_options):
            verifier_coordinator, verifier_profile = _create_runtime_model_services(
                repository=repository,
                model=verifier_model,
                binding=verifier_model_execution_binding,
                provider_profile=verifier_model_execution_profile,
                result_root=storage.root,
            )
        else:
            verifier_coordinator = coordinator
            verifier_profile = profile
        knowledge_compiler = _RuntimeDocumentKnowledgeCompiler(
            profile=profile,
            verifier_profile=verifier_profile,
            compiler=DocumentKnowledgeCompiler(coordinator),
            self_verifier=DocumentKnowledgeVerifier(coordinator),
            verifier=DocumentKnowledgeVerifier(verifier_coordinator),
        )

    def resolve_source_identity(
        workspace_root: Path,
        connector_id: str,
        canonical_identity: str,
    ) -> SourceIdentityBinding | None:
        return VerifiedSourceIdentityRegistry(
            workspace_root,
            cache=repository,
            truth=portable,
        ).resolve_verified(connector_id, canonical_identity)

    service = DocumentService(
        repository,
        storage,
        parser,
        portable,
        work_root=storage.root,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id=_resolve_execution_owner_id(owner_id, resource_owner),
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
        source_identity_resolver=resolve_source_identity,
        knowledge_compiler=knowledge_compiler,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
    )
    return _create_document_runtime(service, repository)


def _resolve_document_worker_config(
    paths: RuntimePaths,
    environ: Mapping[str, str],
) -> DoclingWorkerConfig:
    resolved = resolve_document_basic_pack_paths(paths, environ)
    if resolved is None:
        raise DomainError(
            "document_pack_unavailable",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "The document-basic Pack installation is incomplete",
        )
    python_executable, artifacts_path = resolved
    if not python_executable.is_file() or not artifacts_path.is_dir():
        raise DomainError(
            "document_pack_unavailable",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Install the compatible document-basic Pack before producing PDF notes",
        )
    return DoclingWorkerConfig(
        python_executable=python_executable,
        artifacts_path=artifacts_path,
        backend_root=Path(__file__).resolve().parent.parent,
    )


def _workspace_resource_admission(
    paths: RuntimePaths,
    instance: WorkspaceInstance,
) -> tuple[str, MachineResourceLeaseStore, ResourceOwner]:
    process_instance_id = f"runtime-{uuid4().hex}"
    return (
        process_instance_id,
        MachineResourceLeaseStore.open(paths.data_dir / "machine"),
        ResourceOwner(
            instance.workspace_identity,
            process_instance_id,
            process_id=os.getpid(),
        ),
    )


def create_document_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
    runtime_paths: RuntimePaths | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    requested_model_identity: str | None = None,
    requested_provider_profile: str | None = None,
    requested_verifier_model_identity: str | None = None,
    requested_verifier_provider_profile: str | None = None,
    require_existing_job_store: bool = False,
) -> AllToNoteRuntime:
    if local_app_data is not None and runtime_paths is not None:
        raise ValueError("runtime_path_override_conflict")
    paths = runtime_paths or resolve_runtime_paths(
        local_data_parent=local_app_data or _default_local_app_data()
    )
    trusted_root = paths.workspace_registry_parent
    paths.assert_outside_workspace(workspace_root)
    worker_config = _resolve_document_worker_config(paths, os.environ)
    trusted_root.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceInstanceRegistry(
        trusted_root,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace_root)
    process_instance_id, resource_lease_store, resource_owner = (
        _workspace_resource_admission(paths, instance)
    )
    status = CodexAppServerStatusService.get_status()
    snapshot_values = (
        current_config_snapshot.values
        if current_config_snapshot is not None
        else {}
    )
    configured_providers = snapshot_values.get("providers", {})
    if not isinstance(configured_providers, Mapping):
        configured_providers = {}
    provider_profile = requested_provider_profile or "default"
    if requested_provider_profile is None:
        configured_profile = snapshot_values.get(
            "default_provider_profile",
            "default",
        )
        if type(configured_profile) is str and configured_profile.strip():
            provider_profile = configured_profile
    provider_values = configured_providers.get(provider_profile, {})
    if not isinstance(provider_values, Mapping):
        provider_values = {}
    configured_model = provider_values.get("default_model")
    selected_model = requested_model_identity or (
        configured_model
        if type(configured_model) is str and configured_model.strip()
        else status.default_model
    )
    verifier_provider_profile = requested_verifier_provider_profile
    if (
        verifier_provider_profile is None
        and requested_model_identity is None
        and requested_provider_profile is None
    ):
        configured_verifier_profile = snapshot_values.get(
            "default_verifier_provider_profile"
        )
        if (
            type(configured_verifier_profile) is str
            and configured_verifier_profile.strip()
        ):
            verifier_provider_profile = configured_verifier_profile
    if (
        (requested_verifier_model_identity is None)
        != (requested_verifier_provider_profile is None)
    ):
        raise DomainError(
            "model_execution_binding_mismatch",
            ErrorCategory.INVALID_REQUEST,
            "Document verifier selection must include profile and model",
        )
    selected_verifier_model = requested_verifier_model_identity
    if verifier_provider_profile is not None and selected_verifier_model is None:
        verifier_values = configured_providers.get(
            verifier_provider_profile,
            {},
        )
        if not isinstance(verifier_values, Mapping):
            verifier_values = {}
        verifier_provider_type = verifier_values.get("type")
        if verifier_provider_type != "codex-app-server":
            raise DomainError(
                "model_provider_unsupported",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The configured Document verifier provider is not supported",
            )
        configured_verifier_model = verifier_values.get("default_model")
        if (
            type(configured_verifier_model) is not str
            or not configured_verifier_model.strip()
        ):
            raise DomainError(
                "document_verifier_model_required",
                ErrorCategory.INVALID_REQUEST,
                "The configured Document verifier profile has no frozen model",
            )
        selected_verifier_model = configured_verifier_model
    if selected_verifier_model is not None and selected_verifier_model == selected_model:
        raise DomainError(
            "document_verifier_not_independent",
            ErrorCategory.INVALID_REQUEST,
            "The configured Document verifier must use a different model",
        )
    model = None
    binding = None
    verifier_model = None
    verifier_binding = None
    if status.ready and selected_model:
        bridge = CodexAppServerCompletionBridge(
            model_identity=selected_model
        )
        model = LegacyModelBinding(
            provider_kind="codex-app-server",
            model_identity=selected_model,
            bridge=bridge,
            capabilities=LegacyModelCapabilities(),
        )
        binding = ModelExecutionBinding(
            schema_version=1,
            provider_type="codex-app-server",
            model_identity=selected_model,
            credential_profile_ref="codex/local-login",
            context_window_tokens=128_000,
            max_output_tokens=16_000,
            max_concurrency=1,
            supports_structured_output=True,
            supports_temperature=False,
            timeout_seconds=600,
        )
        if selected_verifier_model is not None:
            verifier_bridge = CodexAppServerCompletionBridge(
                model_identity=selected_verifier_model
            )
            verifier_model = LegacyModelBinding(
                provider_kind="codex-app-server",
                model_identity=selected_verifier_model,
                bridge=verifier_bridge,
                capabilities=LegacyModelCapabilities(),
            )
            verifier_binding = ModelExecutionBinding(
                schema_version=1,
                provider_type="codex-app-server",
                model_identity=selected_verifier_model,
                credential_profile_ref="codex/local-login",
                context_window_tokens=128_000,
                max_output_tokens=16_000,
                max_concurrency=1,
                supports_structured_output=True,
                supports_temperature=False,
                timeout_seconds=600,
            )
    elif selected_verifier_model is not None:
        raise DomainError(
            "codex_app_server_unavailable",
            ErrorCategory.POLICY_DENIED,
            "The configured Document verifier model is unavailable",
        )
    return create_document_runtime(
        instance.machine_root,
        worker_config=worker_config,
        model=model,
        model_execution_binding=binding,
        model_execution_profile=provider_profile if model is not None else None,
        verifier_model=verifier_model,
        verifier_model_execution_binding=verifier_binding,
        verifier_model_execution_profile=(
            verifier_provider_profile if verifier_model is not None else None
        ),
        owner_id=process_instance_id,
        local_instance_id=instance.instance_id,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
        require_existing_job_store=require_existing_job_store,
    )


def _create_local_video_runtime_components(
    machine_root: Path,
    *,
    source: VideoSourcePort,
    source_metadata: Mapping[str, Mapping[str, object]],
    transcriber: TranscriptPort,
    model: LegacyModelBinding,
    model_execution_binding: ModelExecutionBinding | None = None,
    model_execution_profile: str | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    screenshot_adapter_factory: Callable[
        [FileAttemptStorage, SqliteJobRepository], ScreenshotPort
    ]
    | None = None,
    resource_lease_store: ResourceLeaseStorePort | None = None,
    resource_owner: ResourceOwner | None = None,
) -> tuple[AllToNoteRuntime, VideoService]:
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
    result_root = resolved_machine_root / "external-results"
    operations = _LocalVideoOperations(
        repository,
        storage,
        source,
        source_metadata,
        transcriber,
        model,
        result_root,
        (
            screenshot_adapter_factory(storage, repository)
            if screenshot_adapter_factory is not None
            else FFmpegScreenshotAdapter(storage, repository)
        ),
    )
    knowledge_compiler, faithful_compiler = _create_runtime_compilers(
        repository=repository,
        model=model,
        binding=model_execution_binding,
        provider_profile=model_execution_profile,
        result_root=result_root,
    )
    service = VideoService(
        repository,
        storage,
        IWikiPortableGateway(),
        operations,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id=_resolve_execution_owner_id(owner_id, resource_owner),
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
        knowledge_compiler=knowledge_compiler,
        faithful_compiler=faithful_compiler,
        current_config_snapshot=current_config_snapshot,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
    )
    return _create_video_runtime(service, repository), service


def create_local_video_runtime(
    machine_root: Path,
    *,
    source: VideoSourcePort,
    source_metadata: Mapping[str, Mapping[str, object]],
    transcriber: TranscriptPort,
    model: LegacyModelBinding,
    model_execution_binding: ModelExecutionBinding | None = None,
    model_execution_profile: str | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    screenshot_adapter_factory: Callable[
        [FileAttemptStorage, SqliteJobRepository], ScreenshotPort
    ]
    | None = None,
    resource_lease_store: ResourceLeaseStorePort | None = None,
    resource_owner: ResourceOwner | None = None,
) -> AllToNoteRuntime:
    runtime, _ = _create_local_video_runtime_components(
        machine_root,
        source=source,
        source_metadata=source_metadata,
        transcriber=transcriber,
        model=model,
        model_execution_binding=model_execution_binding,
        model_execution_profile=model_execution_profile,
        current_config_snapshot=current_config_snapshot,
        owner_id=owner_id,
        local_instance_id=local_instance_id,
        clock=clock,
        screenshot_adapter_factory=screenshot_adapter_factory,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
    )
    return runtime


def _create_fake_runtime_components(
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
    workspace_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    operation_hooks: Mapping[str, Callable[[Callable[[], None]], None]] | None = None,
    screenshot_requests: tuple[ScreenshotRequest, ...] = (),
    current_config_snapshot: JobConfigSnapshot | None = None,
    resource_lease_store: ResourceLeaseStorePort | None = None,
    resource_owner: ResourceOwner | None = None,
) -> tuple[AllToNoteRuntime, VideoService]:
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
        owner_id=_resolve_execution_owner_id(owner_id, resource_owner),
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
        current_config_snapshot=current_config_snapshot,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
    )
    return (
        _create_video_runtime(
            service,
            repository,
            workspace_instance_id=workspace_instance_id,
        ),
        service,
    )


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
    workspace_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    operation_hooks: Mapping[str, Callable[[Callable[[], None]], None]] | None = None,
    screenshot_requests: tuple[ScreenshotRequest, ...] = (),
    current_config_snapshot: JobConfigSnapshot | None = None,
    resource_lease_store: ResourceLeaseStorePort | None = None,
    resource_owner: ResourceOwner | None = None,
) -> AllToNoteRuntime:
    runtime, _ = _create_fake_runtime_components(
        machine_root,
        calls=calls,
        capabilities=capabilities,
        quality_fail=quality_fail,
        crash_after_commit_once=crash_after_commit_once,
        crash_operation_once=crash_operation_once,
        call_log_path=call_log_path,
        owner_id=owner_id,
        local_instance_id=local_instance_id,
        workspace_instance_id=workspace_instance_id,
        clock=clock,
        operation_hooks=operation_hooks,
        screenshot_requests=screenshot_requests,
        current_config_snapshot=current_config_snapshot,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
    )
    return runtime


def create_fake_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    operation_hooks: Mapping[str, Callable[[Callable[[], None]], None]] | None = None,
) -> AllToNoteRuntime:
    trusted_root = local_app_data or _default_local_app_data()
    paths = resolve_runtime_paths(local_data_parent=trusted_root)
    paths.assert_outside_workspace(workspace_root)
    trusted_root.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceInstanceRegistry(
        trusted_root,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace_root)
    process_instance_id, resource_lease_store, resource_owner = (
        _workspace_resource_admission(paths, instance)
    )
    return create_fake_runtime(
        instance.machine_root,
        owner_id=process_instance_id,
        local_instance_id=instance.instance_id,
        workspace_instance_id=instance.instance_id,
        current_config_snapshot=current_config_snapshot,
        operation_hooks=operation_hooks,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
    )


def _pack_identity(
    pack: ResolvedOfficialVideoPack,
) -> ExecutionPackIdentity:
    return ExecutionPackIdentity(
        pack_id=pack.pack_id,
        pack_version=pack.pack_version,
        platform=pack.platform,
        manifest_sha256=pack.manifest_sha256,
    )


def _bilibili_cookie() -> str | None:
    from app.services.cookie_manager import CookieConfigManager

    value = CookieConfigManager().get("bilibili")
    return value if isinstance(value, str) and value else None


def create_codex_app_server_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
    runtime_paths: RuntimePaths | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    execution_pack_environment: JobPackEnvironmentSnapshot | None = None,
    require_existing_job_store: bool = False,
) -> AllToNoteRuntime:
    """Create the real URL producer using the locally authenticated Codex CLI."""

    status = CodexAppServerStatusService.get_status()
    if not status.ready or not status.default_model:
        raise DomainError(
            "codex_app_server_unavailable",
            ErrorCategory.POLICY_DENIED,
            "The local Codex CLI must be installed, signed in, and have a default model",
    )
    if local_app_data is not None and runtime_paths is not None:
        raise ValueError("runtime_path_override_conflict")
    paths = runtime_paths or resolve_runtime_paths(
        local_data_parent=local_app_data or _default_local_app_data()
    )
    trusted_root = paths.workspace_registry_parent
    paths.assert_outside_workspace(workspace_root)
    trusted_root.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceInstanceRegistry(
        trusted_root,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace_root)
    model_identity = status.default_model
    bridge = CodexAppServerCompletionBridge(model_identity=model_identity)
    model = LegacyModelBinding(
        provider_kind="codex-app-server",
        model_identity=model_identity,
        bridge=bridge,
        capabilities=LegacyModelCapabilities(),
    )
    binding = ModelExecutionBinding(
        schema_version=1,
        provider_type="codex-app-server",
        model_identity=model_identity,
        credential_profile_ref="codex/local-login",
        context_window_tokens=128_000,
        max_output_tokens=16_000,
        max_concurrency=2,
        supports_structured_output=True,
        supports_temperature=False,
        timeout_seconds=600,
    )
    pack_resolver = OfficialVideoPackResolver(
        paths,
        trusted_keys=official_video_pack_trust_keys(),
    )
    if execution_pack_environment is None:
        media_pack = pack_resolver.resolve_active(MEDIA_BASIC)
        try:
            transcribe_pack = pack_resolver.resolve_active(TRANSCRIBE_CPU)
        except DomainError as error:
            if error.code != "pack_unavailable":
                raise
            transcribe_pack = None
        source = PackedBilibiliVideoSourceAdapter(
            LegacyVideoSourceAdapter(local_machine_id=instance.instance_id),
            media_pack,
            cookie_resolver=_bilibili_cookie,
        )
        transcriber = (
            PackedCpuTranscriber(transcribe_pack)
            if transcribe_pack is not None
            else None
        )
        pack_identities = (_pack_identity(media_pack),)
        if transcribe_pack is not None:
            pack_identities += (_pack_identity(transcribe_pack),)
        pack_environment = JobPackEnvironmentSnapshot(
            schema_version=1,
            packs=pack_identities,
        )
        generated_transcriber_identity = (
            transcriber.identity
            if transcriber is not None
            else "transcribe-cpu/unavailable"
        )
    else:
        pack_environment = execution_pack_environment

    def resolve_pack_ports(
        snapshot: JobPackEnvironmentSnapshot,
    ) -> tuple[VideoSourcePort, TranscriptPort | None, str]:
        pack_ids = frozenset(pack.pack_id for pack in snapshot.packs)
        if pack_ids not in (
            frozenset({MEDIA_BASIC.pack_id}),
            frozenset({MEDIA_BASIC.pack_id, TRANSCRIBE_CPU.pack_id}),
        ):
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The frozen Video Pack environment is unsupported",
            )
        try:
            media_identity = snapshot.pack(MEDIA_BASIC.pack_id)
        except ValueError as error:
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The exact Video Pack generation is unavailable",
            ) from error
        transcribe_identity = next(
            (
                pack
                for pack in snapshot.packs
                if pack.pack_id == TRANSCRIBE_CPU.pack_id
            ),
            None,
        )
        if media_identity.pack_version != MEDIA_BASIC.pack_version or (
            transcribe_identity is not None
            and transcribe_identity.pack_version != TRANSCRIBE_CPU.pack_version
        ):
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The exact Video Pack generation is unavailable",
            )
        resolved_media = pack_resolver.resolve_exact(
            MEDIA_BASIC,
            media_identity.manifest_sha256,
        )
        resolved_transcribe = (
            pack_resolver.resolve_exact(
                TRANSCRIBE_CPU,
                transcribe_identity.manifest_sha256,
            )
            if transcribe_identity is not None
            else None
        )
        if _pack_identity(resolved_media) != media_identity or (
            transcribe_identity is not None
            and (
                resolved_transcribe is None
                or _pack_identity(resolved_transcribe) != transcribe_identity
            )
        ):
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The exact Video Pack generation is unavailable",
            )
        resolved_source = PackedBilibiliVideoSourceAdapter(
            LegacyVideoSourceAdapter(local_machine_id=instance.instance_id),
            resolved_media,
            cookie_resolver=_bilibili_cookie,
        )
        resolved_transcriber = (
            PackedCpuTranscriber(resolved_transcribe)
            if resolved_transcribe is not None
            else None
        )
        return (
            resolved_source,
            resolved_transcriber,
            (
                resolved_transcriber.identity
                if resolved_transcriber is not None
                else "transcribe-cpu/unavailable"
            ),
        )

    if execution_pack_environment is not None:
        source, transcriber, generated_transcriber_identity = (
            resolve_pack_ports(pack_environment)
        )

    process_instance_id, resource_lease_store, resource_owner = (
        _workspace_resource_admission(paths, instance)
    )
    return create_platform_video_runtime(
        instance.machine_root,
        source=source,
        source_metadata={},
        transcriber=transcriber,
        generated_transcriber_identity=generated_transcriber_identity,
        model=model,
        model_execution_binding=binding,
        model_execution_profile="default",
        owner_id=process_instance_id,
        local_instance_id=instance.instance_id,
        workspace_instance_id=instance.instance_id,
        current_config_snapshot=current_config_snapshot,
        pack_environment=pack_environment,
        pack_port_resolver=resolve_pack_ports,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
        require_existing_job_store=require_existing_job_store,
    )


def _default_local_app_data() -> Path:
    return resolve_runtime_paths().workspace_registry_parent


__all__ = [
    "AllToNoteRuntime",
    "FakeCallCounts",
    "VideoPreflightCapabilities",
    "create_fake_runtime",
    "create_fake_runtime_for_workspace",
    "create_codex_app_server_runtime_for_workspace",
    "create_document_runtime_for_workspace",
    "create_local_video_runtime",
    "create_platform_video_runtime",
]
