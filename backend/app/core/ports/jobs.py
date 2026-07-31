from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

from app.core.domain.production import RecipeProduceResult
from app.core.jobs.legacy_video_result import (
    QualityOverall,
    VideoProducedDocument,
    VideoProduceResult,
)
from app.core.application.video_acquisition import AttemptStoredAsset, StoredAssetRole
from app.core.errors import ErrorDetail
from app.core.jobs.external_operation import ExternalOperation
from app.core.jobs.model import (
    Attempt,
    AttemptState,
    Challenge,
    CheckpointMetadata,
    CheckpointRecord,
    Job,
    JobExecutionBinding,
    JobEvent,
    JobState,
)
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.ports.source import CancellationTokenPort


@dataclass(frozen=True)
class SourceIdentityBinding:
    connector_id: str
    canonical_identity: str
    source_id: str
    owning_bundle_id: str
    manifest_sha256: str


@dataclass(frozen=True)
class VideoResultPlan:
    job_id: str
    run_id: str
    bundle_id: str
    manifest_sha256: str
    source_id: str
    source_revision_id: str
    primary_draft_artifact_id: str
    transcript_artifact_id: str
    evidence_set_artifact_id: str
    quality_report_artifact_id: str
    display_asset_ids: tuple[str, ...]
    quality_overall: QualityOverall
    publish_eligible: bool
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]
    documents: tuple[VideoProducedDocument, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_asset_ids", tuple(self.display_asset_ids))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "documents", tuple(self.documents))


@dataclass(frozen=True)
class RecipeResultPlan:
    result_kind: str
    job_id: str
    run_id: str
    bundle_id: str
    manifest_sha256: str
    source_id: str
    source_revision_id: str
    artifacts: Mapping[str, str]
    quality_overall: str
    publish_eligible: bool
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class PortableCommitReceipt:
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    workspace_relative_bundle_path: str
    idempotent: bool


@dataclass(frozen=True)
class JobCompletion:
    result: VideoProduceResult | RecipeProduceResult
    source_identity: SourceIdentityBinding


@dataclass(frozen=True)
class ScreenshotSourceCapability:
    job_id: str
    attempt_id: str
    relative_locator: str
    device: int
    inode: int
    byte_length: int


@dataclass(frozen=True)
class ScreenshotOutputCapability:
    job_id: str
    attempt_id: str
    artifact_id: str
    relative_locator: str
    parent_device: int
    parent_inode: int
    leaf_device: int
    leaf_inode: int
    authority_owner_id: str
    authority_fencing_token: int


class JobRepositoryPort(Protocol):
    """Boundary for durable job state and execution records."""

    def create_job(
        self,
        *,
        request_hash: str,
        request_json: str | None = None,
        principal: str,
        client_request_id: str | None,
        retry_of_job_id: str | None = None,
        initial_events: tuple[tuple[str, str], ...] = (),
        execution_binding: JobExecutionBinding | None = None,
    ) -> Job: ...

    def get_job_execution_binding(self, job_id: str) -> JobExecutionBinding: ...

    def read_source_identity_candidate(
        self,
        connector_id: str,
        canonical_identity: str,
    ) -> SourceIdentityBinding | None: ...

    def get_job_details(
        self, job_id: str
    ) -> tuple[Job, Attempt | None, Challenge | None]: ...

    def get_job_result(
        self, job_id: str
    ) -> VideoProduceResult | RecipeProduceResult | None: ...

    def get_job_request(self, job_id: str) -> str | None: ...

    def get_job_error(self, job_id: str) -> ErrorDetail | None: ...

    def commit_video_result_atomic(
        self,
        job_id: str,
        attempt_id: str,
        authority: ExecutionAuthority,
        *,
        result_plan: VideoResultPlan,
        source_identity: SourceIdentityBinding,
        commit: Callable[[], PortableCommitReceipt],
    ) -> JobCompletion: ...

    def commit_recipe_result_atomic(
        self,
        job_id: str,
        attempt_id: str,
        authority: ExecutionAuthority,
        *,
        result_plan: RecipeResultPlan,
        source_identity: SourceIdentityBinding,
        commit: Callable[[], PortableCommitReceipt],
    ) -> JobCompletion: ...

    def fail_job_atomic(
        self,
        job_id: str,
        error: ErrorDetail,
        *,
        attempt_id: str | None = None,
        authority: ExecutionAuthority | None = None,
    ) -> Job: ...

    def cancel_job(self, job_id: str) -> Job: ...

    def respond_challenge_atomic(
        self,
        job_id: str,
        challenge_id: str,
        *,
        response_hash: str,
        response_json: str,
    ) -> tuple[Job, Attempt]: ...

    def create_retry_job_atomic(
        self,
        original_job_id: str,
        *,
        expected_original_state: JobState,
        confirmed_unknown_operation_ids: tuple[str, ...],
        client_request_id: str,
        initial_events: tuple[tuple[str, str], ...] = (),
    ) -> Job: ...


class JobExecutionRepositoryPort(JobRepositoryPort, Protocol):
    """Narrow execution surface used by sequential Recipe executors."""

    def transition_job(self, job_id: str, state: JobState) -> Job: ...

    def create_attempt(self, job_id: str, step_id: str) -> Attempt: ...

    def acquire_scheduler_lease(
        self, owner_id: str, *, ttl_seconds: int
    ) -> ExecutionAuthority: ...

    def heartbeat_scheduler_lease(
        self, authority: ExecutionAuthority, *, ttl_seconds: int
    ) -> ExecutionAuthority: ...

    def release_scheduler_lease(
        self, authority: ExecutionAuthority
    ) -> bool: ...

    def start_attempt(
        self, attempt_id: str, authority: ExecutionAuthority
    ) -> Attempt: ...

    def transition_attempt(
        self,
        attempt_id: str,
        state: AttemptState,
        *,
        authority: ExecutionAuthority | None = None,
    ) -> Attempt: ...

    def latest_checkpoint(
        self, job_id: str, step_id: str
    ) -> CheckpointMetadata | None: ...

    def take_over_running_attempt(
        self,
        job_id: str,
        attempt_id: str,
        authority: ExecutionAuthority,
    ) -> Attempt: ...

    def reconcile_external_operations_after_process_loss(
        self,
        job_id: str,
        authority: ExecutionAuthority,
    ) -> tuple[ExternalOperation, ...]: ...

    def pause_for_external_outcome_atomic(
        self,
        job_id: str,
        attempt_id: str,
        authority: ExecutionAuthority,
    ) -> Challenge: ...


VideoExecutionRepositoryPort = JobExecutionRepositoryPort


class AttemptQueryRepositoryPort(Protocol):
    """Read-only Attempt history used for persisted execution provenance."""

    def list_attempts(self, job_id: str) -> tuple[Attempt, ...]: ...


class AttemptMetadataRepositoryPort(Protocol):
    """Durable metadata boundary for checkpoints and Job events."""

    def record_checkpoint(
        self,
        metadata: CheckpointMetadata,
        authority: ExecutionAuthority,
    ) -> CheckpointMetadata: ...

    def latest_checkpoint(
        self, job_id: str, step_id: str
    ) -> CheckpointMetadata | None: ...

    def append_event(
        self, job_id: str, event_type: str, payload_json: str
    ) -> JobEvent: ...

    def list_events(
        self, job_id: str, after_sequence: int = 0
    ) -> tuple[JobEvent, ...]: ...

    def authorize_attempt_storage(
        self,
        job_id: str,
        attempt_id: str,
        authority: ExecutionAuthority,
        *,
        expected_step_id: str | None = None,
    ) -> None: ...


class AttemptStoragePort(Protocol):
    """Boundary for private attempt staging and checkpoints."""

    def save_checkpoint(
        self, record: CheckpointRecord, authority: ExecutionAuthority
    ) -> CheckpointMetadata: ...

    def validate_checkpoint(
        self,
        metadata: CheckpointMetadata,
        *,
        expected_schema_id: str,
        expected_input_hash: str,
    ) -> bool: ...

    def append_event(
        self, job_id: str, event_type: str, payload_json: str
    ) -> JobEvent: ...

    def reconcile_event_projection(self, job_id: str) -> tuple[JobEvent, ...]: ...

    def snapshot_asset(
        self,
        source_path: Path,
        *,
        job_id: str,
        attempt_id: str,
        role: StoredAssetRole,
        expected_sha256: str,
        authority: ExecutionAuthority,
        token: CancellationTokenPort,
    ) -> AttemptStoredAsset: ...

    def resolve_asset(
        self,
        stored: AttemptStoredAsset,
        *,
        expected_job_id: str,
        expected_attempt_id: str,
    ) -> Path: ...

    def allocate_screenshot_output(
        self,
        *,
        job_id: str,
        attempt_id: str,
        artifact_id: str,
        authority: ExecutionAuthority,
    ) -> ScreenshotOutputCapability: ...

    def validate_screenshot_output(
        self,
        capability: ScreenshotOutputCapability,
        *,
        authority: ExecutionAuthority,
    ) -> Path: ...

    def read_screenshot_output(
        self,
        capability: ScreenshotOutputCapability,
        *,
        job_id: str,
        attempt_id: str,
        artifact_id: str,
        authority: ExecutionAuthority,
    ) -> bytes: ...

    def cleanup_screenshot_output(
        self,
        capability: ScreenshotOutputCapability,
        *,
        authority: ExecutionAuthority,
    ) -> None: ...

    def verify_screenshot_source(
        self,
        stored: AttemptStoredAsset,
        *,
        expected_job_id: str,
        expected_attempt_id: str,
    ) -> ScreenshotSourceCapability: ...

    def validate_screenshot_source(
        self, capability: ScreenshotSourceCapability
    ) -> Path: ...
