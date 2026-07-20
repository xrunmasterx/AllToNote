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
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
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
from app.core.jobs.model import CheckpointMetadata
from app.core.portable.bundle_assembler import DisplayAssetInput, VideoSourceMetadata
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
from app.runtime_paths import resolve_runtime_paths
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
                "knowledge_map_prompt": 2,
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
            prompt_version=2,
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
    ) -> None:
        self._sdk = sdk
        self.job_repository = job_repository

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        return self._sdk.submit_video(request)

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        return self._sdk.wait_job(job_id, event_sink)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._sdk.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._sdk.cancel_job(job_id)


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
    model_execution_binding: ModelExecutionBinding | None = None,
    model_execution_profile: str | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
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
    result_root = resolved_machine_root / "external-results"
    operations = _PlatformVideoOperations(
        repository,
        source,
        source_metadata,
        model,
        result_root,
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
        owner_id=owner_id or f"runtime-{uuid4().hex}",
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
        knowledge_compiler=knowledge_compiler,
        faithful_compiler=faithful_compiler,
        current_config_snapshot=current_config_snapshot,
    )
    return AllToNoteRuntime(AllToNoteSDK(service), repository)


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
        owner_id=owner_id or f"runtime-{uuid4().hex}",
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
        knowledge_compiler=knowledge_compiler,
        faithful_compiler=faithful_compiler,
        current_config_snapshot=current_config_snapshot,
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
    current_config_snapshot: JobConfigSnapshot | None = None,
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
        current_config_snapshot=current_config_snapshot,
    )
    return AllToNoteRuntime(AllToNoteSDK(service), repository)


def create_fake_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
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
    return create_fake_runtime(
        instance.machine_root,
        local_instance_id=instance.instance_id,
        current_config_snapshot=current_config_snapshot,
    )


def create_codex_app_server_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
) -> AllToNoteRuntime:
    """Create the real URL producer using the locally authenticated Codex CLI."""

    status = CodexAppServerStatusService.get_status()
    if not status.ready or not status.default_model:
        raise DomainError(
            "codex_app_server_unavailable",
            ErrorCategory.POLICY_DENIED,
            "The local Codex CLI must be installed, signed in, and have a default model",
    )
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
    source = LegacyVideoSourceAdapter(local_machine_id=instance.instance_id)
    return create_platform_video_runtime(
        instance.machine_root,
        source=source,
        source_metadata={},
        model=model,
        model_execution_binding=binding,
        model_execution_profile="default",
        local_instance_id=instance.instance_id,
        current_config_snapshot=current_config_snapshot,
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
    "create_local_video_runtime",
    "create_platform_video_runtime",
]
