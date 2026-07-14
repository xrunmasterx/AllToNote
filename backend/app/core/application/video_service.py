from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar
from uuid import UUID

from app.core.application.job_service import JobService
from app.core.domain.ids import new_typed_id, sha256_digest
from app.core.domain.video import (
    GeneratedVideoDraft,
    JobSnapshot,
    JobState,
    QualityOverall,
    TranscriptDocument,
    VideoProduceRequest,
    VideoProduceResult,
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
from app.core.portable.evidence import build_evidence_set
from app.core.portable.quality import evaluate_video_draft
from app.core.ports.jobs import (
    AttemptStoragePort,
    JobCompletion,
    SourceIdentityBinding,
    VideoExecutionRepositoryPort,
)
from app.core.ports.portable import PortableCommitResultPort, PortableWorkspacePort


RUNTIME_VERSION = "0.1.0"
CHECKPOINT_SCHEMA = "video-step.v1"
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


class VideoRecipeOperations(Protocol):
    def preflight(self, request: VideoProduceRequest) -> None: ...

    def resolve_source(
        self,
        request: VideoProduceRequest,
        *,
        source_id: str,
        source_revision_id: str,
    ) -> VideoSourceMetadata: ...

    def acquire(self, source: VideoSourceMetadata, *, external: bool) -> object: ...

    def transcribe(
        self,
        request: VideoProduceRequest,
        acquired: object,
        *,
        external: bool,
    ) -> TranscriptDocument: ...

    def generate_draft(
        self,
        request: VideoProduceRequest,
        transcript: TranscriptDocument,
        evidence_ids: dict[str, str],
        *,
        external: bool,
    ) -> GeneratedVideoDraft: ...

    def screenshots(
        self,
        request: VideoProduceRequest,
        draft: GeneratedVideoDraft,
        *,
        external: bool,
    ) -> tuple[DisplayAssetInput, ...]: ...

    def after_portable_commit(self, result: PortableCommitResultPort) -> None: ...


@dataclass(frozen=True)
class _CandidateCheckpoint:
    staging_relative_path: str
    bundle_id: str
    manifest_sha256: str
    run_id: str
    source_id: str
    source_revision_id: str
    primary_draft_artifact_id: str
    transcript_artifact_id: str
    evidence_set_artifact_id: str
    quality_report_artifact_id: str
    display_asset_ids: tuple[str, ...]
    quality_overall: QualityOverall
    publish_eligible: bool
    usage: Mapping[str, int]
    warnings: tuple[str, ...]

    def encode(self) -> bytes:
        return json.dumps(
            {
                "step": "assemble_candidate_bundle",
                "staging_relative_path": self.staging_relative_path,
                "bundle_id": self.bundle_id,
                "manifest_sha256": self.manifest_sha256,
                "run_id": self.run_id,
                "source_id": self.source_id,
                "source_revision_id": self.source_revision_id,
                "primary_draft_artifact_id": self.primary_draft_artifact_id,
                "transcript_artifact_id": self.transcript_artifact_id,
                "evidence_set_artifact_id": self.evidence_set_artifact_id,
                "quality_report_artifact_id": self.quality_report_artifact_id,
                "display_asset_ids": list(self.display_asset_ids),
                "quality_overall": self.quality_overall.value,
                "publish_eligible": self.publish_eligible,
                "usage": dict(self.usage),
                "warnings": list(self.warnings),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    @classmethod
    def decode(cls, payload: bytes) -> _CandidateCheckpoint:
        try:
            value = json.loads(payload)
            if type(value) is not dict or value.get("step") != "assemble_candidate_bundle":
                raise TypeError
            usage = value["usage"]
            if type(usage) is not dict or any(
                type(key) is not str or type(item) is not int or item < 0
                for key, item in usage.items()
            ):
                raise TypeError
            return cls(
                staging_relative_path=value["staging_relative_path"],
                bundle_id=value["bundle_id"],
                manifest_sha256=value["manifest_sha256"],
                run_id=value["run_id"],
                source_id=value["source_id"],
                source_revision_id=value["source_revision_id"],
                primary_draft_artifact_id=value["primary_draft_artifact_id"],
                transcript_artifact_id=value["transcript_artifact_id"],
                evidence_set_artifact_id=value["evidence_set_artifact_id"],
                quality_report_artifact_id=value["quality_report_artifact_id"],
                display_asset_ids=tuple(value["display_asset_ids"]),
                quality_overall=QualityOverall(value["quality_overall"]),
                publish_eligible=value["publish_eligible"],
                usage=usage,
                warnings=tuple(value["warnings"]),
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise DomainError(
                "candidate_checkpoint_invalid",
                ErrorCategory.INTERNAL,
                "Candidate checkpoint is invalid",
            ) from None


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
    ) -> None:
        self._repository = repository
        self._job_service = JobService(repository)
        self._attempt_storage = attempt_storage
        self._portable = portable
        self._operations = operations
        self._checkpoint_reader = checkpoint_reader
        self._owner_id = owner_id
        self._requests: dict[str, VideoProduceRequest] = {}
        self._errors: dict[str, ErrorDetail] = {}

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        if not isinstance(request, VideoProduceRequest):
            raise DomainError(
                "video_produce_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Video production requires a versioned request",
            )
        snapshot = self._job_service.submit(request)
        self._requests[snapshot.job_id] = request
        return snapshot

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._snapshot(job_id)

    def wait_job(self, job_id: str) -> JobSnapshot:
        snapshot = self._snapshot(job_id)
        if snapshot.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            return snapshot
        request = self._requests.get(job_id)
        if request is None:
            raise DomainError(
                "job_request_unavailable",
                ErrorCategory.INTERNAL,
                "Submitted request is unavailable for execution",
            )
        if snapshot.state is JobState.QUEUED:
            self._repository.transition_job(job_id, JobState.RUNNING)
        authority = self._repository.acquire_scheduler_lease(
            self._owner_id, ttl_seconds=300
        )
        _, active_attempt, _ = self._repository.get_job_details(job_id)
        try:
            if active_attempt is not None and active_attempt.step_id == "commit":
                return self._reconcile_commit(request, active_attempt, authority)
            return self._execute(job_id, request, authority)
        except DomainError as error:
            self._fail_job(job_id, active_attempt, authority, error)
            return self._snapshot(job_id)

    def _execute(
        self,
        job_id: str,
        request: VideoProduceRequest,
        authority: ExecutionAuthority,
    ) -> JobSnapshot:
        request_hash = self._request_hash(request)
        self._checkpointed(
            job_id,
            "preflight",
            request_hash,
            authority,
            lambda: self._preflight(request),
        )
        ids = self._ids(job_id)
        source = self._checkpointed(
            job_id,
            "resolve_source",
            request_hash,
            authority,
            lambda: self._operations.resolve_source(
                request,
                source_id=ids["source"],
                source_revision_id=ids["revision"],
            ),
        )
        acquired = self._checkpointed(
            job_id,
            "acquire",
            request_hash,
            authority,
            lambda: self._operations.acquire(source, external=True),
        )
        transcript = self._checkpointed(
            job_id,
            "normalize_transcript",
            request_hash,
            authority,
            lambda: self._operations.transcribe(request, acquired, external=True),
        )
        self._checkpointed(
            job_id,
            "create_source_revision",
            request_hash,
            authority,
            lambda: source.source_revision_id,
        )
        bundle_input = self._build_input(
            request,
            job_id=job_id,
            source=source,
            transcript=transcript,
            authority=authority,
            request_hash=request_hash,
        )
        checkpoint = self._checkpointed(
            job_id,
            "assemble_candidate_bundle",
            request_hash,
            authority,
            lambda: self._assemble(bundle_input),
            encode=lambda value: value.encode(),
        )
        self._checkpointed(
            job_id,
            "quality_and_portable_validation",
            request_hash,
            authority,
            lambda: self._validate_candidate(request, checkpoint),
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
        source: VideoSourceMetadata,
        transcript: TranscriptDocument,
        authority: ExecutionAuthority,
        request_hash: str,
    ) -> VideoBundleInput:
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
            lambda: self._operations.generate_draft(
                request, transcript, evidence_ids, external=True
            ),
        )
        screenshots = self._checkpointed(
            job_id,
            "optional_screenshots",
            request_hash,
            authority,
            lambda: self._operations.screenshots(request, draft, external=True),
        )
        quality = evaluate_video_draft(
            draft,
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
                local_instance_id="task11",
                nonce=job_id.removeprefix("job_").replace("-", ""),
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
                transcriber_identity="fake/transcriber-v1",
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

    def _assemble(self, bundle_input: VideoBundleInput) -> _CandidateCheckpoint:
        candidate = BundleAssembler().assemble(bundle_input)
        ids = bundle_input.artifact_ids
        return _CandidateCheckpoint(
            staging_relative_path=candidate.staging_relative_path,
            bundle_id=candidate.bundle_id,
            manifest_sha256=candidate.manifest_sha256,
            run_id=bundle_input.receipt.run_id,
            source_id=bundle_input.source.source_id,
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

        def commit() -> JobCompletion:
            nonlocal callback_entered
            callback_entered = True
            committed = self._portable.commit_prepared(prepared)
            self._operations.after_portable_commit(committed)
            result = VideoProduceResult(
                job_id=attempt.job_id,
                run_id=checkpoint.run_id,
                bundle_id=committed.bundle_id,
                manifest_sha256=committed.manifest_sha256,
                commit_sha256=committed.commit_sha256,
                workspace_relative_bundle_path=committed.relative_path,
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
                idempotent=committed.idempotent,
            )
            return JobCompletion(
                result=result,
                source_identity=SourceIdentityBinding(
                    connector_id="fixture",
                    canonical_identity=request.input_value,
                    source_id=result.source_id,
                    owning_bundle_id=result.bundle_id,
                    manifest_sha256=result.manifest_sha256,
                ),
            )

        try:
            self._repository.commit_video_result_atomic(
                attempt.job_id, attempt.attempt_id, authority, commit
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
            expected_input_hash=request_hash,
        ):
            raise DomainError(
                "candidate_checkpoint_invalid",
                ErrorCategory.INTERNAL,
                "Candidate checkpoint is unavailable",
            )
        return _CandidateCheckpoint.decode(self._checkpoint_reader(metadata))

    def _preflight(self, request: VideoProduceRequest) -> None:
        self._operations.preflight(request)
        self._portable.inspect(request.workspace_root)

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
        action: Callable[[], _T],
        *,
        encode: Callable[[_T], bytes] | None = None,
    ) -> _T:
        attempt = self._repository.create_attempt(job_id, step_id)
        attempt = self._repository.start_attempt(attempt.attempt_id, authority)
        try:
            value = action()
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
        except BaseException:
            self._repository.transition_attempt(
                attempt.attempt_id, AttemptState.FAILED, authority=authority
            )
            raise
        self._repository.transition_attempt(
            attempt.attempt_id, AttemptState.SUCCEEDED, authority=authority
        )
        return value

    def _fail_job(
        self,
        job_id: str,
        active_attempt: Attempt | None,
        authority: ExecutionAuthority,
        error: DomainError,
    ) -> None:
        current_job, current_attempt, _ = self._repository.get_job_details(job_id)
        attempt = current_attempt or active_attempt
        if attempt is not None and attempt.state is AttemptState.RUNNING:
            self._repository.transition_attempt(
                attempt.attempt_id, AttemptState.FAILED, authority=authority
            )
        if current_job.state is JobState.RUNNING:
            self._repository.transition_job(job_id, JobState.FAILED)
        self._errors[job_id] = ErrorDetail(
            error.code, error.category, error.message, error.details
        )

    def _snapshot(self, job_id: str) -> JobSnapshot:
        snapshot = self._job_service.get(job_id)
        error = self._errors.get(job_id)
        if error is None:
            return snapshot
        return JobSnapshot(
            job_id=snapshot.job_id,
            state=snapshot.state,
            cancellation_requested=snapshot.cancellation_requested,
            active_attempt_id=snapshot.active_attempt_id,
            challenge_id=snapshot.challenge_id,
            retry_of_job_id=snapshot.retry_of_job_id,
            result=snapshot.result,
            error=error,
        )

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


__all__ = [
    "CHECKPOINT_SCHEMA",
    "CHECKPOINT_STEPS",
    "PREFLIGHT_CHECKS",
    "RUNTIME_VERSION",
    "VideoRecipeOperations",
    "VideoService",
]
