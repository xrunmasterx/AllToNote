from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from errno import EACCES, ENOSYS
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from filelock import FileLock, Timeout

from app.core.application.checkpoint_runner import (
    CheckpointedStepExecutionContext,
    CheckpointedStepRunner,
)
from app.core.application.job_service import JobService
from app.core.application.video_acquisition import VideoAcquisition
from app.core.application.video_checkpoints import (
    CandidateCheckpoint as _CandidateCheckpoint,
    decode_acquired as _decode_acquired,
    decode_draft as _decode_draft,
    decode_preflight as _decode_preflight,
    decode_revision as _decode_revision,
    decode_screenshots as _decode_screenshots,
    decode_source as _decode_source,
    decode_transcript as _decode_transcript,
    decode_validation as _decode_validation,
    encode_acquired as _encode_acquired,
    encode_draft as _encode_draft,
    encode_screenshots as _encode_screenshots,
    encode_source as _encode_source,
    encode_transcript as _encode_transcript,
    encode_validation as _encode_validation,
)
from app.core.config.events import JOB_CONFIG_SNAPSHOT_EVENT
from app.core.config.model import JobConfigSnapshot
from app.core.domain.ids import new_typed_id, sha256_digest
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    GeneratedVideoDraft,
    JobSnapshot,
    JobState,
    QualityOverall,
    ResolvedVideoOutput,
    ScreenshotPlanItem,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    VideoDocumentKind,
    VideoProducedDocument,
    VideoProduceRequest,
    VIDEO_INPUT_SNAPSHOT_EVENT,
    parse_video_input_snapshot_payload,
    video_input_snapshot_payload,
)
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.jobs.model import (
    Attempt,
    AttemptState,
    CheckpointMetadata,
    JobExecutionBinding,
    JobExecutionOwner,
)
from app.core.jobs.resource_lease import (
    HEAVY_PRODUCTION_RESOURCE_NAME,
    ExecutionAuthority,
    JobExecutionAuthority,
    ResourceLease,
    ResourceLeaseStorePort,
    ResourceOwner,
)
from app.core.packs.events import (
    JOB_PACK_ENVIRONMENT_EVENT,
    JobPackEnvironmentSnapshot,
    parse_job_pack_environment_payload,
)
from app.core.portable.artifacts import PortableArtifactRef, build_transcript
from app.core.portable.bundle_assembler import (
    BundleAssembler,
    DisplayAssetInput,
    FeaturePackProvenance,
    ReceiptProvenance,
    StepAttemptSummary,
    VideoArtifactIds,
    VideoBundleInput,
    VideoDraftBundleInput,
    VideoSourceMetadata,
)
from app.core.portable.evidence import (
    EvidenceSet,
    build_evidence_set,
    rewrite_segment_citations,
)
from app.core.portable.quality import evaluate_video_draft
from app.core.recipes.video.compilation.quality import (
    CoverageOmissionV1 as TextCoverageOmissionV1,
    KnowledgeNoteCandidateV1,
    assess_knowledge_note,
)
from app.core.ports.jobs import (
    AttemptMetadataRepositoryPort,
    AttemptQueryRepositoryPort,
    AttemptStoragePort,
    JobClaimRepositoryPort,
    PortableCommitReceipt,
    SourceIdentityBinding,
    VideoResultPlan,
    VideoExecutionRepositoryPort,
)
from app.core.ports.portable import PortableCommitResultPort, PortableWorkspacePort
from app.core.ports.source import ResolvedVideoSource

if TYPE_CHECKING:
    from app.core.application.faithful_edition_compiler import (
        FaithfulCompiledVideoDocument,
    )
    from app.core.application.video_compiler import CompiledVideoDocument


RUNTIME_VERSION = "0.1.0"
CHECKPOINT_SCHEMA = "video-step.v1"
_VIDEO_INPUT_DIRECTORY = "video-inputs"
_VIDEO_INPUT_COPY_CHUNK_BYTES = 1024 * 1024
_CANDIDATE_ASSEMBLY_BEHAVIOR = "portable-output-profile-v2"
_DOCUMENT_COMPILATION_BEHAVIOR = (
    "projection-v2/finalization-v1/citation-format-v1"
)
_HISTORICAL_COMMIT_CANDIDATE_BEHAVIORS_V1 = (
    "linked-screenshot-draft-v1",
    "linked-screenshot-draft-v2",
)


if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _video_snapshot_lock_is_ordinary(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat_module.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and not VideoService._stat_is_reparse_point(metadata)
    )


def _open_video_snapshot_lock_matches(path: Path, descriptor: int) -> bool:
    try:
        metadata = path.lstat()
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        _video_snapshot_lock_is_ordinary(path)
        and metadata.st_dev == opened.st_dev
        and metadata.st_ino == opened.st_ino
    )


