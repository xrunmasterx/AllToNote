from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from app.core.application.checkpoint_runner import CheckpointedStepRunner
from app.core.application.document_checkpoints import (
    DocumentCandidateCheckpoint,
    decode_parsed_document,
    encode_parsed_document,
)
from app.core.application.job_service import JobService
from app.core.domain.document import DocumentProduceRequest, ParsedDocument
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.jobs.model import (
    Attempt,
    AttemptState,
    CheckpointMetadata,
    JobExecutionBinding,
    JobSnapshot,
    JobState,
)
from app.core.jobs.resource_lease import (
    HEAVY_PRODUCTION_RESOURCE_NAME,
    ExecutionAuthority,
    ResourceLease,
    ResourceLeaseStorePort,
    ResourceOwner,
)
from app.core.portable.document_bundle_assembler import DocumentBundleAssembler
from app.core.ports.document import DocumentParserPort
from app.core.ports.jobs import (
    AttemptStoragePort,
    PortableCommitReceipt,
    RecipeResultPlan,
    SourceIdentityBinding,
    JobExecutionRepositoryPort,
)
from app.core.ports.portable import PortableWorkspacePort


CHECKPOINT_SCHEMA = "document-step.v1"
_LEASE_TTL_SECONDS = 300
_HEARTBEAT_SECONDS = 30.0
_BINDING = JobExecutionBinding(
    recipe_id="alltonote.document-note",
    recipe_version=1,
    executor_id="alltonote.document",
    executor_version=1,
    pack_id="document-basic",
    pack_version="docling-2.117.0-tableformer-v2.3.0",
)
_REQUEST_FIELDS = frozenset(
    {
        "expected_source_mtime_ns",
        "expected_source_sha256",
        "expected_source_size",
        "input_path",
        "recipe_id",
        "recipe_version",
        "request_schema_version",
        "workspace_root",
    }
)


