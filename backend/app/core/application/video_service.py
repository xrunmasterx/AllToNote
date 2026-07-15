from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import UUID

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
from app.core.domain.ids import new_typed_id, sha256_digest
from app.core.domain.video import (
    GeneratedVideoDraft,
    JobSnapshot,
    JobState,
    ScreenshotPlanItem,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.jobs.model import Attempt, AttemptState, CheckpointMetadata, CheckpointRecord
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.portable.artifacts import PortableArtifactRef, build_transcript
from app.core.portable.bundle_assembler import (
    BundleAssembler,
    DisplayAssetInput,
    ReceiptProvenance,
    StepAttemptSummary,
    VideoArtifactIds,
    VideoBundleInput,
    VideoSourceMetadata,
)
from app.core.portable.evidence import build_evidence_set, rewrite_segment_citations
from app.core.portable.quality import evaluate_video_draft
from app.core.ports.jobs import (
    AttemptStoragePort,
    PortableCommitReceipt,
    SourceIdentityBinding,
    VideoResultPlan,
    VideoExecutionRepositoryPort,
)
from app.core.ports.portable import PortableCommitResultPort, PortableWorkspacePort
from app.core.ports.source import ResolvedVideoSource


RUNTIME_VERSION = "0.1.0"
CHECKPOINT_SCHEMA = "video-step.v1"
_CANDIDATE_ASSEMBLY_BEHAVIOR = "linked-screenshot-draft-v1"
_SCHEDULER_LEASE_TTL_SECONDS = 300
_SCHEDULER_HEARTBEAT_INTERVAL_SECONDS = 30.0
_AUTHORITY_LOSS_CODES = frozenset({"attempt_fenced", "scheduler_lease_lost"})
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
_REQUEST_KEYS = frozenset(
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


@dataclass(frozen=True)
class VideoStepExecutionContext:
    job_id: str
    step_id: str
    attempt_id: str
    authority: ExecutionAuthority
    heartbeat: Callable[[], None]


def _checkpoint_error() -> DomainError:
    return DomainError(
        "checkpoint_content_invalid",
        ErrorCategory.INTERNAL,
        "Checkpoint content is invalid",
    )


def _decode_object(payload: bytes, *, keys: frozenset[str]) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError):
        raise _checkpoint_error() from None
    if type(value) is not dict or frozenset(value) != keys:
        raise _checkpoint_error()
    return value


def _encode_object(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


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


class VideoService:
    def __init__(
        self,
        repository: VideoExecutionRepositoryPort,
        attempt_storage: AttemptStoragePort,
        portable: PortableWorkspacePort,
        operations: VideoRecipeOperations,
        *,
        checkpoint_reader: Callable[[CheckpointMetadata], bytes],
        owner_id: str,
        work_root: Path,
        local_instance_id: str,
    ) -> None:
        self._repository = repository
        self._job_service = JobService(repository)
        self._attempt_storage = attempt_storage
        self._portable = portable
        self._operations = operations
        self._checkpoint_reader = checkpoint_reader
        self._owner_id = owner_id
        self._work_root = work_root
        self._local_instance_id = local_instance_id
        self._execution_lock = threading.Lock()
        self._heartbeat_interval_seconds = _SCHEDULER_HEARTBEAT_INTERVAL_SECONDS

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        if not isinstance(request, VideoProduceRequest):
            raise DomainError(
                "video_produce_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Video production requires a versioned request",
            )
        snapshot = self._job_service.submit(request)
        return snapshot

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._snapshot(job_id)

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
        request = self._load_request(job_id)
        if snapshot.state is JobState.QUEUED:
            self._repository.transition_job(job_id, JobState.RUNNING)
        authority = self._repository.acquire_scheduler_lease(
            self._owner_id, ttl_seconds=_SCHEDULER_LEASE_TTL_SECONDS
        )
        _, active_attempt, _ = self._repository.get_job_details(job_id)
        try:
            if (
                active_attempt is not None
                and active_attempt.state is AttemptState.RUNNING
                and active_attempt.fencing_token != authority.fencing_token
            ):
                active_attempt = self._repository.take_over_running_attempt(
                    job_id, active_attempt.attempt_id, authority
                )
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
            try:
                self._fail_job(job_id, active_attempt, authority, error)
            except DomainError as convergence_error:
                if convergence_error.code in _AUTHORITY_LOSS_CODES:
                    raise error from convergence_error
                raise
            return self._snapshot(job_id)
        finally:
            try:
                self._repository.release_scheduler_lease(authority)
            except Exception:
                # The lease has a bounded TTL; cleanup must not replace the Job result
                # or the primary execution error.
                pass

    def _execute(
        self,
        job_id: str,
        request: VideoProduceRequest,
        authority: ExecutionAuthority,
        *,
        resumed_attempt: Attempt | None,
    ) -> JobSnapshot:
        request_hash = self._request_hash(request)
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
                request,
                source_id=ids["source"],
                source_revision_id=ids["revision"],
            ),
            encode=_encode_source,
            decode=_decode_source,
            resumed_attempt=resumed_attempt,
        )
        acquired = self._checkpointed(
            job_id,
            "acquire",
            request_hash,
            authority,
            lambda execution: self._operations.acquire(
                request,
                source,
                source_id=ids["source"],
                source_revision_id=ids["revision"],
                execution=execution,
            ),
            encode=_encode_acquired,
            decode=_decode_acquired,
            resumed_attempt=resumed_attempt,
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
        commit_attempt = self._repository.create_attempt(job_id, "commit")
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
        checkpoint = self._load_candidate_checkpoint(
            attempt.job_id, self._request_hash(request)
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
        draft = self._checkpointed(
            job_id,
            "generate_draft",
            request_hash,
            authority,
            lambda execution: self._finalize_draft(
                self._operations.generate_draft(
                    request,
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
        started, completed, created = self._timestamps(job_id)
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
                attempt_id=self._derived_id(job_id, "att", "receipt"),
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
                usage={
                    key: value
                    for key, value in draft.usage.items()
                    if key in {"input_tokens", "output_tokens"}
                    and type(value) is int
                },
                warnings=(),
                redactions={
                    "secrets": "omitted",
                    "prompts": "hash_only",
                    "provider_payloads": "omitted",
                },
                steps=tuple(
                    StepAttemptSummary(
                        step_id=step,
                        attempt=1,
                        state="succeeded",
                        started_at=started,
                        completed_at=completed,
                    )
                    for step in CHECKPOINT_STEPS
                ),
            ),
            display_assets=screenshots,
        )

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

    def _assemble(self, bundle_input: VideoBundleInput) -> _CandidateCheckpoint:
        candidate = BundleAssembler().assemble(bundle_input)
        ids = bundle_input.artifact_ids
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
            quality_overall=bundle_input.quality.overall,
            publish_eligible=bundle_input.quality.publish_eligible,
            usage={
                key: value
                for key, value in bundle_input.receipt.usage.items()
                if type(value) is int
            },
            warnings=bundle_input.receipt.warnings,
        )

    def _commit(
        self,
        request: VideoProduceRequest,
        checkpoint: _CandidateCheckpoint,
        attempt: Attempt,
        authority: ExecutionAuthority,
    ) -> JobSnapshot:
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

    def _load_candidate_checkpoint(
        self, job_id: str, request_hash: str
    ) -> _CandidateCheckpoint:
        metadata = self._repository.latest_checkpoint(
            job_id, "assemble_candidate_bundle"
        )
        if metadata is None or not self._attempt_storage.validate_checkpoint(
            metadata,
            expected_schema_id=CHECKPOINT_SCHEMA,
            expected_input_hash=self._candidate_assembly_input_hash(request_hash),
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
            value = json.loads(payload) if payload is not None else None
            if type(value) is not dict or frozenset(value) != _REQUEST_KEYS:
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
            return VideoProduceRequest(
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
                style=value["style"],
                screenshot_policy=ScreenshotPolicy(value["screenshot_policy"]),
                client_request_id=job.client_request_id,
                principal=job.principal,
                provided_transcript=transcript,
            )
        except (DomainError, KeyError, TypeError, ValueError, UnicodeError):
            raise DomainError(
                "job_request_invalid",
                ErrorCategory.INTERNAL,
                "Stored video request is invalid",
            ) from None

    @staticmethod
    def _canonical_source_identity(source: VideoSourceMetadata) -> str:
        return f"{source.canonical_identity_scheme}:{source.stable_video_identity}"

    @staticmethod
    def _transcriber_identity(source: VideoSourceMetadata) -> str:
        if source.subtitle_acquisition == "provided":
            return "core/provided-transcript-v1"
        if source.subtitle_acquisition == "platform":
            return f"{source.connector_id}/platform-subtitle-v1"
        return "fake/transcriber-v1"

    def _preflight(self, request: VideoProduceRequest) -> str:
        capabilities = self._operations.preflight_capabilities(request)
        if not isinstance(capabilities, VideoPreflightCapabilities):
            raise DomainError(
                "preflight_capabilities_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Video preflight capabilities are invalid",
            )
        checks = (
            (request.request_schema_version == 1, "request_schema_unsupported"),
            (
                request.recipe_id == "alltonote.video-course-note"
                and request.recipe_version == 1,
                "recipe_version_unsupported",
            ),
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
        self._heartbeat(authority)
        existing = self._repository.latest_checkpoint(job_id, step_id)
        if reuse and existing is not None and self._attempt_storage.validate_checkpoint(
            existing,
            expected_schema_id=CHECKPOINT_SCHEMA,
            expected_input_hash=input_hash,
        ):
            try:
                payload = self._checkpoint_reader(existing)
                if decode is not None:
                    value = decode(payload)
                else:
                    marker = _decode_object(
                        payload, keys=frozenset({"state", "step"})
                    )
                    if marker != {"state": "succeeded", "step": step_id}:
                        raise _checkpoint_error()
                    value = None  # type: ignore[assignment]
            except (DomainError, OSError, RuntimeError, TypeError, ValueError):
                pass
            else:
                if self._is_resumed_step(resumed_attempt, job_id, step_id):
                    self._repository.transition_attempt(
                        resumed_attempt.attempt_id,
                        AttemptState.SUCCEEDED,
                        authority=authority,
                    )
                self._heartbeat(authority)
                return value
        if self._is_resumed_step(resumed_attempt, job_id, step_id):
            attempt = resumed_attempt
        else:
            attempt = self._repository.create_attempt(job_id, step_id)
            attempt = self._repository.start_attempt(attempt.attempt_id, authority)
        try:
            self._heartbeat(authority)
            execution = VideoStepExecutionContext(
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                authority=authority,
                heartbeat=lambda: self._heartbeat(authority),
            )
            value = self._run_checkpoint_action(action, execution)
            self._heartbeat(authority)
            payload = (
                encode(value)
                if encode is not None
                else json.dumps(
                    {"step": step_id, "state": "succeeded"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            self._attempt_storage.save_checkpoint(
                CheckpointRecord(
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt.attempt_id,
                    schema_id=CHECKPOINT_SCHEMA,
                    input_hash=input_hash,
                    payload=payload,
                    metadata_json="{}",
                ),
                authority,
            )
        except DomainError as error:
            try:
                if error.code == "external_outcome_unknown":
                    self._repository.pause_for_external_outcome_atomic(
                        job_id,
                        attempt.attempt_id,
                        authority,
                    )
                else:
                    self._repository.transition_attempt(
                        attempt.attempt_id, AttemptState.FAILED, authority=authority
                    )
            except DomainError as convergence_error:
                if convergence_error.code not in _AUTHORITY_LOSS_CODES:
                    raise
            raise
        except BaseException:
            try:
                self._repository.transition_attempt(
                    attempt.attempt_id, AttemptState.FAILED, authority=authority
                )
            except DomainError as convergence_error:
                if convergence_error.code not in _AUTHORITY_LOSS_CODES:
                    raise
            raise
        self._repository.transition_attempt(
            attempt.attempt_id, AttemptState.SUCCEEDED, authority=authority
        )
        return value

    def _run_checkpoint_action(
        self,
        action: Callable[[VideoStepExecutionContext], _T],
        execution: VideoStepExecutionContext,
    ) -> _T:
        stop = threading.Event()
        heartbeat_failures: list[BaseException] = []

        def heartbeat_until_stopped() -> None:
            try:
                while not stop.wait(self._heartbeat_interval_seconds):
                    execution.heartbeat()
            except BaseException as error:
                heartbeat_failures.append(error)
                stop.set()

        worker = threading.Thread(
            target=heartbeat_until_stopped,
            name=f"alltonote-scheduler-heartbeat-{execution.attempt_id}",
        )
        worker.start()
        try:
            value = action(execution)
        except BaseException:
            stop.set()
            worker.join()
            raise
        stop.set()
        worker.join()
        if heartbeat_failures:
            raise heartbeat_failures[0]
        return value

    @staticmethod
    def _is_resumed_step(
        attempt: Attempt | None,
        job_id: str,
        step_id: str,
    ) -> bool:
        return (
            attempt is not None
            and attempt.job_id == job_id
            and attempt.step_id == step_id
            and attempt.state is AttemptState.RUNNING
        )

    def _heartbeat(self, authority: ExecutionAuthority) -> None:
        self._repository.heartbeat_scheduler_lease(
            authority,
            ttl_seconds=_SCHEDULER_LEASE_TTL_SECONDS,
        )

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
        authority: ExecutionAuthority,
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
            authority=authority if running_attempt is not None else None,
        )

    def _snapshot(self, job_id: str) -> JobSnapshot:
        return self._job_service.get(job_id)

    @staticmethod
    def _request_hash(request: VideoProduceRequest) -> str:
        return sha256_digest(
            json.dumps(
                {
                    "schema": request.request_schema_version,
                    "workspace": str(request.workspace_root),
                    "input": request.input_value,
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
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @classmethod
    def _preflight_policy_hash(cls, request: VideoProduceRequest) -> str:
        return sha256_digest(
            json.dumps(
                {"checks": PREFLIGHT_CHECKS, "request": cls._request_hash(request)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _candidate_assembly_input_hash(request_hash: str) -> str:
        return sha256_digest(
            json.dumps(
                {
                    "behavior": _CANDIDATE_ASSEMBLY_BEHAVIOR,
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
        }

    @staticmethod
    def _timestamps(job_id: str) -> tuple[str, str, str]:
        now_ms = int(UUID(job_id.removeprefix("job_")).hex[:12], 16)
        base = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

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
    "VideoRecipeOperations",
    "VideoService",
    "VideoStepExecutionContext",
    "bind_screenshot_assets",
    "build_screenshot_plan",
]