class _VideoInputFileLock(FileLock):
    """Lock one stable ordinary file without truncating a linked target."""

    def _acquire(self) -> None:
        lock_path = Path(self.lock_file)
        if os.path.lexists(lock_path) and not _video_snapshot_lock_is_ordinary(
            lock_path
        ):
            raise OSError("Video input snapshot lock is unsafe")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, self._context.mode)
        except OSError as error:
            if os.path.lexists(lock_path) and not _video_snapshot_lock_is_ordinary(
                lock_path
            ):
                raise OSError("Video input snapshot lock is unsafe") from error
            if os.name == "nt" and error.errno == EACCES:
                return
            raise
        if not _open_video_snapshot_lock_matches(lock_path, descriptor):
            os.close(descriptor)
            raise OSError("Video input snapshot lock is unsafe")
        try:
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if os.name == "nt":
                if error.errno != EACCES:
                    raise
            elif error.errno == ENOSYS:
                raise OSError(
                    "Video input snapshot filesystem does not support locking"
                ) from error
            return
        if not _open_video_snapshot_lock_matches(lock_path, descriptor):
            os.close(descriptor)
            raise OSError("Video input snapshot lock is unsafe")
        self._context.lock_file_fd = descriptor

    def _release(self) -> None:
        descriptor = self._context.lock_file_fd
        if descriptor is None:
            return
        self._context.lock_file_fd = None
        if os.name == "nt":
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
_SUPPORTED_VIDEO_RECIPE_KEYS = frozenset(
    {
        ("alltonote.video-course-note", 1),
        ("alltonote.video-producer", 2),
    }
)
_SCHEDULER_LEASE_TTL_SECONDS = 300
_SCHEDULER_HEARTBEAT_INTERVAL_SECONDS = 30.0
_AUTHORITY_LOSS_CODES = frozenset(
    {"attempt_fenced", "job_claim_fenced", "scheduler_lease_lost"}
)
PREFLIGHT_CHECKS = (
    "request_schema",
    "workspace_contract",
    "recipe_version",
    "runtime_version",
    "video_feature_pack",
    "job_store",
    "work_directory",
    "disk_space",
    "source_capability",
    "transcript_capability",
    "model_capability",
    "screenshot_capability",
    "screenshot_model_incompatibility",
    "ffmpeg_loadability",
    "model_loadability",
    "transcriber_loadability",
    "effective_config",
    "credential_reference",
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
_T = TypeVar("_T")
_REQUEST_KEYS_V1 = frozenset(
    {
        "request_schema_version",
        "workspace_root",
        "input_value",
        "recipe_id",
        "recipe_version",
        "provider_profile",
        "model_override",
        "transcriber_profile",
        "output_language",
        "quality_preset",
        "style",
        "screenshot_policy",
        "provided_transcript",
    }
)
_REQUEST_KEYS_V2 = _REQUEST_KEYS_V1 | frozenset(
    {"requested_outputs", "faithful_language_policy", "output_bindings"}
)


@dataclass(frozen=True)
class VideoPreflightCapabilities:
    runtime_version: str = RUNTIME_VERSION
    video_feature_pack: bool = True
    source_capability: bool = True
    transcript_capability: bool = True
    model_capability: bool = True
    screenshot_capability: bool = True
    screenshot_model_compatible: bool = True
    ffmpeg_loadable: bool = True
    model_loadable: bool = True
    transcriber_loadable: bool = True
    effective_config_valid: bool = True
    credential_references_resolvable: bool = True
    required_free_bytes: int = 1


VideoStepExecutionContext = CheckpointedStepExecutionContext


def _checkpoint_error() -> DomainError:
    return DomainError(
        "checkpoint_content_invalid",
        ErrorCategory.INTERNAL,
        "Checkpoint content is invalid",
    )


def _encode_object(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _is_sha256_digest(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _decode_compilation_identity(payload: bytes, step_id: str) -> str:
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError):
        raise _checkpoint_error() from None
    if (
        type(value) is not dict
        or frozenset(value) != frozenset({"identity", "step"})
        or value.get("step") != step_id
        or not _is_sha256_digest(value.get("identity"))
    ):
        raise _checkpoint_error()
    return value["identity"]


class VideoRecipeOperations(Protocol):
    def preflight_capabilities(
        self, request: VideoProduceRequest
    ) -> VideoPreflightCapabilities: ...

    def resolve_source(
        self,
        request: VideoProduceRequest,
        *,
        source_id: str,
        source_revision_id: str,
    ) -> ResolvedVideoSource: ...

    def acquire(
        self,
        request: VideoProduceRequest,
        source: ResolvedVideoSource,
        *,
        source_id: str,
        source_revision_id: str,
        execution: VideoStepExecutionContext,
    ) -> VideoAcquisition: ...

    def transcribe(
        self,
        request: VideoProduceRequest,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> TranscriptDocument: ...

    def generate_draft(
        self,
        request: VideoProduceRequest,
        transcript: TranscriptDocument,
        *,
        execution: VideoStepExecutionContext,
    ) -> GeneratedVideoDraft: ...

    def screenshots(
        self,
        plan: tuple[ScreenshotPlanItem, ...],
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> tuple[DisplayAssetInput, ...]: ...

    def after_portable_commit(self, result: PortableCommitResultPort) -> None: ...


PackEnvironmentActivator = Callable[
    [JobPackEnvironmentSnapshot],
    tuple[VideoRecipeOperations, str],
]
SourceIdentityResolver = Callable[
    [Path, ResolvedVideoSource],
    SourceIdentityBinding | None,
]


@dataclass(frozen=True)
class VideoKnowledgeCompilationInput:
    output: ResolvedVideoOutput
    source_id: str
    source_revision_id: str
    source_title: str
    source_language: str
    source_duration_ms: int
    transcript_basis: str
    transcript: TranscriptDocument
    output_language: str
    style: str
    screenshot_policy: ScreenshotPolicy
    provider_profile: str
    model_override: str | None


class VideoKnowledgeCompilerPort(Protocol):
    """Narrow service boundary for the v2 knowledge-note compiler."""

    def compilation_identity(self) -> str: ...

    def compile(
        self,
        request: VideoKnowledgeCompilationInput,
        *,
        execution: VideoStepExecutionContext,
    ) -> CompiledVideoDocument: ...


@dataclass(frozen=True)
class VideoFaithfulCompilationInput:
    output: ResolvedVideoOutput
    source_title: str
    source_language: str
    source_duration_ms: int
    transcript_basis: str
    transcript: TranscriptDocument
    language_policy: FaithfulLanguagePolicy
    output_language: str
    provider_profile: str
    model_override: str | None


class VideoFaithfulCompilerPort(Protocol):
    """Narrow service boundary for the v2 faithful-edition compiler."""

    def compilation_identity(self) -> str: ...

    def compile(
        self,
        request: VideoFaithfulCompilationInput,
        *,
        execution: VideoStepExecutionContext,
    ) -> FaithfulCompiledVideoDocument: ...


@dataclass(frozen=True)
class _DetachedVideoInput:
    path: Path
    sha256: str
    byte_length: int
    original_title: str


class _VideoServiceRepositoryPort(
    VideoExecutionRepositoryPort,
    JobClaimRepositoryPort,
    AttemptMetadataRepositoryPort,
    AttemptQueryRepositoryPort,
    Protocol,
):
    pass


class VideoService:
    def __init__(
        self,
        repository: _VideoServiceRepositoryPort,
        attempt_storage: AttemptStoragePort,
        portable: PortableWorkspacePort,
        operations: VideoRecipeOperations,
        *,
        checkpoint_reader: Callable[[CheckpointMetadata], bytes],
        owner_id: str,
        work_root: Path,
        local_instance_id: str,
        knowledge_compiler: VideoKnowledgeCompilerPort | None = None,
        faithful_compiler: VideoFaithfulCompilerPort | None = None,
        current_config_snapshot: JobConfigSnapshot | None = None,
        generated_transcriber_identity: str = "fake/transcriber-v1",
        pack_environment: JobPackEnvironmentSnapshot | None = None,
        pack_environment_activator: PackEnvironmentActivator | None = None,
        source_identity_resolver: SourceIdentityResolver | None = None,
        resource_lease_store: ResourceLeaseStorePort | None = None,
        resource_owner: ResourceOwner | None = None,
        adopted_resource_lease: ResourceLease | None = None,
        expected_job_authority: JobExecutionAuthority | None = None,
    ) -> None:
        if (resource_lease_store is None) != (resource_owner is None):
            raise ValueError("resource_admission_pair_required")
        if (adopted_resource_lease is None) != (expected_job_authority is None):
            raise ValueError("resource_adoption_pair_required")
        if resource_owner is not None and adopted_resource_lease is not None:
            raise ValueError("resource_admission_mode_conflict")
        if (
            resource_owner is not None
            and owner_id != resource_owner.process_instance_id
        ):
            raise ValueError("resource_admission_owner_mismatch")
        if adopted_resource_lease is not None and (
            adopted_resource_lease.resource_name
            != HEAVY_PRODUCTION_RESOURCE_NAME
            or owner_id != adopted_resource_lease.owner.process_instance_id
            or expected_job_authority is None
            or owner_id != expected_job_authority.owner_id
        ):
            raise ValueError("resource_adoption_owner_mismatch")
        self._repository = repository
        self._job_service = JobService(repository)
        self._attempt_storage = attempt_storage
        self._portable = portable
        self._operations = operations
        self._checkpoint_reader = checkpoint_reader
        self._owner_id = owner_id
        self._work_root = work_root
        self._local_instance_id = local_instance_id
        self._knowledge_compiler = knowledge_compiler
        self._faithful_compiler = faithful_compiler
        self._current_config_snapshot = current_config_snapshot
        self._generated_transcriber_identity = generated_transcriber_identity
        if pack_environment is not None:
            if not isinstance(pack_environment, JobPackEnvironmentSnapshot):
                raise ValueError("pack_environment_invalid")
            pack_environment.pack("media-basic")
        self._submission_pack_environment = pack_environment
        self._execution_pack_environment = pack_environment
        self._pack_environment_activator = pack_environment_activator
        self._source_identity_resolver = source_identity_resolver
        self._resource_lease_store = resource_lease_store
        self._resource_owner = resource_owner
        self._adopted_resource_lease = adopted_resource_lease
        self._expected_job_authority = expected_job_authority
        self._active_resource_lease: ResourceLease | None = None
        self._submitted_config_snapshots: dict[str, JobConfigSnapshot] = {}
        self._execution_lock = threading.Lock()
        self._checkpoint_runner = CheckpointedStepRunner(
            repository,
            attempt_storage,
            checkpoint_reader=lambda metadata: self._checkpoint_reader(metadata),
            checkpoint_schema=CHECKPOINT_SCHEMA,
            scheduler_lease_ttl_seconds=_SCHEDULER_LEASE_TTL_SECONDS,
            heartbeat_interval_seconds=_SCHEDULER_HEARTBEAT_INTERVAL_SECONDS,
            additional_heartbeat=self._heartbeat_resource_lease,
        )

    def execution_binding(
        self,
        recipe_id: str,
        recipe_version: int,
    ) -> JobExecutionBinding:
        media_pack = (
            self._submission_pack_environment.pack("media-basic")
            if self._submission_pack_environment is not None
            else None
        )
        return JobExecutionBinding(
            recipe_id=recipe_id,
            recipe_version=recipe_version,
            executor_id="alltonote.video",
            executor_version=1,
            pack_id=(
                media_pack.pack_id
                if media_pack is not None
                else "media-basic"
            ),
            pack_version=(
                media_pack.pack_version
                if media_pack is not None
                else "builtin-v1"
            ),
        )

    def submit_video(
        self,
        request: VideoProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> JobSnapshot:
        if not isinstance(request, VideoProduceRequest):
            raise DomainError(
                "video_produce_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Video production requires a versioned request",
            )
        detached_input = (
            self._snapshot_detached_input(request)
            if execution_owner is JobExecutionOwner.ENGINE
            else None
        )
        config_snapshot = request.config_snapshot or self._current_config_snapshot
        initial_events = tuple(
            event
            for event in (
                (
                    (JOB_CONFIG_SNAPSHOT_EVENT, config_snapshot)
                    if config_snapshot is not None
                    else None
                ),
                (
                    (
                        JOB_PACK_ENVIRONMENT_EVENT,
                        self._submission_pack_environment,
                    )
                    if self._submission_pack_environment is not None
                    else None
                ),
                (
                    (
                        VIDEO_INPUT_SNAPSHOT_EVENT,
                        video_input_snapshot_payload(
                            detached_input.sha256,
                            detached_input.byte_length,
                        ),
                    )
                    if detached_input is not None
                    else None
                ),
            )
            if event is not None
        )
        recipe_key = (request.recipe_id, request.recipe_version)
        bound_recipe_id, bound_recipe_version = (
            recipe_key
            if recipe_key in _SUPPORTED_VIDEO_RECIPE_KEYS
            else ("alltonote.video-course-note", 1)
        )
        snapshot = self._job_service.submit(
            self._job_request_payload(request),
            initial_events=initial_events,
            execution_binding=self.execution_binding(
                bound_recipe_id,
                bound_recipe_version,
            ),
            execution_owner=execution_owner,
        )
        if config_snapshot is not None:
            self._submitted_config_snapshots[snapshot.job_id] = config_snapshot
        return snapshot

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._snapshot(job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._job_service.cancel(job_id)

    def wait_job(self, job_id: str) -> JobSnapshot:
        with self._execution_lock:
            return self._wait_job_locked(job_id)

    def _wait_job_locked(self, job_id: str) -> JobSnapshot:
        snapshot = self._snapshot(job_id)
        if snapshot.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.WAITING_FOR_INPUT,
        }:
            return snapshot
        self._assert_pack_environment_compatible(job_id)
        self._assert_config_compatible(job_id)
        request = self._load_request(job_id)
        self._assert_expected_job_authority(job_id)
        self._acquire_resource_lease()
        try:
            try:
                claim = self._repository.claim_job(
                    job_id,
                    self._owner_id,
                    ttl_seconds=_SCHEDULER_LEASE_TTL_SECONDS,
                )
            except DomainError as error:
                if error.code != "job_not_claimable":
                    raise
                snapshot = self._snapshot(job_id)
                if snapshot.state in {
                    JobState.SUCCEEDED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.WAITING_FOR_INPUT,
                }:
                    return snapshot
                raise
            authority = claim.authority
            try:
                if (
                    self._expected_job_authority is not None
                    and authority != self._expected_job_authority
                ):
                    raise DomainError(
                        "job_claim_fenced",
                        ErrorCategory.CONFLICT,
                        "Job execution authority is expired or fenced",
                    )
                snapshot = self._snapshot(job_id)
                if snapshot.state in {
                    JobState.SUCCEEDED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.WAITING_FOR_INPUT,
                }:
                    return snapshot
                active_attempt = claim.active_attempt
                try:
                    if (
                        active_attempt is not None
                        and active_attempt.state is AttemptState.RUNNING
                        and active_attempt.fencing_token != authority.fencing_token
                    ):
                        active_attempt = self._repository.take_over_running_attempt(
                            job_id, active_attempt.attempt_id, authority
                        )
                        if active_attempt.state is AttemptState.CANCELLED:
                            return self._snapshot(job_id)
                        unknown = self._repository.reconcile_external_operations_after_process_loss(
                            job_id, authority
                        )
                        if unknown:
                            self._repository.pause_for_external_outcome_atomic(
                                job_id,
                                active_attempt.attempt_id,
                                authority,
                            )
                            return self._snapshot(job_id)
                    if active_attempt is not None and active_attempt.step_id == "commit":
                        return self._reconcile_commit(request, active_attempt, authority)
                    return self._execute(
                        job_id,
                        request,
                        authority,
                        resumed_attempt=active_attempt,
                    )
                except DomainError as error:
                    if error.code in {
                        "job_store_busy",
                        "machine_lease_store_busy",
                    }:
                        raise
                    try:
                        self._fail_job(job_id, active_attempt, authority, error)
                    except DomainError as convergence_error:
                        if convergence_error.code in _AUTHORITY_LOSS_CODES:
                            raise error from convergence_error
                        raise
                    return self._snapshot(job_id)
            finally:
                try:
                    self._repository.release_job_claim(authority)
                except Exception:
                    # The lease has a bounded TTL; cleanup must not replace the Job result
                    # or the primary execution error.
                    pass
        finally:
            self._release_resource_lease()

    def _acquire_resource_lease(self) -> None:
        if self._adopted_resource_lease is not None:
            self._active_resource_lease = self._adopted_resource_lease
            self._adopted_resource_lease = None
            self._heartbeat_resource_lease()
            return
        if self._expected_job_authority is not None:
            raise DomainError(
                "resource_lease_lost",
                ErrorCategory.CONFLICT,
                "Machine resource lease is expired or fenced",
            )
        if self._resource_lease_store is None or self._resource_owner is None:
            return
        self._active_resource_lease = self._resource_lease_store.acquire(
            HEAVY_PRODUCTION_RESOURCE_NAME,
            self._resource_owner,
            ttl_seconds=_SCHEDULER_LEASE_TTL_SECONDS,
        )

    def _assert_expected_job_authority(self, job_id: str) -> None:
        if (
            self._expected_job_authority is not None
            and self._expected_job_authority.job_id != job_id
        ):
            raise DomainError(
                "job_claim_fenced",
                ErrorCategory.CONFLICT,
                "Job execution authority is expired or fenced",
            )

    def _heartbeat_resource_lease(self) -> None:
        if self._active_resource_lease is None:
            return
        self._active_resource_lease = self._active_resource_lease.heartbeat(
            ttl_seconds=_SCHEDULER_LEASE_TTL_SECONDS
        )

    def _release_resource_lease(self) -> None:
        lease = self._active_resource_lease
        self._active_resource_lease = None
        if lease is None:
            return
        try:
            lease.release()
        except Exception:
            pass

    def _snapshot_detached_input(
        self,
        request: VideoProduceRequest,
    ) -> _DetachedVideoInput | None:
        candidate = Path(request.input_value.strip()).expanduser().absolute()
        if not os.path.lexists(candidate):
            return None
        source = self._validated_local_input(candidate)
        staging = self._work_root / _VIDEO_INPUT_DIRECTORY / ".staging"
        temporary = staging / f".source-{uuid4().hex}.tmp"
        try:
            self._ensure_video_input_directory(staging, create=True)
            initial = source.lstat()
            digest = hashlib.sha256()
            byte_length = 0
            with source.open("rb") as input_stream, temporary.open("xb") as output:
                opened = os.fstat(input_stream.fileno())
                if not self._ordinary_input_stat(opened) or not os.path.samestat(
                    initial, opened
                ):
                    raise self._video_input_changed()
                while True:
                    chunk = input_stream.read(_VIDEO_INPUT_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    byte_length += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                final = os.fstat(input_stream.fileno())
            current = source.lstat()
            if (
                not os.path.samestat(opened, final)
                or not os.path.samestat(opened, current)
                or not self._ordinary_input_stat(final)
                or final.st_size != opened.st_size
                or final.st_mtime_ns != opened.st_mtime_ns
                or byte_length != opened.st_size
            ):
                raise self._video_input_changed()
            sha256 = "sha256:" + digest.hexdigest()
            target = self._detached_input_path(request, sha256)
            self._ensure_video_input_directory(target.parent, create=True)
            snapshot = _DetachedVideoInput(
                path=target,
                sha256=sha256,
                byte_length=byte_length,
                original_title=source.stem or "video",
            )
            lock_path = target.parent / ".snapshot.lock"
            self._ensure_video_input_lock(lock_path)
            with _VideoInputFileLock(str(lock_path), timeout=30):
                self._ensure_video_input_lock(lock_path)
                self._ensure_video_input_directory(target.parent, create=False)
                if os.path.lexists(target):
                    self._verify_detached_input(snapshot)
                else:
                    os.replace(temporary, target)
                    self._verify_detached_input(snapshot)
            return snapshot
        except Timeout as error:
            raise DomainError(
                "video_input_snapshot_unavailable",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Local Video input snapshot is busy",
            ) from error
        except DomainError:
            raise
        except OSError as error:
            raise DomainError(
                "video_input_snapshot_unavailable",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Local video input could not be snapshotted for Engine execution",
            ) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _execution_input(
        self,
        job_id: str,
        request: VideoProduceRequest,
    ) -> _DetachedVideoInput | None:
        events = tuple(
            event
            for event in self._repository.list_events(job_id)
            if event.event_type == VIDEO_INPUT_SNAPSHOT_EVENT
        )
        if not events:
            return None
        if len(events) != 1:
            raise DomainError(
                "video_input_snapshot_invalid",
                ErrorCategory.INTERNAL,
                "Stored local Video input snapshot binding is invalid",
            )
        try:
            payload = parse_video_input_snapshot_payload(events[0].payload_json)
        except ValueError as error:
            raise DomainError(
                "video_input_snapshot_invalid",
                ErrorCategory.INTERNAL,
                "Stored local Video input snapshot binding is invalid",
            ) from error
        sha256 = payload["sha256"]
        byte_length = payload["byte_length"]
        assert isinstance(sha256, str)
        assert type(byte_length) is int
        snapshot = _DetachedVideoInput(
            path=self._detached_input_path(request, sha256),
            sha256=sha256,
            byte_length=byte_length,
            original_title=Path(request.input_value.strip()).stem.strip() or "video",
        )
        self._verify_detached_input(snapshot)
        return snapshot

    def _detached_input_path(
        self,
        request: VideoProduceRequest,
        sha256: str,
    ) -> Path:
        suffix = Path(request.input_value.strip()).suffix
        if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix) is None:
            suffix = ".media"
        digest = sha256.removeprefix("sha256:")
        return (
            self._work_root
            / _VIDEO_INPUT_DIRECTORY
            / digest[:2]
            / digest
            / f"source{suffix}"
        )

    def _verify_detached_input(self, snapshot: _DetachedVideoInput) -> None:
        try:
            self._ensure_video_input_directory(snapshot.path.parent, create=False)
            initial = snapshot.path.lstat()
            if not self._ordinary_input_stat(initial):
                raise OSError("Video input snapshot is unsafe")
            digest = hashlib.sha256()
            byte_length = 0
            with snapshot.path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if not self._ordinary_input_stat(opened) or not os.path.samestat(
                    initial, opened
                ):
                    raise OSError("Video input snapshot changed")
                while True:
                    chunk = stream.read(_VIDEO_INPUT_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    byte_length += len(chunk)
                    if byte_length > snapshot.byte_length:
                        raise OSError("Video input snapshot size changed")
                    digest.update(chunk)
                final = os.fstat(stream.fileno())
            current = snapshot.path.lstat()
            if (
                not os.path.samestat(opened, final)
                or not os.path.samestat(opened, current)
                or opened.st_size != final.st_size
                or opened.st_mtime_ns != final.st_mtime_ns
                or byte_length != snapshot.byte_length
                or "sha256:" + digest.hexdigest() != snapshot.sha256
            ):
                raise OSError("Video input snapshot changed")
        except OSError as error:
            raise DomainError(
                "video_input_snapshot_invalid",
                ErrorCategory.CONFLICT,
                "The Engine-bound local Video input snapshot is missing or invalid",
            ) from error

    def _ensure_video_input_directory(
        self,
        directory: Path,
        *,
        create: bool,
    ) -> None:
        try:
            relative = directory.relative_to(self._work_root)
        except ValueError as error:
            raise OSError("Video input snapshot escaped managed storage") from error
        root_resolved = self._work_root.resolve(strict=True)
        current = self._work_root
        for component in (None, *relative.parts):
            if component is not None:
                current /= component
            if not os.path.lexists(current):
                if not create:
                    raise OSError("Video input snapshot directory is missing")
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            current_stat = current.lstat()
            if (
                not stat_module.S_ISDIR(current_stat.st_mode)
                or self._stat_is_reparse_point(current_stat)
                or not current.resolve(strict=True).is_relative_to(root_resolved)
            ):
                raise OSError("Video input snapshot directory is unsafe")

    @staticmethod
    def _ensure_video_input_lock(path: Path) -> None:
        if not os.path.lexists(path):
            return
        if not _video_snapshot_lock_is_ordinary(path):
            raise OSError("Video input snapshot lock is unsafe")

    def _validated_local_input(self, path: Path) -> Path:
        try:
            for component in (path, *path.parents):
                if not os.path.lexists(component):
                    continue
                component_stat = component.lstat()
                if self._stat_is_reparse_point(component_stat):
                    raise OSError("Local video input path contains a reparse point")
            resolved = path.resolve(strict=True)
            if not self._ordinary_input_stat(resolved.lstat()):
                raise OSError("Local video input is not an ordinary file")
            return resolved
        except OSError as error:
            raise DomainError(
                "video_input_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Local video input must be a safe ordinary file",
            ) from error

    @classmethod
    def _ordinary_input_stat(cls, value: os.stat_result) -> bool:
        return (
            stat_module.S_ISREG(value.st_mode)
            and value.st_nlink == 1
            and not cls._stat_is_reparse_point(value)
        )

    @staticmethod
    def _stat_is_reparse_point(value: os.stat_result) -> bool:
        reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return stat_module.S_ISLNK(value.st_mode) or bool(
            getattr(value, "st_file_attributes", 0) & reparse_flag
        )

    @staticmethod
    def _video_input_changed() -> DomainError:
        return DomainError(
            "video_input_changed",
            ErrorCategory.CONFLICT,
            "Local video input changed while it was being snapshotted",
        )

    def _execute(
        self,
        job_id: str,
        request: VideoProduceRequest,
        authority: ExecutionAuthority,
        *,
        resumed_attempt: Attempt | None,
    ) -> JobSnapshot:
        request_hash = self._request_hash(request)
        detached_input = self._execution_input(job_id, request)
        execution_request = (
            replace(request, input_value=str(detached_input.path))
            if detached_input is not None
            else request
        )
        self._checkpointed(
            job_id,
            "preflight",
            request_hash,
            authority,
            lambda _execution: self._preflight(request),
            encode=lambda value: _encode_object(
                {"step": "preflight", "policy_hash": value}
            ),
            decode=_decode_preflight,
            reuse=False,
            resumed_attempt=resumed_attempt,
        )
        ids = self._ids(job_id)
        source = self._checkpointed(
            job_id,
            "resolve_source",
            request_hash,
            authority,
            lambda _execution: self._operations.resolve_source(
                execution_request,
                source_id=ids["source"],
                source_revision_id=ids["revision"],
            ),
            encode=_encode_source,
            decode=_decode_source,
            resumed_attempt=resumed_attempt,
        )
        if detached_input is not None and (
            source.connector_id != "local"
            or source.content_sha256 != detached_input.sha256
        ):
            raise DomainError(
                "video_input_snapshot_invalid",
                ErrorCategory.INTERNAL,
                "Resolved local Video input does not match its Engine binding",
            )
        source_id = ids["source"]
        if self._source_identity_resolver is not None:
            binding = self._source_identity_resolver(
                Path(request.workspace_root),
                source,
            )
            if binding is not None:
                if (
                    binding.connector_id != source.connector_id
                    or binding.canonical_identity != source.canonical_identity
                ):
                    raise DomainError(
                        "source_identity_resolution_invalid",
                        ErrorCategory.INTERNAL,
                        "Resolved source identity does not match the video source",
                    )
                source_id = binding.source_id
        acquired = self._checkpointed(
            job_id,
            "acquire",
            request_hash,
            authority,
            lambda execution: self._operations.acquire(
                execution_request,
                source,
                source_id=source_id,
                source_revision_id=ids["revision"],
                execution=execution,
            ),
            encode=_encode_acquired,
            decode=_decode_acquired,
            resumed_attempt=resumed_attempt,
        )
        if detached_input is not None:
            acquired = replace(
                acquired,
                metadata=replace(
                    acquired.metadata,
                    title=detached_input.original_title,
                ),
            )
        acquisition_checkpoint = self._repository.latest_checkpoint(job_id, "acquire")
        if acquisition_checkpoint is None:
            raise _checkpoint_error()
        transcript = self._checkpointed(
            job_id,
            "normalize_transcript",
            request_hash,
            authority,
            lambda execution: self._operations.transcribe(
                request,
                acquired,
                acquisition_checkpoint=acquisition_checkpoint,
                execution=execution,
            ),
            encode=_encode_transcript,
            decode=_decode_transcript,
            resumed_attempt=resumed_attempt,
        )
        self._checkpointed(
            job_id,
            "create_source_revision",
            request_hash,
            authority,
            lambda _execution: acquired.metadata.source_revision_id,
            encode=lambda value: _encode_object(
                {"step": "create_source_revision", "revision_id": value}
            ),
            decode=_decode_revision,
            resumed_attempt=resumed_attempt,
        )
        bundle_input = self._build_input(
            request,
            job_id=job_id,
            acquired=acquired,
            acquisition_checkpoint=acquisition_checkpoint,
            transcript=transcript,
            authority=authority,
            request_hash=request_hash,
            resumed_attempt=resumed_attempt,
        )
        candidate_input_hash = self._candidate_assembly_input_hash(request_hash)
        checkpoint = self._checkpointed(
            job_id,
            "assemble_candidate_bundle",
            candidate_input_hash,
            authority,
            lambda _execution: self._assemble(bundle_input),
            encode=lambda value: value.encode(),
            decode=_CandidateCheckpoint.decode,
            resumed_attempt=resumed_attempt,
        )
        self._checkpointed(
            job_id,
            "quality_and_portable_validation",
            request_hash,
            authority,
            lambda _execution: self._validate_candidate(request, checkpoint),
            encode=_encode_validation,
            decode=lambda payload: _decode_validation(payload, checkpoint),
            resumed_attempt=resumed_attempt,
        )
        commit_attempt = self._repository.create_attempt(
            job_id,
            "commit",
            authority=authority,
        )
        commit_attempt = self._repository.start_attempt(
            commit_attempt.attempt_id, authority
        )
        return self._commit(request, checkpoint, commit_attempt, authority)

    def _reconcile_commit(
        self,
        request: VideoProduceRequest,
        attempt: Attempt,
        authority: ExecutionAuthority,
    ) -> JobSnapshot:
        checkpoint = self._load_commit_candidate_checkpoint(
            attempt.job_id, request
        )
        return self._commit(request, checkpoint, attempt, authority)

    def _build_input(
        self,
        request: VideoProduceRequest,
        *,
        job_id: str,
        acquired: VideoAcquisition,
        acquisition_checkpoint: CheckpointMetadata,
        transcript: TranscriptDocument,
        authority: ExecutionAuthority,
        request_hash: str,
        resumed_attempt: Attempt | None,
    ) -> VideoBundleInput:
        source = acquired.metadata
        job = self._repository.get_job_details(job_id)[0]
        ids = self._ids(job_id)
        transcript_payload = build_transcript(
            source.source_revision_id, transcript.language, transcript.segments
        )
        transcript_ref = PortableArtifactRef(
            ids["bundle"], ids["transcript"], sha256_digest(transcript_payload)
        )
        evidence_ids = {
            segment.segment_id: self._derived_id(job_id, "ev", segment.segment_id)
            for segment in transcript.segments
        }
        evidence = build_evidence_set(
            ids["bundle"],
            source.source_revision_id,
            transcript_ref,
            transcript,
            evidence_ids,
        )
        if request.request_schema_version == 2:
            return self._build_v2_input(
                request,
                job_id=job_id,
                acquired=acquired,
                acquisition_checkpoint=acquisition_checkpoint,
                transcript=transcript,
                evidence=evidence,
                evidence_ids=evidence_ids,
                authority=authority,
                request_hash=request_hash,
                resumed_attempt=resumed_attempt,
                retry_of_job_id=job.retry_of_job_id,
            )
        draft = self._checkpointed(
            job_id,
            "generate_draft",
            request_hash,
            authority,
            lambda execution: self._finalize_draft(
                self._generate_draft(
                    request,
                    source,
                    transcript,
                    execution=execution,
                ),
                transcript,
                evidence_ids,
            ),
            encode=_encode_draft,
            decode=_decode_draft,
            resumed_attempt=resumed_attempt,
        )
        draft = replace(
            draft,
            markdown=rewrite_segment_citations(draft.markdown, evidence_ids),
        )
        screenshots = self._checkpointed(
            job_id,
            "optional_screenshots",
            request_hash,
            authority,
            lambda execution: self._capture_screenshots(
                job_id,
                request.screenshot_policy,
                draft,
                transcript,
                acquired,
                acquisition_checkpoint,
                execution,
            ),
            encode=_encode_screenshots,
            decode=_decode_screenshots,
            resumed_attempt=resumed_attempt,
        )
        screenshot_plan = build_screenshot_plan(
            job_id,
            request.screenshot_policy,
            draft,
            transcript,
        )
        linked_draft = bind_screenshot_assets(draft, screenshot_plan, screenshots)
        quality = evaluate_video_draft(
            linked_draft,
            evidence,
            draft_bundle_id=ids["bundle"],
            draft_artifact_id=ids["draft"],
        )
        attempt_id, started, completed, steps = self._receipt_attempts(
            job_id,
            resumed_attempt=resumed_attempt,
        )
        created = max(
            self._timestamps(job_id, source.observed_at)[2],
            completed,
        )
        return VideoBundleInput(
            bundle_id=ids["bundle"],
            created_at=created,
            location=self._portable.candidate_location(
                request.workspace_root,
                local_instance_id=self._local_instance_id,
                nonce=self._candidate_location_nonce(job_id),
            ),
            source=source,
            artifact_ids=VideoArtifactIds(
                source_metadata=ids["metadata"],
                transcript=ids["transcript"],
                evidence_set=ids["evidence"],
                primary_draft=ids["draft"],
                quality_report=ids["quality"],
            ),
            transcript=transcript,
            evidence_set=evidence,
            quality=quality,
            receipt=ReceiptProvenance(
                run_id=ids["run"],
                job_id=job_id,
                attempt_id=attempt_id,
                started_at=started,
                completed_at=completed,
                recipe_id=request.recipe_id,
                recipe_version=request.recipe_version,
                capability_id="alltonote.video-source-bundle",
                capability_version="1.0.0",
                runtime_version=RUNTIME_VERSION,
                portable_contract_id="iwiki-portable-contract-v1",
                effective_policy_hashes={
                    "preflight": self._preflight_policy_hash(request),
                    "generation": request_hash,
                    "redaction": sha256_digest("task11-redaction-v1"),
                },
                model_identity=draft.model_identity,
                transcriber_identity=self._transcriber_identity(source),
                feature_packs=self._receipt_feature_packs(),
                usage={
                    key: value
                    for key, value in draft.usage.items()
                    if key in {"input_tokens", "output_tokens"}
                    and type(value) is int
                },
                warnings=(),
                retry_of_job_id=job.retry_of_job_id,
                redactions={
                    "secrets": "omitted",
                    "prompts": "hash_only",
                    "provider_payloads": "omitted",
                },
                steps=steps,
            ),
            display_assets=screenshots,
        )

    def _build_v2_input(
        self,
        request: VideoProduceRequest,
        *,
        job_id: str,
        acquired: VideoAcquisition,
        acquisition_checkpoint: CheckpointMetadata,
        transcript: TranscriptDocument,
        evidence: EvidenceSet,
        evidence_ids: Mapping[str, str],
        authority: ExecutionAuthority,
        request_hash: str,
        resumed_attempt: Attempt | None,
        retry_of_job_id: str | None,
    ) -> VideoBundleInput:
        outputs = request.resolved_outputs or ()
        if not outputs:
            raise DomainError(
                "output_binding_unsupported",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Video output bindings are unavailable",
            )
        source = acquired.metadata
        ids = self._ids(job_id)
        portable_drafts: list[VideoDraftBundleInput] = []
        generated_drafts: list[GeneratedVideoDraft] = []
        screenshots: tuple[DisplayAssetInput, ...] = ()
        for ordinal, output in enumerate(outputs):
            step_id = (
                "generate_draft"
                if ordinal == 0
                else f"generate_{output.document_kind.value.replace('-', '_')}_draft"
            )
            compiler_identity = self._compilation_identity(output)
            freeze_step_id = (
                f"freeze_{output.document_kind.value.replace('-', '_')}_compilation"
            )
            freeze_input_hash = self._compilation_freeze_input_hash(
                request_hash,
                transcript,
                output,
            )
            frozen_identity = self._checkpointed(
                job_id,
                freeze_step_id,
                freeze_input_hash,
                authority,
                lambda _execution, identity=compiler_identity: identity,
                encode=lambda value, frozen_step=freeze_step_id: _encode_object(
                    {"identity": value, "step": frozen_step}
                ),
                decode=lambda payload, frozen_step=freeze_step_id: (
                    _decode_compilation_identity(payload, frozen_step)
                ),
                resumed_attempt=resumed_attempt,
            )
            if frozen_identity != compiler_identity:
                raise DomainError(
                    "compilation_binding_drift",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "The frozen document compiler identity changed during recovery",
                    {"document_kind": output.document_kind.value},
                )
            draft_input_hash = self._document_compilation_input_hash(
                freeze_input_hash,
                frozen_identity,
            )
            draft = self._checkpointed(
                job_id,
                step_id,
                draft_input_hash,
                authority,
                lambda execution, selected=output: self._finalize_draft(
                    self._generate_output_draft(
                        request,
                        selected,
                        source,
                        transcript,
                        execution=execution,
                    ),
                    transcript,
                    evidence_ids,
                ),
                encode=_encode_draft,
                decode=_decode_draft,
                resumed_attempt=resumed_attempt,
            )
            draft = replace(
                draft,
                markdown=rewrite_segment_citations(draft.markdown, evidence_ids),
            )
            if output.document_kind is VideoDocumentKind.KNOWLEDGE_NOTE:
                screenshots = self._checkpointed(
                    job_id,
                    "optional_screenshots",
                    request_hash,
                    authority,
                    lambda execution: self._capture_screenshots(
                        job_id,
                        request.screenshot_policy,
                        draft,
                        transcript,
                        acquired,
                        acquisition_checkpoint,
                        execution,
                    ),
                    encode=_encode_screenshots,
                    decode=_decode_screenshots,
                    resumed_attempt=resumed_attempt,
                )
                screenshot_plan = build_screenshot_plan(
                    job_id,
                    request.screenshot_policy,
                    draft,
                    transcript,
                )
                draft = bind_screenshot_assets(draft, screenshot_plan, screenshots)
            draft_artifact_id = (
                ids["draft"] if ordinal == 0 else ids["faithful_draft"]
            )
            quality_artifact_id = (
                ids["quality"] if ordinal == 0 else ids["faithful_quality"]
            )
            quality = evaluate_video_draft(
                draft,
                evidence,
                draft_bundle_id=ids["bundle"],
                draft_artifact_id=draft_artifact_id,
            )
            try:
                quality_summary = json.loads(
                    str(draft.usage["compiler_quality_summary"])
                )
                model_operation_count = int(
                    draft.usage["model_operation_count"]
                )
                sequential_model_waves = int(
                    draft.usage["sequential_model_waves"]
                )
                repair_operation_count = int(
                    draft.usage["repair_operation_count"]
                )
                model_binding_sha256 = str(
                    draft.usage["model_binding_sha256"]
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise DomainError(
                    "video_compilation_result_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "Video compiler omitted required Portable provenance",
                ) from None
            transcript_basis = {
                "provided": "human-transcript",
                "platform": "platform-caption",
                "generated": "asr-transcript",
            }.get(source.subtitle_acquisition, "unknown")
            language_policy = (
                request.faithful_language_policy.value
                if output.document_kind is VideoDocumentKind.FAITHFUL_EDITION
                else "output-language"
            )
            target_language = (
                None
                if language_policy == "preserve-source"
                else request.output_language
            )
            faithful_summary = (
                {
                    "section_count": int(draft.usage["section_count"]),
                    "uncertainty_count": int(draft.usage["uncertainty_count"]),
                    "anchor_warning_count": int(
                        draft.usage["anchor_warning_count"]
                    ),
                    "body_segment_reference_coverage_ratio": float(
                        draft.usage[
                            "body_segment_reference_coverage_ratio"
                        ]
                    ),
                }
                if output.document_kind is VideoDocumentKind.FAITHFUL_EDITION
                else None
            )
            portable_drafts.append(
                VideoDraftBundleInput(
                    document_kind=output.document_kind,
                    draft_artifact_id=draft_artifact_id,
                    quality_report_artifact_id=quality_artifact_id,
                    quality=quality,
                    recipe_id=output.recipe_id,
                    recipe_version=output.recipe_version,
                    quality_profile=output.quality_preset,
                    transcript_basis=transcript_basis,
                    source_language=source.language,
                    language_policy=language_policy,
                    target_language=target_language,
                    model_binding_sha256=model_binding_sha256,
                    model_operation_count=model_operation_count,
                    sequential_model_waves=sequential_model_waves,
                    repair_operation_count=repair_operation_count,
                    usage={
                        "input_tokens": int(draft.usage["input_tokens"]),
                        "output_tokens": int(draft.usage["output_tokens"]),
                    },
                    warnings=draft.warnings,
                    quality_summary=quality_summary,
                    faithful_summary=faithful_summary,
                )
            )
            generated_drafts.append(draft)
        if VideoDocumentKind.KNOWLEDGE_NOTE not in {
            output.document_kind for output in outputs
        }:
            screenshots = self._checkpointed(
                job_id,
                "optional_screenshots",
                request_hash,
                authority,
                lambda _execution: (),
                encode=_encode_screenshots,
                decode=_decode_screenshots,
                resumed_attempt=resumed_attempt,
            )
        model_identities = {draft.model_identity for draft in generated_drafts}
        if len(model_identities) != 1:
            raise DomainError(
                "model_execution_binding_mismatch",
                ErrorCategory.RECIPE_FAILED,
                "Video outputs used inconsistent model bindings",
            )
        primary = portable_drafts[0]
        attempt_id, started, completed, steps = self._receipt_attempts(
            job_id,
            resumed_attempt=resumed_attempt,
        )
        created = max(
            self._timestamps(job_id, source.observed_at)[2],
            completed,
        )
        usage = {
            key: sum(
                int(draft.usage.get(key, 0))
                for draft in generated_drafts
                if type(draft.usage.get(key, 0)) is int
            )
            for key in ("input_tokens", "output_tokens")
        }
        return VideoBundleInput(
            bundle_id=ids["bundle"],
            created_at=created,
            location=self._portable.candidate_location(
                request.workspace_root,
                local_instance_id=self._local_instance_id,
                nonce=self._candidate_location_nonce(job_id),
            ),
            source=source,
            artifact_ids=VideoArtifactIds(
                source_metadata=ids["metadata"],
                transcript=ids["transcript"],
                evidence_set=ids["evidence"],
                primary_draft=primary.draft_artifact_id,
                quality_report=primary.quality_report_artifact_id,
            ),
            transcript=transcript,
            evidence_set=evidence,
            quality=primary.quality,
            receipt=ReceiptProvenance(
                run_id=ids["run"],
                job_id=job_id,
                attempt_id=attempt_id,
                started_at=started,
                completed_at=completed,
                recipe_id=request.recipe_id,
                recipe_version=request.recipe_version,
                capability_id="alltonote.video-source-bundle",
                capability_version="1.0.0",
                runtime_version=RUNTIME_VERSION,
                portable_contract_id="iwiki-portable-contract-v1",
                effective_policy_hashes={
                    "preflight": self._preflight_policy_hash(request),
                    "generation": request_hash,
                    "redaction": sha256_digest("task11-redaction-v1"),
                },
                model_identity=next(iter(model_identities)),
                transcriber_identity=self._transcriber_identity(source),
                feature_packs=self._receipt_feature_packs(),
                usage=usage,
                warnings=(),
                retry_of_job_id=retry_of_job_id,
                redactions={
                    "secrets": "omitted",
                    "prompts": "hash_only",
                    "provider_payloads": "omitted",
                },
                steps=steps,
            ),
            display_assets=screenshots,
            drafts=tuple(portable_drafts),
            primary_draft_artifact_id=primary.draft_artifact_id,
        )

    def _generate_output_draft(
        self,
        request: VideoProduceRequest,
        output: ResolvedVideoOutput,
        source: VideoSourceMetadata,
        transcript: TranscriptDocument,
        *,
        execution: VideoStepExecutionContext,
    ) -> GeneratedVideoDraft:
        if output.document_kind is VideoDocumentKind.KNOWLEDGE_NOTE:
            return self._generate_draft(
                request,
                source,
                transcript,
                execution=execution,
                output=output,
            )
        compiler = self._faithful_compiler
        if compiler is None:
            raise DomainError(
                "faithful_compiler_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Faithful edition compiler is unavailable",
            )
        compiled = compiler.compile(
            VideoFaithfulCompilationInput(
                output=output,
                source_title=source.title,
                source_language=source.language,
                source_duration_ms=source.duration_ms,
                transcript_basis=source.subtitle_acquisition,
                transcript=transcript,
                language_policy=request.faithful_language_policy,
                output_language=request.output_language,
                provider_profile=request.provider_profile,
                model_override=request.model_override,
            ),
            execution=execution,
        )
        return self._project_compiled_faithful_draft(compiled, transcript=transcript)

    def _generate_draft(
        self,
        request: VideoProduceRequest,
        source: VideoSourceMetadata,
        transcript: TranscriptDocument,
        *,
        execution: VideoStepExecutionContext,
        output: ResolvedVideoOutput | None = None,
    ) -> GeneratedVideoDraft:
        if request.request_schema_version == 1:
            return self._operations.generate_draft(
                request,
                transcript,
                execution=execution,
            )
        compiler = self._knowledge_compiler
        if compiler is None:
            raise DomainError(
                "knowledge_compiler_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Knowledge note compiler is unavailable",
            )
        output = output or (
            request.resolved_outputs[0] if request.resolved_outputs else None
        )
        if output is None:
            raise DomainError(
                "output_binding_unsupported",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Knowledge note output binding is unavailable",
            )
        compiled = compiler.compile(
            VideoKnowledgeCompilationInput(
                output=output,
                source_id=source.source_id,
                source_revision_id=source.source_revision_id,
                source_title=source.title,
                source_language=source.language,
                source_duration_ms=source.duration_ms,
                transcript_basis=source.subtitle_acquisition,
                transcript=transcript,
                output_language=request.output_language,
                style=request.style,
                screenshot_policy=request.screenshot_policy,
                provider_profile=request.provider_profile,
                model_override=request.model_override,
            ),
            execution=execution,
        )
        return self._project_compiled_knowledge_draft(
            compiled,
            transcript=transcript,
        )

    @staticmethod
    def _project_compiled_knowledge_draft(
        compiled: object,
        *,
        transcript: TranscriptDocument,
    ) -> GeneratedVideoDraft:
        try:
            document_kind = compiled.document_kind  # type: ignore[attr-defined]
            model_identity = compiled.model_identity  # type: ignore[attr-defined]
            markdown = compiled.markdown  # type: ignore[attr-defined]
            cited_segment_ids = tuple(  # type: ignore[attr-defined]
                compiled.cited_segment_ids
            )
            screenshot_requests = tuple(  # type: ignore[attr-defined]
                compiled.screenshot_requests
            )
            compiled_usage = compiled.usage  # type: ignore[attr-defined]
            summary = compiled.execution_summary  # type: ignore[attr-defined]
            plan = compiled.plan  # type: ignore[attr-defined]
            coverage = compiled.coverage  # type: ignore[attr-defined]
            warnings = tuple(compiled.warnings)  # type: ignore[attr-defined]
            required_coverage_ids = tuple(
                (*coverage.covered_input_ids, *(item.input_id for item in coverage.omissions))
            )
            quality_assessment = assess_knowledge_note(
                KnowledgeNoteCandidateV1(
                    markdown=markdown,
                    allowed_segment_ids=tuple(
                        segment.segment_id for segment in transcript.segments
                    ),
                    required_coverage_input_ids=required_coverage_ids,
                    covered_coverage_input_ids=coverage.covered_input_ids,
                    omissions=tuple(
                        TextCoverageOmissionV1(item.input_id, item.reason)
                        for item in coverage.omissions
                    ),
                )
            )
            quality_summary = {
                "overall": quality_assessment.overall.value,
                "checks": [
                    {
                        **({"reason": check.reason} if check.reason is not None else {}),
                        "id": check.check_id,
                        "status": check.status,
                    }
                    for check in quality_assessment.checks
                ],
                "method_summary": {
                    "deterministic": len(quality_assessment.checks),
                    "model": 0,
                    "human": 0,
                },
                "metrics": {
                    "cited_segment_count": len(
                        quality_assessment.cited_segment_ids
                    ),
                    "coverage_input_count": len(required_coverage_ids),
                },
            }
            usage = {
                "input_tokens": compiled_usage.input_tokens,
                "output_tokens": compiled_usage.output_tokens,
                "token_counts_complete": str(
                    compiled_usage.token_counts_complete
                ).lower(),
                "compilation_topology": summary.topology.value,
                "chunk_count": summary.chunk_count,
                "knowledge_item_count": summary.knowledge_item_count,
                "model_operation_count": summary.model_operation_count,
                "sequential_model_waves": summary.sequential_model_waves,
                "repair_operation_count": int(
                    summary.sequential_model_waves
                    > plan.expected_sequential_model_waves
                ),
                "model_binding_sha256": plan.model_binding_sha256,
                "compiler_quality_summary": json.dumps(
                    quality_summary,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
            known_segment_ids = {
                segment.segment_id for segment in transcript.segments
            }
            citations_known = set(cited_segment_ids).issubset(known_segment_ids)
        except (AttributeError, TypeError, ValueError):
            raise DomainError(
                "knowledge_compilation_result_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Knowledge compiler returned an invalid document",
            ) from None
        if (
            document_kind is not VideoDocumentKind.KNOWLEDGE_NOTE
            or type(model_identity) is not str
            or not model_identity.strip()
            or not citations_known
            or type(compiled_usage.input_tokens) is not int
            or compiled_usage.input_tokens < 0
            or type(compiled_usage.output_tokens) is not int
            or compiled_usage.output_tokens < 0
            or type(compiled_usage.token_counts_complete) is not bool
            or type(summary.chunk_count) is not int
            or summary.chunk_count < 1
            or type(summary.knowledge_item_count) is not int
            or summary.knowledge_item_count < 0
            or type(summary.model_operation_count) is not int
            or summary.model_operation_count < 1
            or type(summary.sequential_model_waves) is not int
            or not 1
            <= summary.sequential_model_waves
            <= summary.model_operation_count
            or type(summary.topology.value) is not str
            or not summary.topology.value
            or quality_assessment.overall is QualityOverall.FAIL
            or type(plan.model_binding_sha256) is not str
            or not plan.model_binding_sha256.startswith("sha256:")
            or len(plan.model_binding_sha256) != 71
            or any(type(warning) is not str or not warning for warning in warnings)
            or len(warnings) != len(set(warnings))
        ):
            raise DomainError(
                "knowledge_compilation_result_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Knowledge compiler returned an invalid document",
            )
        if compiled_usage.token_counts_complete is False:
            warnings = tuple(dict.fromkeys((*warnings, "model_token_usage_incomplete")))
        try:
            return GeneratedVideoDraft(
                markdown=markdown,
                cited_segment_ids=cited_segment_ids,
                screenshot_requests=screenshot_requests,
                model_identity=model_identity,
                usage=usage,
                warnings=warnings,
            )
        except DomainError as error:
            raise DomainError(
                "knowledge_compilation_result_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Knowledge compiler returned an invalid document",
            ) from error

    def _capture_screenshots(
        self,
        job_id: str,
        policy: ScreenshotPolicy,
        draft: GeneratedVideoDraft,
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> tuple[DisplayAssetInput, ...]:
        plan = build_screenshot_plan(job_id, policy, draft, transcript)
        if not plan:
            return ()
        return self._operations.screenshots(
            plan,
            transcript,
            acquired,
            acquisition_checkpoint=acquisition_checkpoint,
            execution=execution,
        )

    @staticmethod
    def _project_compiled_faithful_draft(
        compiled: object,
        *,
        transcript: TranscriptDocument,
    ) -> GeneratedVideoDraft:
        try:
            document_kind = compiled.document_kind  # type: ignore[attr-defined]
            model_identity = compiled.model_identity  # type: ignore[attr-defined]
            markdown = compiled.markdown  # type: ignore[attr-defined]
            cited_segment_ids = tuple(  # type: ignore[attr-defined]
                compiled.cited_segment_ids
            )
            screenshot_requests = tuple(  # type: ignore[attr-defined]
                compiled.screenshot_requests
            )
            compiled_usage = compiled.usage  # type: ignore[attr-defined]
            summary = compiled.execution_summary  # type: ignore[attr-defined]
            assessment = compiled.text_assessment  # type: ignore[attr-defined]
            plan = compiled.plan  # type: ignore[attr-defined]
            warnings = tuple(compiled.warnings)  # type: ignore[attr-defined]
            status_map = {
                "pass": "pass",
                "warning": "warn",
                "fail": "fail",
                "not_applicable": "skipped",
            }
            method_summary = {"deterministic": 0, "model": 0, "human": 0}
            compiler_checks: list[dict[str, str]] = []
            for check in assessment.checks:
                method_summary[check.method.value] += 1
                check_document = {
                    "id": check.check_id,
                    "status": status_map[check.status.value],
                }
                if check.status.value != "pass":
                    check_document["reason"] = check.safe_details
                compiler_checks.append(check_document)
            metrics = assessment.metrics
            quality_summary = {
                "overall": assessment.overall.value,
                "checks": compiler_checks,
                "method_summary": method_summary,
                "metrics": {
                    "body_segment_reference_coverage_ratio": (
                        metrics.body_segment_reference_coverage_ratio
                    ),
                    "order_violation_count": metrics.order_violation_count,
                    "unknown_reference_count": metrics.unknown_reference_count,
                    "duplicate_assignment_count": metrics.duplicate_assignment_count,
                    "source_character_count": metrics.source_character_count,
                    "target_character_count": metrics.target_character_count,
                    "length_ratio": metrics.length_ratio,
                    "number_mismatch_count": metrics.number_mismatch_count,
                    "technical_token_mismatch_count": (
                        metrics.technical_token_mismatch_count
                    ),
                    "qualifier_warning_count": metrics.qualifier_warning_count,
                    "uncertainty_count": metrics.uncertainty_count,
                    "anchor_warning_count": metrics.anchor_warning_count,
                },
            }
            usage = {
                "input_tokens": compiled_usage.input_tokens,
                "output_tokens": compiled_usage.output_tokens,
                "token_counts_complete": str(
                    compiled_usage.token_counts_complete
                ).lower(),
                "section_count": summary.section_count,
                "model_operation_count": summary.model_operation_count,
                "sequential_model_waves": summary.sequential_model_waves,
                "repair_operation_count": summary.repair_operation_count,
                "uncertainty_count": summary.uncertainty_count,
                "anchor_warning_count": summary.anchor_warning_count,
                "body_segment_reference_coverage_ratio": (
                    summary.body_segment_reference_coverage_ratio
                ),
                "model_binding_sha256": plan.model_binding_sha256,
                "compiler_quality_summary": json.dumps(
                    quality_summary,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
            known_segment_ids = {
                segment.segment_id for segment in transcript.segments
            }
        except (AttributeError, TypeError, ValueError):
            raise DomainError(
                "faithful_compilation_result_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Faithful compiler returned an invalid document",
            ) from None
        if (
            document_kind is not VideoDocumentKind.FAITHFUL_EDITION
            or type(model_identity) is not str
            or not model_identity.strip()
            or not set(cited_segment_ids).issubset(known_segment_ids)
            or len(cited_segment_ids) != len(set(cited_segment_ids))
            or screenshot_requests
            or assessment.overall is QualityOverall.FAIL
            or type(compiled_usage.input_tokens) is not int
            or compiled_usage.input_tokens < 0
            or type(compiled_usage.output_tokens) is not int
            or compiled_usage.output_tokens < 0
            or type(compiled_usage.token_counts_complete) is not bool
            or type(summary.section_count) is not int
            or summary.section_count < 1
            or type(summary.model_operation_count) is not int
            or summary.model_operation_count < summary.section_count
            or type(summary.sequential_model_waves) is not int
            or not 1 <= summary.sequential_model_waves <= 2
            or type(summary.repair_operation_count) is not int
            or summary.repair_operation_count < 0
            or type(summary.body_segment_reference_coverage_ratio) is not float
            or summary.body_segment_reference_coverage_ratio != 1.0
            or type(plan.model_binding_sha256) is not str
            or not plan.model_binding_sha256.startswith("sha256:")
            or len(plan.model_binding_sha256) != 71
            or any(type(warning) is not str or not warning for warning in warnings)
            or len(warnings) != len(set(warnings))
        ):
            raise DomainError(
                "faithful_compilation_quality_failed",
                ErrorCategory.RECIPE_FAILED,
                "Faithful edition did not pass the required quality gates",
            )
        if compiled_usage.token_counts_complete is False:
            warnings = tuple(dict.fromkeys((*warnings, "model_token_usage_incomplete")))
        try:
            return GeneratedVideoDraft(
                markdown=markdown,
                cited_segment_ids=cited_segment_ids,
                screenshot_requests=(),
                model_identity=model_identity,
                usage=usage,
                warnings=warnings,
            )
        except DomainError as error:
            raise DomainError(
                "faithful_compilation_result_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Faithful compiler returned an invalid document",
            ) from error

    def _assemble(self, bundle_input: VideoBundleInput) -> _CandidateCheckpoint:
        candidate = BundleAssembler().assemble(bundle_input)
        ids = bundle_input.artifact_ids
        primary_draft = (
            next(
                draft
                for draft in bundle_input.drafts
                if draft.draft_artifact_id == ids.primary_draft
            )
            if bundle_input.drafts
            else None
        )
        documents = tuple(
            VideoProducedDocument(
                document_kind=draft.document_kind,
                draft_artifact_id=draft.draft_artifact_id,
                quality_report_artifact_id=draft.quality_report_artifact_id,
                quality_overall=draft.portable_overall,
                publish_eligible=draft.portable_publish_eligible,
            )
            for draft in bundle_input.drafts
        )
        return _CandidateCheckpoint(
            staging_relative_path=candidate.staging_relative_path,
            bundle_id=candidate.bundle_id,
            manifest_sha256=candidate.manifest_sha256,
            run_id=bundle_input.receipt.run_id,
            source_id=bundle_input.source.source_id,
            connector_id=bundle_input.source.connector_id,
            canonical_identity=self._canonical_source_identity(bundle_input.source),
            source_revision_id=bundle_input.source.source_revision_id,
            primary_draft_artifact_id=ids.primary_draft,
            transcript_artifact_id=ids.transcript,
            evidence_set_artifact_id=ids.evidence_set,
            quality_report_artifact_id=ids.quality_report,
            display_asset_ids=tuple(
                asset.artifact_id for asset in bundle_input.display_assets
            ),
            quality_overall=(
                primary_draft.portable_overall
                if primary_draft is not None
                else bundle_input.quality.overall
            ),
            publish_eligible=(
                primary_draft.portable_publish_eligible
                if primary_draft is not None
                else bundle_input.quality.publish_eligible
            ),
            usage={
                key: value
                for key, value in bundle_input.receipt.usage.items()
                if type(value) is int
            },
            warnings=bundle_input.receipt.warnings,
            documents=documents,
        )

    def _commit(
        self,
        request: VideoProduceRequest,
        checkpoint: _CandidateCheckpoint,
        attempt: Attempt,
        authority: ExecutionAuthority,
    ) -> JobSnapshot:
        self._heartbeat_resource_lease()
        prepared = self._portable.prepare_candidate(
            request.workspace_root,
            checkpoint.staging_relative_path,
            expected_bundle_id=checkpoint.bundle_id,
            expected_manifest_sha256=checkpoint.manifest_sha256,
        )
        callback_entered = False

        result_plan = VideoResultPlan(
            job_id=attempt.job_id,
            run_id=checkpoint.run_id,
            bundle_id=checkpoint.bundle_id,
            manifest_sha256=checkpoint.manifest_sha256,
            source_id=checkpoint.source_id,
            source_revision_id=checkpoint.source_revision_id,
            primary_draft_artifact_id=checkpoint.primary_draft_artifact_id,
            transcript_artifact_id=checkpoint.transcript_artifact_id,
            evidence_set_artifact_id=checkpoint.evidence_set_artifact_id,
            quality_report_artifact_id=checkpoint.quality_report_artifact_id,
            display_asset_ids=checkpoint.display_asset_ids,
            quality_overall=checkpoint.quality_overall,
            publish_eligible=checkpoint.publish_eligible,
            usage=checkpoint.usage,
            warnings=checkpoint.warnings,
            documents=checkpoint.documents,
        )
        source_identity = SourceIdentityBinding(
            connector_id=checkpoint.connector_id,
            canonical_identity=checkpoint.canonical_identity,
            source_id=checkpoint.source_id,
            owning_bundle_id=checkpoint.bundle_id,
            manifest_sha256=checkpoint.manifest_sha256,
        )

        def commit() -> PortableCommitReceipt:
            nonlocal callback_entered
            callback_entered = True
            committed = self._portable.commit_prepared(prepared)
            self._operations.after_portable_commit(committed)
            return PortableCommitReceipt(
                bundle_id=committed.bundle_id,
                manifest_sha256=committed.manifest_sha256,
                commit_sha256=committed.commit_sha256,
                workspace_relative_bundle_path=committed.relative_path,
                idempotent=committed.idempotent,
            )

        try:
            self._heartbeat_resource_lease()
            self._repository.commit_video_result_atomic(
                attempt.job_id,
                attempt.attempt_id,
                authority,
                result_plan=result_plan,
                source_identity=source_identity,
                commit=commit,
            )
        except BaseException:
            if not callback_entered:
                self._portable.discard_prepared(prepared)
            raise
        return self._snapshot(attempt.job_id)

    def _load_commit_candidate_checkpoint(
        self, job_id: str, request: VideoProduceRequest
    ) -> _CandidateCheckpoint:
        metadata = self._repository.latest_checkpoint(
            job_id, "assemble_candidate_bundle"
        )
        request_hash = self._request_hash(request)
        accepted_hashes = [self._candidate_assembly_input_hash(request_hash)]
        if request.request_schema_version == 1:
            accepted_hashes.extend(
                self._candidate_assembly_input_hash_for_behavior(
                    request_hash,
                    behavior,
                )
                for behavior in _HISTORICAL_COMMIT_CANDIDATE_BEHAVIORS_V1
            )
        if metadata is None or not any(
            self._attempt_storage.validate_checkpoint(
                metadata,
                expected_schema_id=CHECKPOINT_SCHEMA,
                expected_input_hash=input_hash,
            )
            for input_hash in accepted_hashes
        ):
            raise DomainError(
                "candidate_checkpoint_invalid",
                ErrorCategory.INTERNAL,
                "Candidate checkpoint is unavailable",
            )
        return _CandidateCheckpoint.decode(self._checkpoint_reader(metadata))

    def _load_request(self, job_id: str) -> VideoProduceRequest:
        job, _, _ = self._repository.get_job_details(job_id)
        payload = self._repository.get_job_request(job_id)
        try:
            if payload is None or sha256_digest(payload) != job.request_hash:
                raise TypeError
            value = json.loads(payload) if payload is not None else None
            if type(value) is not dict:
                raise TypeError
            schema_version = value.get("request_schema_version")
            expected_keys = {
                1: _REQUEST_KEYS_V1,
                2: _REQUEST_KEYS_V2,
            }.get(schema_version)
            if expected_keys is None or frozenset(value) != expected_keys:
                raise TypeError
            provided = value["provided_transcript"]
            transcript = (
                None
                if provided is None
                else _decode_transcript(
                    _encode_object(
                        {"step": "normalize_transcript", **provided}
                    )
                )
            )
            request = VideoProduceRequest(
                request_schema_version=value["request_schema_version"],
                workspace_root=Path(value["workspace_root"]),
                input_value=value["input_value"],
                recipe_id=value["recipe_id"],
                recipe_version=value["recipe_version"],
                provider_profile=value["provider_profile"],
                model_override=value["model_override"],
                transcriber_profile=value["transcriber_profile"],
                output_language=value["output_language"],
                quality_preset=value["quality_preset"],
                requested_outputs=(
                    (VideoDocumentKind.KNOWLEDGE_NOTE,)
                    if schema_version == 1
                    else tuple(
                        VideoDocumentKind(item) for item in value["requested_outputs"]
                    )
                ),
                resolved_outputs=(
                    None
                    if schema_version == 1
                    else tuple(
                        ResolvedVideoOutput(
                            document_kind=VideoDocumentKind(
                                binding["document_kind"]
                            ),
                            recipe_id=binding["recipe_id"],
                            recipe_version=binding["recipe_version"],
                            quality_preset=binding["quality_preset"],
                        )
                        for binding in value["output_bindings"]
                    )
                ),
                faithful_language_policy=(
                    FaithfulLanguagePolicy.PRESERVE_SOURCE
                    if schema_version == 1
                    else FaithfulLanguagePolicy(value["faithful_language_policy"])
                ),
                style=value["style"],
                screenshot_policy=ScreenshotPolicy(value["screenshot_policy"]),
                client_request_id=job.client_request_id,
                principal=job.principal,
                provided_transcript=transcript,
            )
            return request
        except (DomainError, KeyError, TypeError, ValueError, UnicodeError):
            raise DomainError(
                "job_request_invalid",
                ErrorCategory.INTERNAL,
                "Stored video request is invalid",
            ) from None

    @staticmethod
    def _canonical_source_identity(source: VideoSourceMetadata) -> str:
        return f"{source.canonical_identity_scheme}:{source.stable_video_identity}"

    def _transcriber_identity(self, source: VideoSourceMetadata) -> str:
        if source.subtitle_acquisition == "provided":
            return "core/provided-transcript-v1"
        if source.subtitle_acquisition == "platform":
            return f"{source.connector_id}/platform-subtitle-v1"
        return self._generated_transcriber_identity

    def _receipt_feature_packs(self) -> tuple[FeaturePackProvenance, ...]:
        if self._execution_pack_environment is None:
            return ()
        return tuple(
            FeaturePackProvenance(
                pack_id=pack.pack_id,
                pack_version=pack.pack_version,
                platform=pack.platform,
                manifest_sha256=pack.manifest_sha256,
            )
            for pack in self._execution_pack_environment.packs
        )

    def _receipt_attempts(
        self,
        job_id: str,
        *,
        resumed_attempt: Attempt | None,
    ) -> tuple[str, str, str, tuple[StepAttemptSummary, ...]]:
        recorded = self._repository.list_attempts(job_id)
        active = tuple(
            attempt
            for attempt in recorded
            if attempt.state in {AttemptState.PENDING, AttemptState.RUNNING}
        )
        if active and (
            resumed_attempt is None
            or len(active) != 1
            or active[0].attempt_id != resumed_attempt.attempt_id
        ):
            raise DomainError(
                "attempt_provenance_invalid",
                ErrorCategory.INTERNAL,
                "Completed step Attempt provenance is unavailable",
            )
        attempts = tuple(
            attempt
            for attempt in recorded
            if attempt.state not in {AttemptState.PENDING, AttemptState.RUNNING}
        )
        if not attempts:
            raise DomainError(
                "attempt_provenance_invalid",
                ErrorCategory.INTERNAL,
                "Completed step Attempt provenance is unavailable",
            )
        ordinals: dict[str, int] = {}
        steps: list[StepAttemptSummary] = []
        for attempt in attempts:
            ordinal = ordinals.get(attempt.step_id, 0) + 1
            ordinals[attempt.step_id] = ordinal
            steps.append(
                StepAttemptSummary(
                    step_id=attempt.step_id,
                    attempt=ordinal,
                    state=attempt.state.value,
                    started_at=attempt.created_at,
                    completed_at=attempt.updated_at,
                )
            )
        return (
            attempts[-1].attempt_id,
            min(attempt.created_at for attempt in attempts),
            max(attempt.updated_at for attempt in attempts),
            tuple(steps),
        )

    def _preflight(self, request: VideoProduceRequest) -> str:
        capabilities = self._operations.preflight_capabilities(request)
        if not isinstance(capabilities, VideoPreflightCapabilities):
            raise DomainError(
                "preflight_capabilities_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Video preflight capabilities are invalid",
            )
        if request.request_schema_version not in {1, 2}:
            raise DomainError(
                "request_schema_unsupported",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Video preflight check failed",
            )
        if request.request_schema_version == 1:
            recipe_supported = (
                request.recipe_id == "alltonote.video-course-note"
                and request.recipe_version == 1
            )
        else:
            recipe_supported = (
                request.recipe_id == "alltonote.video-producer"
                and request.recipe_version == 2
            )
        if not recipe_supported:
            raise DomainError(
                "recipe_version_unsupported",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Video preflight check failed",
            )
        if request.request_schema_version == 2:
            supported_outputs = {
                VideoDocumentKind.KNOWLEDGE_NOTE: ResolvedVideoOutput(
                    document_kind=VideoDocumentKind.KNOWLEDGE_NOTE,
                    recipe_id="alltonote.video-course-note",
                    recipe_version=2,
                    quality_preset="balanced",
                ),
                VideoDocumentKind.FAITHFUL_EDITION: ResolvedVideoOutput(
                    document_kind=VideoDocumentKind.FAITHFUL_EDITION,
                    recipe_id="alltonote.video-faithful-edition",
                    recipe_version=1,
                    quality_preset="balanced",
                ),
            }
            expected_outputs = tuple(
                supported_outputs[kind] for kind in request.requested_outputs
            )
            if request.quality_preset != "balanced":
                raise DomainError(
                    "output_quality_unsupported",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Video preflight check failed",
                )
            if request.resolved_outputs != expected_outputs:
                raise DomainError(
                    "output_binding_unsupported",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Video preflight check failed",
                )
            if (
                VideoDocumentKind.KNOWLEDGE_NOTE in request.requested_outputs
                and self._knowledge_compiler is None
            ):
                raise DomainError(
                    "knowledge_compiler_unavailable",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Video preflight check failed",
                )
            if (
                VideoDocumentKind.FAITHFUL_EDITION in request.requested_outputs
                and self._faithful_compiler is None
            ):
                raise DomainError(
                    "faithful_compiler_unavailable",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Video preflight check failed",
                )
            if (
                request.screenshot_policy is not ScreenshotPolicy.OFF
                and VideoDocumentKind.KNOWLEDGE_NOTE
                not in request.requested_outputs
            ):
                raise DomainError(
                    "screenshot_requires_knowledge_note",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Video preflight check failed",
                )
        checks = (
            (capabilities.runtime_version == RUNTIME_VERSION, "runtime_version_unsupported"),
            (capabilities.video_feature_pack, "video_feature_pack_unavailable"),
            (capabilities.source_capability, "source_capability_unavailable"),
            (capabilities.transcript_capability, "transcript_capability_unavailable"),
            (capabilities.model_capability, "model_capability_unavailable"),
            (
                request.screenshot_policy is ScreenshotPolicy.OFF
                or capabilities.screenshot_capability,
                "screenshot_capability_unavailable",
            ),
            (
                request.screenshot_policy is ScreenshotPolicy.OFF
                or capabilities.screenshot_model_compatible,
                "screenshot_model_incompatible",
            ),
            (capabilities.ffmpeg_loadable, "ffmpeg_unavailable"),
            (capabilities.model_loadable, "model_unavailable"),
            (capabilities.transcriber_loadable, "transcriber_unavailable"),
            (capabilities.effective_config_valid, "effective_config_invalid"),
            (
                capabilities.credential_references_resolvable,
                "credential_reference_unavailable",
            ),
        )
        for valid, code in checks:
            if not valid:
                raise DomainError(
                    code,
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Video preflight check failed",
                )
        self._portable.inspect(request.workspace_root)
        if not self._work_root.is_dir() or not os.access(self._work_root, os.W_OK):
            raise DomainError(
                "work_directory_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Video work directory is unavailable",
            )
        try:
            free_bytes = shutil.disk_usage(request.workspace_root).free
        except OSError:
            raise DomainError(
                "disk_space_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Workspace disk space is unavailable",
            ) from None
        if free_bytes < capabilities.required_free_bytes:
            raise DomainError(
                "disk_space_insufficient",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Workspace has insufficient free disk space",
            )
        return self._preflight_policy_hash(request)

    def _validate_candidate(
        self,
        request: VideoProduceRequest,
        checkpoint: _CandidateCheckpoint,
    ) -> object:
        report = self._portable.validate_candidate(
            request.workspace_root, checkpoint.staging_relative_path
        )
        if (
            not report.valid
            or report.bundle_id != checkpoint.bundle_id
            or report.manifest_sha256 != checkpoint.manifest_sha256
        ):
            raise DomainError(
                "portable_bundle_validation_failed",
                ErrorCategory.RECIPE_FAILED,
                "Candidate Bundle failed semantic validation",
            )
        return report

    def _checkpointed(
        self,
        job_id: str,
        step_id: str,
        input_hash: str,
        authority: ExecutionAuthority,
        action: Callable[[VideoStepExecutionContext], _T],
        *,
        encode: Callable[[_T], bytes] | None = None,
        decode: Callable[[bytes], _T] | None = None,
        reuse: bool = True,
        resumed_attempt: Attempt | None = None,
    ) -> _T:
        return self._checkpoint_runner.run(
            job_id,
            step_id,
            input_hash,
            authority,
            action,
            encode=encode,
            decode=decode,
            reuse=reuse,
            resumed_attempt=resumed_attempt,
        )

    @property
    def _heartbeat_interval_seconds(self) -> float:
        return self._checkpoint_runner.heartbeat_interval_seconds

    @_heartbeat_interval_seconds.setter
    def _heartbeat_interval_seconds(self, value: float) -> None:
        self._checkpoint_runner.heartbeat_interval_seconds = value

    @staticmethod
    def _finalize_draft(
        draft: GeneratedVideoDraft,
        transcript: TranscriptDocument,
        evidence_ids: Mapping[str, str],
    ) -> GeneratedVideoDraft:
        by_segment = {segment.segment_id: segment for segment in transcript.segments}
        markdown = rewrite_segment_citations(draft.markdown, evidence_ids)
        definitions = []
        for segment_id in draft.cited_segment_ids:
            segment = by_segment[segment_id]
            definitions.append(
                f"[^{evidence_ids[segment_id]}]: Video "
                f"{VideoService._timestamp(segment.start_ms)}-"
                f"{VideoService._timestamp(segment.end_ms)}"
            )
        finalized = markdown.rstrip() + "\n\n" + "\n".join(definitions) + "\n"
        return replace(draft, markdown=finalized)

    @staticmethod
    def _timestamp(milliseconds: int) -> str:
        minutes, remainder = divmod(milliseconds, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"{minutes:02d}:{seconds:02d}.{millis:03d}"

    def _fail_job(
        self,
        job_id: str,
        active_attempt: Attempt | None,
        authority: JobExecutionAuthority,
        error: DomainError,
    ) -> None:
        current_job, current_attempt, _ = self._repository.get_job_details(job_id)
        attempt = current_attempt or active_attempt
        if current_job.state is not JobState.RUNNING:
            return
        running_attempt = (
            attempt
            if attempt is not None
            and attempt.state is AttemptState.RUNNING
            and attempt.fencing_token == authority.fencing_token
            else None
        )
        self._repository.fail_job_atomic(
            job_id,
            ErrorDetail(error.code, error.category, error.message, error.details),
            attempt_id=(
                running_attempt.attempt_id if running_attempt is not None else None
            ),
            authority=authority,
        )

    def _snapshot(self, job_id: str) -> JobSnapshot:
        return self._job_service.get(job_id)

    def _assert_config_compatible(self, job_id: str) -> None:
        events = tuple(
            event
            for event in self._repository.list_events(job_id)
            if event.event_type == JOB_CONFIG_SNAPSHOT_EVENT
        )
        if not events:
            return
        if len(events) != 1:
            raise DomainError(
                "config_snapshot_invalid",
                ErrorCategory.INTERNAL,
                "Stored Job configuration snapshot is invalid",
            )
        current = self._submitted_config_snapshots.get(
            job_id, self._current_config_snapshot
        )
        if current is None:
            raise DomainError(
                "effective_config_unavailable",
                ErrorCategory.CONFLICT,
                "Current effective configuration is unavailable for Job recovery",
            )
        try:
            payload = json.loads(events[0].payload_json)
            stored = JobConfigSnapshot(
                snapshot_version=payload["snapshot_version"],
                values=payload["values"],
                digest=payload["digest"],
                semantic_digest=payload["semantic_digest"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DomainError(
                "config_snapshot_invalid",
                ErrorCategory.INTERNAL,
                "Stored Job configuration snapshot is invalid",
            ) from error
        if stored.semantic_digest != current.semantic_digest:
            raise DomainError(
                "effective_config_drift",
                ErrorCategory.CONFLICT,
                "Result-affecting configuration changed; create a new Job",
                {
                    "submitted_semantic_digest": stored.semantic_digest,
                    "current_semantic_digest": current.semantic_digest,
                },
            )

    def _assert_pack_environment_compatible(self, job_id: str) -> None:
        events = tuple(
            event
            for event in self._repository.list_events(job_id)
            if event.event_type == JOB_PACK_ENVIRONMENT_EVENT
        )
        if not events:
            if self._submission_pack_environment is not None:
                raise DomainError(
                    "execution_pack_snapshot_missing",
                    ErrorCategory.CONFLICT,
                    "The Job does not contain a frozen execution Pack environment",
                )
            return
        if len(events) != 1:
            raise DomainError(
                "execution_pack_snapshot_invalid",
                ErrorCategory.CONFLICT,
                "The Job execution Pack environment is unavailable or invalid",
            )
        try:
            stored = parse_job_pack_environment_payload(
                events[0].payload_json
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "execution_pack_snapshot_invalid",
                ErrorCategory.INTERNAL,
                "Stored Job execution Pack environment is invalid",
            ) from error
        if self._pack_environment_activator is None:
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The exact Video Pack generation is unavailable",
            )
        operations, transcriber_identity = self._pack_environment_activator(stored)
        if type(transcriber_identity) is not str or not transcriber_identity:
            raise DomainError(
                "execution_pack_snapshot_invalid",
                ErrorCategory.INTERNAL,
                "The resolved execution Pack environment is invalid",
            )
        self._operations = operations
        self._generated_transcriber_identity = transcriber_identity
        self._execution_pack_environment = stored

    @staticmethod
    def _resolved_output_bindings(
        request: VideoProduceRequest,
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "document_kind": output.document_kind.value,
                "recipe_id": output.recipe_id,
                "recipe_version": output.recipe_version,
                "quality_preset": output.quality_preset,
            }
            for output in request.resolved_outputs or ()
        )

    @classmethod
    def _job_request_payload(cls, request: VideoProduceRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "request_schema_version": request.request_schema_version,
            "workspace_root": request.workspace_root,
            "input_value": cls._persisted_input_value(request.input_value),
            "recipe_id": request.recipe_id,
            "recipe_version": request.recipe_version,
            "provider_profile": request.provider_profile,
            "model_override": request.model_override,
            "transcriber_profile": request.transcriber_profile,
            "output_language": request.output_language,
            "quality_preset": request.quality_preset,
            "style": request.style,
            "screenshot_policy": request.screenshot_policy,
            "client_request_id": request.client_request_id,
            "principal": request.principal,
            "provided_transcript": request.provided_transcript,
        }
        if request.request_schema_version >= 2:
            payload.update(
                {
                    "requested_outputs": request.requested_outputs,
                    "faithful_language_policy": request.faithful_language_policy,
                    "output_bindings": cls._resolved_output_bindings(request),
                }
            )
        return payload

    @classmethod
    def _request_hash(cls, request: VideoProduceRequest) -> str:
        payload: dict[str, object] = {
            "schema": request.request_schema_version,
            "workspace": str(request.workspace_root),
            "input": cls._persisted_input_value(request.input_value),
            "recipe": request.recipe_id,
            "recipe_version": request.recipe_version,
            "language": request.output_language,
            "quality": request.quality_preset,
            "style": request.style,
            "screenshots": request.screenshot_policy.value,
            "provider_profile": request.provider_profile,
            "model_override": request.model_override,
            "transcriber_profile": request.transcriber_profile,
            "provided_transcript": (
                None
                if request.provided_transcript is None
                else {
                    "language": request.provided_transcript.language,
                    "segments": [
                        {
                            "segment_id": segment.segment_id,
                            "start_ms": segment.start_ms,
                            "end_ms": segment.end_ms,
                            "text": segment.text,
                        }
                        for segment in request.provided_transcript.segments
                    ],
                }
            ),
        }
        if request.request_schema_version >= 2:
            payload.update(
                {
                    "requested_outputs": [
                        document_kind.value
                        for document_kind in request.requested_outputs
                    ],
                    "faithful_language_policy": (
                        request.faithful_language_policy.value
                    ),
                    "output_bindings": cls._resolved_output_bindings(request),
                }
            )
        return sha256_digest(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _persisted_input_value(input_value: str) -> str:
        try:
            parsed = urlsplit(input_value)
        except ValueError:
            return input_value
        host = (parsed.hostname or "").casefold()
        if host != "bilibili.com" and not host.endswith(".bilibili.com"):
            return input_value
        query = parse_qsl(parsed.query, keep_blank_values=True)
        persisted = tuple(
            (key, value)
            for key, value in query
            if key.casefold() not in {"share_source", "vd_source"}
        )
        if len(persisted) == len(query):
            return input_value
        return urlunsplit(parsed._replace(query=urlencode(persisted)))

    @classmethod
    def _preflight_policy_hash(cls, request: VideoProduceRequest) -> str:
        return sha256_digest(
            json.dumps(
                {"checks": PREFLIGHT_CHECKS, "request": cls._request_hash(request)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _compilation_identity(self, output: ResolvedVideoOutput) -> str:
        compiler: object | None = (
            self._knowledge_compiler
            if output.document_kind is VideoDocumentKind.KNOWLEDGE_NOTE
            else self._faithful_compiler
        )
        identity_provider = getattr(compiler, "compilation_identity", None)
        if not callable(identity_provider):
            raise DomainError(
                "compiler_identity_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The document compiler does not expose a frozen execution identity",
                {"document_kind": output.document_kind.value},
            )
        identity = identity_provider()
        if not _is_sha256_digest(identity):
            raise DomainError(
                "compiler_identity_invalid",
                ErrorCategory.INTERNAL,
                "The document compiler returned an invalid execution identity",
                {"document_kind": output.document_kind.value},
            )
        return sha256_digest(
            json.dumps(
                {
                    "compiler_identity": identity,
                    "document_behavior": _DOCUMENT_COMPILATION_BEHAVIOR,
                    "document_kind": output.document_kind.value,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _compilation_freeze_input_hash(
        request_hash: str,
        transcript: TranscriptDocument,
        output: ResolvedVideoOutput,
    ) -> str:
        return sha256_digest(
            json.dumps(
                {
                    "output": {
                        "document_kind": output.document_kind.value,
                        "quality_preset": output.quality_preset,
                        "recipe_id": output.recipe_id,
                        "recipe_version": output.recipe_version,
                    },
                    "request": request_hash,
                    "transcript": sha256_digest(_encode_transcript(transcript)),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _document_compilation_input_hash(
        freeze_input_hash: str,
        compiler_identity: str,
    ) -> str:
        return sha256_digest(
            json.dumps(
                {
                    "compiler_identity": compiler_identity,
                    "freeze_input": freeze_input_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _candidate_assembly_input_hash(request_hash: str) -> str:
        return VideoService._candidate_assembly_input_hash_for_behavior(
            request_hash,
            _CANDIDATE_ASSEMBLY_BEHAVIOR,
        )

    @staticmethod
    def _candidate_assembly_input_hash_for_behavior(
        request_hash: str,
        behavior: str,
    ) -> str:
        return sha256_digest(
            json.dumps(
                {
                    "behavior": behavior,
                    "request": request_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _candidate_location_nonce(job_id: str) -> str:
        return sha256_digest(
            f"{_CANDIDATE_ASSEMBLY_BEHAVIOR}:{job_id}"
        )[7:39]

    @classmethod
    def _derived_id(cls, job_id: str, prefix: str, role: str) -> str:
        uuid_value = UUID(job_id.removeprefix("job_"))
        now_ms = int(uuid_value.hex[:12], 16)
        randomness = bytes.fromhex(sha256_digest(f"{job_id}:{role}")[7:])[:10]
        return new_typed_id(prefix, now_ms=now_ms, randomness=randomness)

    @classmethod
    def _ids(cls, job_id: str) -> dict[str, str]:
        return {
            "run": cls._derived_id(job_id, "run", "run"),
            "bundle": cls._derived_id(job_id, "bnd", "bundle"),
            "source": cls._derived_id(job_id, "src", "source"),
            "revision": cls._derived_id(job_id, "rev", "revision"),
            "metadata": cls._derived_id(job_id, "art", "metadata"),
            "transcript": cls._derived_id(job_id, "art", "transcript"),
            "evidence": cls._derived_id(job_id, "art", "evidence"),
            "draft": cls._derived_id(job_id, "art", "draft"),
            "quality": cls._derived_id(job_id, "art", "quality"),
            "faithful_draft": cls._derived_id(
                job_id, "art", "faithful-edition-draft"
            ),
            "faithful_quality": cls._derived_id(
                job_id, "art", "faithful-edition-quality"
            ),
        }

    @staticmethod
    def _timestamps(
        job_id: str,
        source_observed_at: str,
    ) -> tuple[str, str, str]:
        now_ms = int(UUID(job_id.removeprefix("job_")).hex[:12], 16)
        job_created = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        try:
            observed = datetime.fromisoformat(
                source_observed_at.removesuffix("Z") + "+00:00"
            )
        except (AttributeError, TypeError, ValueError):
            raise DomainError(
                "source_metadata_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Source observation timestamp is invalid",
            ) from None
        base = max(job_created, observed + timedelta(seconds=1))

        def render(value: datetime) -> str:
            return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"

        return render(base - timedelta(seconds=2)), render(
            base - timedelta(seconds=1)
        ), render(base)


def build_screenshot_plan(
    job_id: str,
    policy: ScreenshotPolicy,
    draft: GeneratedVideoDraft,
    transcript: TranscriptDocument,
) -> tuple[ScreenshotPlanItem, ...]:
    if (
        not isinstance(policy, ScreenshotPolicy)
        or not isinstance(draft, GeneratedVideoDraft)
        or not isinstance(transcript, TranscriptDocument)
    ):
        raise DomainError(
            "screenshot_request_invalid",
            ErrorCategory.RECIPE_FAILED,
            "Screenshot requests are invalid",
        )
    requests = draft.screenshot_requests
    if policy is ScreenshotPolicy.OFF:
        if requests:
            raise DomainError(
                "screenshot_request_not_allowed",
                ErrorCategory.RECIPE_FAILED,
                "Screenshot requests are disabled for this generation",
            )
        return ()
    if not requests:
        return ()

    segments = {segment.segment_id: segment for segment in transcript.segments}
    seen: set[tuple[str, int]] = set()
    plan: list[ScreenshotPlanItem] = []
    for ordinal, request in enumerate(requests):
        if not isinstance(request, ScreenshotRequest):
            raise DomainError(
                "screenshot_request_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Screenshot request is invalid",
            )
        key = (request.segment_id, request.offset_ms)
        if key in seen:
            raise DomainError(
                "screenshot_request_duplicate",
                ErrorCategory.RECIPE_FAILED,
                "Screenshot requests must not be duplicated",
            )
        seen.add(key)
        segment = segments.get(request.segment_id)
        if (
            segment is None
            or type(request.offset_ms) is not int
            or request.offset_ms < 0
            or request.offset_ms >= segment.end_ms - segment.start_ms
        ):
            raise DomainError(
                "screenshot_request_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Screenshot request is outside its transcript segment",
            )
        timestamp_ms = segment.start_ms + request.offset_ms
        artifact_id = VideoService._derived_id(
            job_id, "art", f"screenshot:{ordinal}"
        )
        plan.append(
            ScreenshotPlanItem(
                ordinal=ordinal,
                segment_id=segment.segment_id,
                segment_start_ms=segment.start_ms,
                segment_end_ms=segment.end_ms,
                timestamp_ms=timestamp_ms,
                artifact_id=artifact_id,
                relative_path=f"assets/{artifact_id}.webp",
            )
        )
    return tuple(plan)


def bind_screenshot_assets(
    draft: GeneratedVideoDraft,
    plan: tuple[ScreenshotPlanItem, ...],
    assets: tuple[DisplayAssetInput, ...],
) -> GeneratedVideoDraft:
    def invalid() -> DomainError:
        return DomainError(
            "screenshot_asset_binding_invalid",
            ErrorCategory.RECIPE_FAILED,
            "Screenshot asset binding is invalid",
        )

    if (
        not isinstance(draft, GeneratedVideoDraft)
        or type(plan) is not tuple
        or type(assets) is not tuple
        or len(plan) != len(assets)
    ):
        raise invalid()
    for item, asset in zip(plan, assets):
        if (
            not isinstance(item, ScreenshotPlanItem)
            or not isinstance(asset, DisplayAssetInput)
            or asset.artifact_id != item.artifact_id
            or asset.relative_path != item.relative_path
        ):
            raise invalid()
    if not plan:
        return draft

    image_lines = [
        (
            f"![Video screenshot {ordinal} at "
            f"{VideoService._timestamp(item.timestamp_ms)}]"
            f"(../{item.relative_path})"
        )
        for ordinal, item in enumerate(plan, start=1)
    ]
    markdown = (
        draft.markdown.rstrip("\r\n")
        + "\n\n## Screenshots\n\n"
        + "\n\n".join(image_lines)
        + "\n"
    )
    return replace(draft, markdown=markdown, screenshot_requests=())


__all__ = [
    "CHECKPOINT_SCHEMA",
    "CHECKPOINT_STEPS",
    "PREFLIGHT_CHECKS",
    "RUNTIME_VERSION",
    "PackEnvironmentActivator",
    "SourceIdentityResolver",
    "VideoKnowledgeCompilationInput",
    "VideoKnowledgeCompilerPort",
    "VideoRecipeOperations",
    "VideoService",
    "VideoStepExecutionContext",
    "bind_screenshot_assets",
    "build_screenshot_plan",
]