class DocumentService:
    def __init__(
        self,
        repository: JobExecutionRepositoryPort,
        attempt_storage: AttemptStoragePort,
        parser: DocumentParserPort,
        portable: PortableWorkspacePort,
        *,
        work_root: Path,
        checkpoint_reader: Callable[[CheckpointMetadata], bytes],
        owner_id: str,
        local_instance_id: str,
        resource_lease_store: ResourceLeaseStorePort | None = None,
        resource_owner: ResourceOwner | None = None,
    ) -> None:
        if (resource_lease_store is None) != (resource_owner is None):
            raise ValueError("resource_admission_pair_required")
        if (
            resource_owner is not None
            and owner_id != resource_owner.process_instance_id
        ):
            raise ValueError("resource_admission_owner_mismatch")
        self._repository = repository
        self._attempt_storage = attempt_storage
        self._parser = parser
        self._portable = portable
        self._work_root = Path(work_root)
        self._checkpoint_reader = checkpoint_reader
        self._owner_id = owner_id
        self._local_instance_id = local_instance_id
        self._resource_lease_store = resource_lease_store
        self._resource_owner = resource_owner
        self._active_resource_lease: ResourceLease | None = None
        self._job_service = JobService(repository)
        self._execution_lock = threading.Lock()
        self._checkpoint_runner = CheckpointedStepRunner(
            repository,
            attempt_storage,
            checkpoint_reader=checkpoint_reader,
            checkpoint_schema=CHECKPOINT_SCHEMA,
            scheduler_lease_ttl_seconds=_LEASE_TTL_SECONDS,
            heartbeat_interval_seconds=_HEARTBEAT_SECONDS,
            additional_heartbeat=self._heartbeat_resource_lease,
        )

    @property
    def execution_binding(self) -> JobExecutionBinding:
        return _BINDING

    def submit_document(self, request: DocumentProduceRequest) -> JobSnapshot:
        if not isinstance(request, DocumentProduceRequest):
            raise DomainError(
                "document_produce_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Document request must use the versioned contract",
            )
        return self._job_service.submit(request, execution_binding=_BINDING)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._job_service.get(job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._job_service.cancel(job_id)

    def wait_job(self, job_id: str) -> JobSnapshot:
        with self._execution_lock:
            return self._wait_job_locked(job_id)

    def _wait_job_locked(self, job_id: str) -> JobSnapshot:
        snapshot = self.get_job(job_id)
        if snapshot.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.WAITING_FOR_INPUT,
        }:
            return snapshot
        self._acquire_resource_lease()
        try:
            authority = self._repository.acquire_scheduler_lease(
                self._owner_id,
                ttl_seconds=_LEASE_TTL_SECONDS,
            )
            try:
                snapshot = self.get_job(job_id)
                if snapshot.state in {
                    JobState.SUCCEEDED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.WAITING_FOR_INPUT,
                }:
                    return snapshot
                if snapshot.state is JobState.QUEUED:
                    self._repository.transition_job(job_id, JobState.RUNNING)
                _, active_attempt, _ = self._repository.get_job_details(job_id)
                try:
                    request = self._load_request(job_id)
                    if (
                        active_attempt is not None
                        and active_attempt.state is AttemptState.RUNNING
                        and active_attempt.fencing_token != authority.fencing_token
                    ):
                        active_attempt = self._repository.take_over_running_attempt(
                            job_id,
                            active_attempt.attempt_id,
                            authority,
                        )
                    if active_attempt is not None and active_attempt.step_id == "commit":
                        return self._reconcile_commit(request, active_attempt, authority)
                    return self._execute(
                        job_id,
                        request,
                        authority,
                        resumed_attempt=active_attempt,
                    )
                except DomainError as error:
                    self._fail_job(job_id, active_attempt, authority, error)
                    return self.get_job(job_id)
            finally:
                try:
                    self._repository.release_scheduler_lease(authority)
                except Exception:
                    pass
        finally:
            self._release_resource_lease()

    def _acquire_resource_lease(self) -> None:
        if self._resource_lease_store is None or self._resource_owner is None:
            return
        self._active_resource_lease = self._resource_lease_store.acquire(
            HEAVY_PRODUCTION_RESOURCE_NAME,
            self._resource_owner,
            ttl_seconds=_LEASE_TTL_SECONDS,
        )

    def _heartbeat_resource_lease(self) -> None:
        if self._active_resource_lease is None:
            return
        self._active_resource_lease = self._active_resource_lease.heartbeat(
            ttl_seconds=_LEASE_TTL_SECONDS
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

    def _execute(
        self,
        job_id: str,
        request: DocumentProduceRequest,
        authority: ExecutionAuthority,
        *,
        resumed_attempt: Attempt | None,
    ) -> JobSnapshot:
        job = self._repository.get_job_details(job_id)[0]
        self._portable.inspect(request.workspace_root)
        self._verify_source_stat(request)
        parsed = self._checkpoint_runner.run(
            job_id,
            "parse_document",
            job.request_hash,
            authority,
            lambda _execution: self._parse(request, job_id),
            encode=encode_parsed_document,
            decode=decode_parsed_document,
            resumed_attempt=resumed_attempt,
        )
        checkpoint = self._checkpoint_runner.run(
            job_id,
            "assemble_candidate_bundle",
            sha256_digest(f"document-candidate-v1:{job.request_hash}"),
            authority,
            lambda _execution: self._assemble(job_id, job.created_at, request, parsed),
            encode=lambda value: value.encode(),
            decode=DocumentCandidateCheckpoint.decode,
            resumed_attempt=resumed_attempt,
        )
        self._checkpoint_runner.run(
            job_id,
            "quality_and_portable_validation",
            job.request_hash,
            authority,
            lambda _execution: self._validate(request, checkpoint),
            resumed_attempt=resumed_attempt,
        )
        attempt = self._repository.create_attempt(job_id, "commit")
        attempt = self._repository.start_attempt(attempt.attempt_id, authority)
        return self._commit(request, checkpoint, attempt, authority)

    def _parse(self, request: DocumentProduceRequest, job_id: str) -> ParsedDocument:
        parsed = self._parser.parse(request.input_path, work_root=self._work_root)
        self._verify_source_stat(request)
        if parsed.source_sha256 != request.expected_source_sha256:
            raise DomainError(
                "document_input_changed",
                ErrorCategory.CONFLICT,
                "Document input changed after submission",
            )
        return parsed

    def _assemble(
        self,
        job_id: str,
        created_at: str,
        request: DocumentProduceRequest,
        parsed: ParsedDocument,
    ) -> DocumentCandidateCheckpoint:
        location = self._portable.candidate_location(
            request.workspace_root,
            local_instance_id=self._local_instance_id,
            nonce=sha256_digest(f"document-candidate-v1:{job_id}")[7:39],
        )
        candidate = DocumentBundleAssembler().assemble(
            parsed,
            job_id=job_id,
            created_at=created_at,
            location=location,
            source_id=self._existing_source_id(request),
        )
        block_count = sum(len(page.blocks) for page in parsed.pages)
        return DocumentCandidateCheckpoint(
            staging_relative_path=candidate.candidate.staging_relative_path,
            bundle_id=candidate.candidate.bundle_id,
            manifest_sha256=candidate.candidate.manifest_sha256,
            run_id=candidate.run_id,
            source_id=candidate.source_id,
            source_revision_id=candidate.source_revision_id,
            artifacts={
                "primary_draft": candidate.primary_draft_artifact_id,
                "normalized_content": candidate.normalized_artifact_id,
                "evidence_set": candidate.evidence_set_artifact_id,
                "quality_report": candidate.quality_report_artifact_id,
                "source_metadata": candidate.source_metadata_artifact_id,
            },
            quality_overall=candidate.quality_overall,
            publish_eligible=candidate.publish_eligible,
            usage={"pages": len(parsed.pages), "blocks": block_count},
            warnings=parsed.warnings,
        )

    def _existing_source_id(
        self,
        request: DocumentProduceRequest,
    ) -> str | None:
        binding = self._repository.read_source_identity_candidate(
            "local-document-sha256",
            request.expected_source_sha256,
        )
        return binding.source_id if binding is not None else None

    def _validate(
        self,
        request: DocumentProduceRequest,
        checkpoint: DocumentCandidateCheckpoint,
    ) -> None:
        report = self._portable.validate_candidate(
            request.workspace_root,
            checkpoint.staging_relative_path,
        )
        if (
            not report.valid
            or report.bundle_id != checkpoint.bundle_id
            or report.manifest_sha256 != checkpoint.manifest_sha256
        ):
            raise DomainError(
                "portable_bundle_validation_failed",
                ErrorCategory.RECIPE_FAILED,
                "Document candidate Bundle failed semantic validation",
            )

    def _commit(
        self,
        request: DocumentProduceRequest,
        checkpoint: DocumentCandidateCheckpoint,
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

        def commit() -> PortableCommitReceipt:
            nonlocal callback_entered
            callback_entered = True
            result = self._portable.commit_prepared(prepared)
            return PortableCommitReceipt(
                bundle_id=result.bundle_id,
                manifest_sha256=result.manifest_sha256,
                commit_sha256=result.commit_sha256,
                workspace_relative_bundle_path=result.relative_path,
                idempotent=result.idempotent,
            )

        try:
            self._heartbeat_resource_lease()
            self._repository.commit_recipe_result_atomic(
                attempt.job_id,
                attempt.attempt_id,
                authority,
                result_plan=RecipeResultPlan(
                    result_kind="document-note",
                    job_id=attempt.job_id,
                    run_id=checkpoint.run_id,
                    bundle_id=checkpoint.bundle_id,
                    manifest_sha256=checkpoint.manifest_sha256,
                    source_id=checkpoint.source_id,
                    source_revision_id=checkpoint.source_revision_id,
                    artifacts=checkpoint.artifacts,
                    quality_overall=checkpoint.quality_overall,
                    publish_eligible=checkpoint.publish_eligible,
                    usage=checkpoint.usage,
                    warnings=checkpoint.warnings,
                ),
                source_identity=SourceIdentityBinding(
                    connector_id="local-document-sha256",
                    canonical_identity=request.expected_source_sha256,
                    source_id=checkpoint.source_id,
                    owning_bundle_id=checkpoint.bundle_id,
                    manifest_sha256=checkpoint.manifest_sha256,
                ),
                commit=commit,
            )
        except BaseException:
            if not callback_entered:
                self._portable.discard_prepared(prepared)
            raise
        return self.get_job(attempt.job_id)

    def _reconcile_commit(
        self,
        request: DocumentProduceRequest,
        attempt: Attempt,
        authority: ExecutionAuthority,
    ) -> JobSnapshot:
        checkpoint = self._load_candidate(attempt.job_id)
        return self._commit(request, checkpoint, attempt, authority)

    def _load_candidate(self, job_id: str) -> DocumentCandidateCheckpoint:
        job = self._repository.get_job_details(job_id)[0]
        metadata = self._repository.latest_checkpoint(job_id, "assemble_candidate_bundle")
        input_hash = sha256_digest(f"document-candidate-v1:{job.request_hash}")
        if metadata is None or not self._attempt_storage.validate_checkpoint(
            metadata,
            expected_schema_id=CHECKPOINT_SCHEMA,
            expected_input_hash=input_hash,
        ):
            raise DomainError(
                "candidate_checkpoint_invalid",
                ErrorCategory.INTERNAL,
                "Document candidate checkpoint is unavailable",
            )
        return DocumentCandidateCheckpoint.decode(self._checkpoint_reader(metadata))

    def _load_request(self, job_id: str) -> DocumentProduceRequest:
        job = self._repository.get_job_details(job_id)[0]
        payload = self._repository.get_job_request(job_id)
        try:
            if payload is None or sha256_digest(payload) != job.request_hash:
                raise TypeError
            value = json.loads(payload)
            if type(value) is not dict or frozenset(value) != _REQUEST_FIELDS:
                raise TypeError
            return DocumentProduceRequest(
                request_schema_version=value["request_schema_version"],
                workspace_root=Path(value["workspace_root"]),
                input_path=Path(value["input_path"]),
                expected_source_sha256=value["expected_source_sha256"],
                expected_source_size=value["expected_source_size"],
                expected_source_mtime_ns=value["expected_source_mtime_ns"],
                recipe_id=value["recipe_id"],
                recipe_version=value["recipe_version"],
                principal=job.principal,
                client_request_id=job.client_request_id,
            )
        except (DomainError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise DomainError(
                "job_request_invalid",
                ErrorCategory.INTERNAL,
                "Stored Document request is invalid",
            ) from None

    @staticmethod
    def _verify_source_stat(request: DocumentProduceRequest) -> None:
        try:
            stat = request.input_path.stat()
        except OSError as error:
            raise DomainError(
                "document_input_unavailable",
                ErrorCategory.CONFLICT,
                "Document input is unavailable",
            ) from error
        if (
            not request.input_path.is_file()
            or stat.st_size != request.expected_source_size
            or stat.st_mtime_ns != request.expected_source_mtime_ns
        ):
            raise DomainError(
                "document_input_changed",
                ErrorCategory.CONFLICT,
                "Document input changed after submission",
            )

    def _fail_job(
        self,
        job_id: str,
        active_attempt: Attempt | None,
        authority: ExecutionAuthority,
        error: DomainError,
    ) -> None:
        job, current_attempt, _ = self._repository.get_job_details(job_id)
        attempt = current_attempt or active_attempt
        if job.state is not JobState.RUNNING:
            return
        running = (
            attempt
            if attempt is not None
            and attempt.state is AttemptState.RUNNING
            and attempt.fencing_token == authority.fencing_token
            else None
        )
        self._repository.fail_job_atomic(
            job_id,
            ErrorDetail(error.code, error.category, error.message, error.details),
            attempt_id=running.attempt_id if running is not None else None,
            authority=authority if running is not None else None,
        )


__all__ = ["DocumentService"]
