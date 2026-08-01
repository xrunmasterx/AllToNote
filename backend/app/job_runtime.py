from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Protocol

from app.adapters.documents.document_basic_pack import (
    PACK_VERSION as DOCUMENT_PACK_VERSION,
)
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.core.application.job_query_service import (
    JobEventPage,
    JobPage,
    JobQueryService,
    JobView,
)
from app.core.application.job_service import JobService
from app.core.config.events import JOB_CONFIG_SNAPSHOT_EVENT
from app.core.config.model import JobConfigSnapshot
from app.core.domain.document import DOCUMENT_INPUT_SNAPSHOT_EVENT
from app.core.domain.video import (
    VIDEO_INPUT_SNAPSHOT_EVENT,
    JobSnapshot,
    JobState,
    RetryJobRequest,
    parse_video_input_snapshot_payload,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import (
    JobEvent,
    JobExecutionBinding,
    JobExecutionOwner,
    LOCAL_USER_PRINCIPAL,
)
from app.core.jobs.resource_lease import JobExecutionAuthority, ResourceLease
from app.core.jobs.state_machine import TERMINAL_JOB_STATES
from app.core.packs.events import (
    JOB_PACK_ENVIRONMENT_EVENT,
    JobPackEnvironmentSnapshot,
    parse_job_pack_environment_payload,
)
from app.core.portable.identity import is_executor_identity
from app.core.recipes.video.descriptor import VIDEO_DESCRIPTORS
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


LOCAL_CLI_PRINCIPAL = LOCAL_USER_PRINCIPAL
_DOCUMENT_EXECUTION_BINDING = JobExecutionBinding(
    recipe_id="alltonote.document-note",
    recipe_version=1,
    executor_id="alltonote.document",
    executor_version=1,
    pack_id="document-basic",
    pack_version=DOCUMENT_PACK_VERSION,
)
_LEGACY_VIDEO_EXECUTION_BINDING = JobExecutionBinding(
    recipe_id="alltonote.legacy",
    recipe_version=1,
    executor_id="alltonote.video",
    executor_version=1,
    pack_id="media-basic",
    pack_version="legacy-v1",
)
_VIDEO_RECIPE_KEYS = frozenset(
    (descriptor.key.recipe_id, descriptor.key.recipe_version)
    for descriptor in VIDEO_DESCRIPTORS
)


class JobExecutionRuntime(Protocol):
    job_repository: SqliteJobRepository

    def wait_job(
        self,
        job_id: str,
        event_sink: object | None = None,
    ) -> JobSnapshot: ...


class JobRuntime:
    """Headless Job query/control facade shared by CLI adapters."""

    def __init__(
        self,
        repository: SqliteJobRepository,
        *,
        wait_job: Callable[[str], JobSnapshot] | None,
        current_config_snapshot: JobConfigSnapshot | None,
        principal: str = LOCAL_CLI_PRINCIPAL,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
        notify_engine_job: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._query = JobQueryService(repository)
        self._jobs = JobService(repository)
        self._wait_job = wait_job
        self._current_config_snapshot = current_config_snapshot
        self._principal = principal
        self._execution_owner = execution_owner
        self._notify_engine_job = notify_engine_job
        self._monotonic = monotonic
        self._sleep = sleep

    def get_job(self, job_id: str) -> JobView:
        return self._query.get(job_id, principal=self._principal)

    def list_jobs(
        self,
        *,
        states: tuple[JobState, ...] = (),
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobPage:
        return self._query.list(
            principal=self._principal,
            states=states,
            cursor=cursor,
            limit=limit,
        )

    def get_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> JobEventPage:
        return self._query.events(
            job_id,
            principal=self._principal,
            after_sequence=after_sequence,
            limit=limit,
        )

    def stream_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Iterator[JobEvent]:
        cursor = after_sequence
        idle_delay_seconds = 0.1
        while True:
            page = self.get_job_events(
                job_id,
                after_sequence=cursor,
                limit=limit,
            )
            if page.events:
                yield from page.events
                cursor = page.events[-1].sequence
                idle_delay_seconds = 0.1
                continue
            current = self.get_job(job_id)
            if _is_wait_boundary(current.snapshot.state):
                final_page = self.get_job_events(
                    job_id,
                    after_sequence=cursor,
                    limit=limit,
                )
                if final_page.events:
                    yield from final_page.events
                    cursor = final_page.events[-1].sequence
                    continue
                return
            self._sleep(idle_delay_seconds)
            idle_delay_seconds = min(idle_delay_seconds * 2, 1.0)

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> JobView:
        current = self.get_job(job_id)
        if _is_wait_boundary(current.snapshot.state):
            return current
        if timeout_seconds is not None:
            if (
                type(timeout_seconds) not in {int, float}
                or isinstance(timeout_seconds, bool)
                or not math.isfinite(float(timeout_seconds))
                or timeout_seconds <= 0
                or timeout_seconds > 86_400
            ):
                raise DomainError(
                    "job_wait_timeout_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Job wait timeout must be greater than zero and at most one day",
                )
            deadline = self._monotonic() + float(timeout_seconds)
            while not _is_wait_boundary(current.snapshot.state):
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise DomainError(
                        "job_wait_timeout",
                        ErrorCategory.RETRYABLE_RUNTIME,
                        "Timed out while waiting for the durable Job state",
                        {
                            "job_id": job_id,
                            "timeout_seconds": timeout_seconds,
                        },
                    )
                self._sleep(min(0.05, remaining))
                current = self.get_job(job_id)
            return current
        if (
            self._repository.get_job(job_id).execution_owner
            is not self._execution_owner
        ):
            while not _is_wait_boundary(current.snapshot.state):
                self._sleep(0.05)
                current = self.get_job(job_id)
            return current
        if self._wait_job is None:
            raise DomainError(
                "job_execution_owner_unavailable",
                ErrorCategory.POLICY_DENIED,
                "No compatible execution owner is available for this Job",
            )
        self._wait_job(job_id)
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobView:
        try:
            self.get_job(job_id)
        except DomainError as error:
            if error.code != "job_projection_invalid":
                raise
        self._jobs.cancel(job_id)
        return self.get_job(job_id)

    def respond_job(
        self,
        job_id: str,
        challenge_id: str,
        response: Mapping[str, object],
    ) -> JobView:
        self.require_respondable(job_id, challenge_id)
        if not isinstance(response, Mapping):
            raise DomainError(
                "challenge_response_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Challenge response must be a JSON object",
            )
        self._jobs.respond(job_id, challenge_id, response)
        resumed = self.get_job(job_id)
        self._reactivate_engine_job(resumed)
        return resumed

    def require_respondable(
        self,
        job_id: str,
        challenge_id: str,
    ) -> JobView:
        current = self.get_job(job_id)
        challenge = current.pending_challenge
        if (
            current.snapshot.state is not JobState.WAITING_FOR_INPUT
            or challenge is None
            or challenge.challenge_id != challenge_id
        ):
            raise DomainError(
                "challenge_not_pending",
                ErrorCategory.CONFLICT,
                "Job does not have the specified pending Challenge",
            )
        if challenge.code == "external_outcome_unknown":
            raise DomainError(
                "external_outcome_unknown",
                ErrorCategory.CONFLICT,
                "Unknown external operations require manual reconciliation before retry",
                {"operation_ids": current.unknown_operation_ids},
            )
        if challenge.kind is None:
            raise DomainError(
                "challenge_schema_unsupported",
                ErrorCategory.CONFLICT,
                "Pending Challenge does not declare a supported response kind",
            )
        return current

    def retry_job(
        self,
        job_id: str,
        request: RetryJobRequest,
    ) -> JobView:
        self.get_job(job_id)
        initial_events: list[tuple[str, object]] = []
        if self._current_config_snapshot is not None:
            initial_events.append(
                (JOB_CONFIG_SNAPSHOT_EVENT, self._current_config_snapshot)
            )
        pack_events = tuple(
            event
            for event in self._repository.list_events(job_id)
            if event.event_type == JOB_PACK_ENVIRONMENT_EVENT
        )
        if len(pack_events) > 1:
            raise DomainError(
                "execution_pack_snapshot_invalid",
                ErrorCategory.CONFLICT,
                "The Job execution Pack environment is unavailable or invalid",
            )
        if pack_events:
            try:
                pack_environment = parse_job_pack_environment_payload(
                    pack_events[0].payload_json
                )
            except (TypeError, ValueError) as error:
                raise DomainError(
                    "execution_pack_snapshot_invalid",
                    ErrorCategory.INTERNAL,
                    "Stored Job execution Pack environment is invalid",
                ) from error
            initial_events.append(
                (JOB_PACK_ENVIRONMENT_EVENT, pack_environment)
            )
        document_input_events = tuple(
            event
            for event in self._repository.list_events(job_id)
            if event.event_type == DOCUMENT_INPUT_SNAPSHOT_EVENT
        )
        if len(document_input_events) > 1:
            raise DomainError(
                "document_input_snapshot_invalid",
                ErrorCategory.CONFLICT,
                "The Document input snapshot binding is unavailable or invalid",
            )
        if document_input_events:
            try:
                document_input = json.loads(document_input_events[0].payload_json)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise DomainError(
                    "document_input_snapshot_invalid",
                    ErrorCategory.INTERNAL,
                    "Stored Document input snapshot binding is invalid",
                ) from error
            if (
                type(document_input) is not dict
                or frozenset(document_input)
                != frozenset({"schema_version", "sha256", "byte_length"})
                or type(document_input.get("schema_version")) is not int
                or type(document_input.get("sha256")) is not str
                or type(document_input.get("byte_length")) is not int
            ):
                raise DomainError(
                    "document_input_snapshot_invalid",
                    ErrorCategory.INTERNAL,
                    "Stored Document input snapshot binding is invalid",
                )
            initial_events.append(
                (DOCUMENT_INPUT_SNAPSHOT_EVENT, document_input)
            )
        video_input_events = tuple(
            event
            for event in self._repository.list_events(job_id)
            if event.event_type == VIDEO_INPUT_SNAPSHOT_EVENT
        )
        if len(video_input_events) > 1:
            raise DomainError(
                "video_input_snapshot_invalid",
                ErrorCategory.CONFLICT,
                "The local Video input snapshot binding is unavailable or invalid",
            )
        if video_input_events:
            try:
                video_input = parse_video_input_snapshot_payload(
                    video_input_events[0].payload_json
                )
            except ValueError as error:
                raise DomainError(
                    "video_input_snapshot_invalid",
                    ErrorCategory.INTERNAL,
                    "Stored local Video input snapshot binding is invalid",
                ) from error
            initial_events.append((VIDEO_INPUT_SNAPSHOT_EVENT, video_input))
        retried = self._jobs.retry(
            job_id,
            request,
            initial_events=tuple(initial_events),
        )
        child = self.get_job(retried.job_id)
        self._reactivate_engine_job(child)
        return child

    def _reactivate_engine_job(self, view: JobView) -> None:
        if self._execution_owner is JobExecutionOwner.ENGINE:
            return
        job = self._repository.get_job(view.snapshot.job_id)
        if (
            job.execution_owner is not JobExecutionOwner.ENGINE
            or job.state not in {JobState.QUEUED, JobState.RUNNING}
        ):
            return
        if self._notify_engine_job is None:
            raise DomainError(
                "engine_job_reactivation_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                f"Job {job.job_id} is durable but Engine activation failed",
                {
                    "job_id": job.job_id,
                    "state": job.state.value,
                    "engine_error_code": "engine_notifier_unavailable",
                },
            )
        cause: BaseException | None = None
        try:
            self._notify_engine_job(job.job_id)
        except DomainError as error:
            engine_error_code = error.code
            cause = error
        except KeyboardInterrupt as error:
            engine_error_code = "interrupted"
            cause = error
        except Exception as error:
            engine_error_code = "internal_error"
            cause = error
        else:
            return
        raise DomainError(
            "engine_job_reactivation_failed",
            ErrorCategory.RETRYABLE_RUNTIME,
            f"Job {job.job_id} is durable but Engine activation failed",
            {
                "job_id": job.job_id,
                "state": job.state.value,
                "engine_error_code": engine_error_code,
            },
        ) from cause


def create_job_runtime_for_execution_runtime(
    runtime: JobExecutionRuntime,
    *,
    current_config_snapshot: JobConfigSnapshot | None,
) -> JobRuntime:
    return JobRuntime(
        runtime.job_repository,
        wait_job=lambda job_id: runtime.wait_job(job_id),
        current_config_snapshot=current_config_snapshot,
    )


def create_job_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
    runtime_paths: RuntimePaths | None = None,
    current_config_snapshot: JobConfigSnapshot | None,
    execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    require_existing_job_store: bool = False,
    adopted_resource_lease: ResourceLease | None = None,
    expected_job_authority: JobExecutionAuthority | None = None,
) -> JobRuntime:
    from iwiki.workspace import open_workspace

    if local_app_data is not None and runtime_paths is not None:
        raise ValueError("runtime_path_override_conflict")
    if (adopted_resource_lease is None) != (expected_job_authority is None):
        raise ValueError("resource_adoption_pair_required")
    paths = runtime_paths or resolve_runtime_paths(
        local_data_parent=local_app_data
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
    repository_factory = (
        SqliteJobRepository.open_existing
        if require_existing_job_store
        else SqliteJobRepository.open
    )
    repository = repository_factory(instance.machine_root / "job-store")

    def notify_engine_job(job_id: str) -> None:
        from app.engine.client import LocalEngineClient
        from app.engine.contracts import EngineJobReference

        LocalEngineClient(paths).notify_job(
            EngineJobReference(instance.instance_id, job_id)
        )

    def execute(job_id: str) -> JobSnapshot:
        binding = _require_supported_execution_binding(repository, job_id)
        execution_authority_options = (
            {
                "adopted_resource_lease": adopted_resource_lease,
                "expected_job_authority": expected_job_authority,
            }
            if adopted_resource_lease is not None
            else {}
        )
        if binding == _DOCUMENT_EXECUTION_BINDING:
            from app.runtime import create_document_runtime_for_workspace

            execution_pack_environment = _job_pack_environment(
                repository,
                job_id,
                binding,
            )
            requested_model_identity = None
            requested_provider_profile = None
            requested_verifier_model_identity = None
            requested_verifier_provider_profile = None
            request_json = repository.get_job_request(job_id)
            if request_json is not None:
                try:
                    request = json.loads(request_json)
                except (TypeError, ValueError, json.JSONDecodeError):
                    request = None
                if (
                    type(request) is dict
                    and type(request.get("request_schema_version")) is int
                    and request.get("request_schema_version") in (2, 3)
                    and type(request.get("model_override")) is str
                    and request["model_override"].strip()
                    and type(request.get("provider_profile")) is str
                    and request["provider_profile"].strip()
                ):
                    requested_model_identity = request["model_override"]
                    requested_provider_profile = request["provider_profile"]
                    if request["request_schema_version"] == 3:
                        if not (
                            type(request.get("verifier_model_override")) is str
                            and request["verifier_model_override"].strip()
                            and type(request.get("verifier_provider_profile")) is str
                            and request["verifier_provider_profile"].strip()
                        ):
                            raise DomainError(
                                "job_request_invalid",
                                ErrorCategory.INTERNAL,
                                "Stored Document request is invalid",
                            )
                        requested_verifier_model_identity = request[
                            "verifier_model_override"
                        ]
                        requested_verifier_provider_profile = request[
                            "verifier_provider_profile"
                        ]
            runtime = create_document_runtime_for_workspace(
                workspace_root,
                runtime_paths=paths,
                current_config_snapshot=current_config_snapshot,
                requested_model_identity=requested_model_identity,
                requested_provider_profile=requested_provider_profile,
                requested_verifier_model_identity=(
                    requested_verifier_model_identity
                ),
                requested_verifier_provider_profile=(
                    requested_verifier_provider_profile
                ),
                execution_pack_environment=execution_pack_environment,
                require_existing_job_store=require_existing_job_store,
                **execution_authority_options,
            )
        else:
            from app.runtime import create_codex_app_server_runtime_for_workspace

            requested_provider_profile, requested_model_identity = (
                _stored_video_model_selection(
                    repository.get_job_request(job_id)
                )
            )
            runtime = create_codex_app_server_runtime_for_workspace(
                workspace_root,
                runtime_paths=paths,
                current_config_snapshot=current_config_snapshot,
                requested_model_identity=requested_model_identity,
                requested_provider_profile=requested_provider_profile,
                execution_pack_environment=_job_pack_environment(
                    repository,
                    job_id,
                    binding,
                ),
                require_existing_job_store=require_existing_job_store,
                **execution_authority_options,
            )
        return runtime.wait_job(job_id)

    return JobRuntime(
        repository,
        wait_job=execute,
        current_config_snapshot=current_config_snapshot,
        execution_owner=execution_owner,
        notify_engine_job=notify_engine_job,
    )


def _stored_video_model_selection(
    request_json: str | None,
) -> tuple[str | None, str | None]:
    if request_json is None:
        return None, None
    try:
        request = json.loads(request_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None
    if type(request) is not dict:
        return None, None
    provider_profile = request.get("provider_profile")
    model_override = request.get("model_override")
    if provider_profile is not None and not is_executor_identity(provider_profile):
        raise DomainError(
            "job_request_invalid",
            ErrorCategory.INTERNAL,
            "Stored Video request is invalid",
        )
    if model_override is not None and not is_executor_identity(model_override):
        raise DomainError(
            "job_request_invalid",
            ErrorCategory.INTERNAL,
            "Stored Video request is invalid",
        )
    return (
        provider_profile,
        model_override,
    )


def _require_supported_execution_binding(
    repository: SqliteJobRepository,
    job_id: str,
) -> JobExecutionBinding:
    binding = repository.get_job_execution_binding(job_id)
    if binding in {
        _DOCUMENT_EXECUTION_BINDING,
        _LEGACY_VIDEO_EXECUTION_BINDING,
    }:
        return binding
    if (
        (binding.recipe_id, binding.recipe_version) in _VIDEO_RECIPE_KEYS
        and binding.executor_id == "alltonote.video"
        and binding.executor_version == 1
        and binding.pack_id == "media-basic"
    ):
        _job_pack_environment(repository, job_id, binding)
        return binding
    raise DomainError(
        "job_executor_unavailable",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "The exact persisted Job executor is unavailable",
    )


def _job_pack_environment(
    repository: SqliteJobRepository,
    job_id: str,
    binding: JobExecutionBinding,
) -> JobPackEnvironmentSnapshot:
    events = tuple(
        event
        for event in repository.list_events(job_id)
        if event.event_type == JOB_PACK_ENVIRONMENT_EVENT
    )
    if not events:
        raise DomainError(
            "execution_pack_snapshot_missing",
            ErrorCategory.CONFLICT,
            "The Job does not contain a frozen execution Pack environment",
        )
    if len(events) != 1:
        raise DomainError(
            "execution_pack_snapshot_invalid",
            ErrorCategory.CONFLICT,
            "The Job execution Pack environment is unavailable or invalid",
        )
    try:
        snapshot = parse_job_pack_environment_payload(events[0].payload_json)
    except (KeyError, TypeError, ValueError) as error:
        raise DomainError(
            "execution_pack_snapshot_invalid",
            ErrorCategory.INTERNAL,
            "Stored Job execution Pack environment is invalid",
        ) from error
    try:
        bound_pack = snapshot.pack(binding.pack_id)
    except ValueError as error:
        raise DomainError(
            "execution_pack_snapshot_invalid",
            ErrorCategory.CONFLICT,
            "The Job execution Pack environment does not match its executor binding",
        ) from error
    if bound_pack.pack_version != binding.pack_version:
        raise DomainError(
            "execution_pack_snapshot_invalid",
            ErrorCategory.CONFLICT,
            "The Job execution Pack environment does not match its executor binding",
        )
    return snapshot


def _is_wait_boundary(state: JobState) -> bool:
    return state in TERMINAL_JOB_STATES or state is JobState.WAITING_FOR_INPUT


__all__ = [
    "JobExecutionRuntime",
    "JobRuntime",
    "LOCAL_CLI_PRINCIPAL",
    "create_job_runtime_for_execution_runtime",
    "create_job_runtime_for_workspace",
]
