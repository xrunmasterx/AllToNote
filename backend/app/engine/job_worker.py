from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import RFC_4122, UUID, uuid4

from app.adapters.jobs.workspace_instance_registry import (
    WorkspaceInstance,
    WorkspaceInstanceRegistry,
)
from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.core.config.events import JOB_CONFIG_SNAPSHOT_EVENT
from app.core.config.model import JobConfigSnapshot
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.jobs.model import (
    AttemptState,
    LOCAL_USER_PRINCIPAL,
    JobExecutionOwner,
    JobState,
)
from app.core.jobs.resource_lease import (
    JobExecutionAuthority,
    ResourceLease,
)
from app.engine.contracts import (
    EngineProtocolError,
    EngineWorkerLaunchV1,
    MAX_WORKER_LAUNCH_BYTES,
)
from app.engine.job_store import open_engine_job_store
from app.runtime_paths import RuntimePaths


_EXIT_CODES = {
    ErrorCategory.INVALID_REQUEST: 2,
    ErrorCategory.WORKSPACE_INCOMPATIBLE: 10,
    ErrorCategory.CONFLICT: 20,
    ErrorCategory.RETRYABLE_RUNTIME: 30,
    ErrorCategory.POLICY_DENIED: 40,
    ErrorCategory.RECIPE_FAILED: 50,
    ErrorCategory.CANCELLED: 60,
    ErrorCategory.INTERNAL: 70,
}
_PERSISTENT_FAILURE_CATEGORIES = frozenset(
    {
        ErrorCategory.INVALID_REQUEST,
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        ErrorCategory.POLICY_DENIED,
        ErrorCategory.RECIPE_FAILED,
        ErrorCategory.INTERNAL,
    }
)
_PERSISTENT_CONFLICT_CODES = frozenset(
    {
        "effective_config_drift",
        "effective_config_unavailable",
        "execution_pack_snapshot_invalid",
        "execution_pack_snapshot_missing",
    }
)
_SUPERVISED_AUTHORITY_TTL_SECONDS = 300
_RECOVERY_CLAIM_TTL_SECONDS = 30


def execute_engine_job(
    paths: RuntimePaths,
    *,
    workspace_instance_id: str,
    job_id: str,
    inspect_workspace: Callable[[Path], str] | None = None,
    runtime_factory: Callable[..., object] | None = None,
    launch: EngineWorkerLaunchV1 | None = None,
) -> object:
    if not _is_typed_job_id(job_id):
        raise DomainError(
            "engine_job_reference_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Engine Job reference is invalid",
        )
    inspector = inspect_workspace or _default_workspace_inspector
    registry = WorkspaceInstanceRegistry(
        paths.workspace_registry_parent,
        inspect_workspace=inspector,
    )
    try:
        instance = registry.get(workspace_instance_id)
    except ValueError as error:
        raise DomainError(
            "engine_job_reference_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Engine Job reference is invalid",
        ) from error
    if instance is None:
        raise DomainError(
            "workspace_instance_not_found",
            ErrorCategory.INVALID_REQUEST,
            "Workspace instance does not exist",
        )
    if launch is not None and (
        launch.reference.workspace_instance_id != workspace_instance_id
        or launch.reference.job_id != job_id
        or launch.resource_handoff.owner.workspace_identity
        != instance.workspace_identity
    ):
        raise DomainError(
            "engine_worker_launch_invalid",
            ErrorCategory.POLICY_DENIED,
            "Engine Worker launch does not match the registered Job",
        )
    adopted_resource_lease: ResourceLease | None = None
    expected_job_authority: JobExecutionAuthority | None = None
    if launch is not None:
        adopted_resource_lease = MachineResourceLeaseStore.open(
            paths.data_dir / "machine"
        ).adopt(
            launch.resource_handoff,
            ttl_seconds=_SUPERVISED_AUTHORITY_TTL_SECONDS,
        )
        expected_job_authority = launch.job_authority
    repository = open_engine_job_store(paths, instance)
    job = repository.get_job(job_id)
    if (
        job.execution_owner is not JobExecutionOwner.ENGINE
        or job.principal != LOCAL_USER_PRINCIPAL
    ):
        raise DomainError(
            "engine_job_authority_denied",
            ErrorCategory.POLICY_DENIED,
            "Engine is not authorized to execute this Job",
        )
    try:
        if expected_job_authority is not None:
            claim = repository.claim_job(
                job_id,
                expected_job_authority.owner_id,
                ttl_seconds=_SUPERVISED_AUTHORITY_TTL_SECONDS,
            )
            if claim.authority != expected_job_authority:
                raise DomainError(
                    "job_claim_fenced",
                    ErrorCategory.CONFLICT,
                    "Engine Worker Job authority is expired or fenced",
                )
        workspace_root = _validated_workspace(paths, instance, inspector)
        config_snapshot = _load_config_snapshot(repository, job_id)
        if runtime_factory is None:
            from app.job_runtime import create_job_runtime_for_workspace

            runtime_factory = create_job_runtime_for_workspace
        runtime_options = {
            "runtime_paths": paths,
            "current_config_snapshot": config_snapshot,
            "execution_owner": JobExecutionOwner.ENGINE,
            "require_existing_job_store": True,
        }
        if adopted_resource_lease is not None:
            runtime_options.update(
                {
                    "adopted_resource_lease": adopted_resource_lease,
                    "expected_job_authority": expected_job_authority,
                }
            )
        runtime = runtime_factory(workspace_root, **runtime_options)
        return runtime.wait_for_job(job_id)
    except DomainError as error:
        _persist_permanent_failure(
            repository,
            job_id,
            error,
            expected_authority=expected_job_authority,
        )
        raise
    finally:
        if expected_job_authority is not None:
            try:
                repository.release_job_claim(expected_job_authority)
            except Exception:
                pass
        if adopted_resource_lease is not None:
            try:
                adopted_resource_lease.release()
            except Exception:
                pass


def _persist_permanent_failure(
    repository: SqliteJobRepository,
    job_id: str,
    error: DomainError,
    *,
    expected_authority: JobExecutionAuthority | None = None,
) -> None:
    if not (
        error.category in _PERSISTENT_FAILURE_CATEGORIES
        or error.code in _PERSISTENT_CONFLICT_CODES
    ):
        return
    authority = None
    try:
        job = repository.get_job(job_id)
        if (
            job.execution_owner is not JobExecutionOwner.ENGINE
            or job.principal != LOCAL_USER_PRINCIPAL
            or job.state not in {JobState.QUEUED, JobState.RUNNING}
        ):
            return
        claim = repository.claim_job(
            job_id,
            (
                expected_authority.owner_id
                if expected_authority is not None
                else f"engine-recovery-{uuid4().hex}"
            ),
            ttl_seconds=_RECOVERY_CLAIM_TTL_SECONDS,
        )
        if expected_authority is not None and claim.authority != expected_authority:
            return
        authority = claim.authority
        if claim.job.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.WAITING_FOR_INPUT,
        }:
            return
        attempt = claim.active_attempt
        if (
            attempt is not None
            and attempt.state is AttemptState.RUNNING
            and attempt.fencing_token != authority.fencing_token
        ):
            attempt = repository.take_over_running_attempt(
                job_id,
                attempt.attempt_id,
                authority,
            )
            unknown = repository.reconcile_external_operations_after_process_loss(
                job_id,
                authority,
            )
            if unknown:
                repository.pause_for_external_outcome_atomic(
                    job_id,
                    attempt.attempt_id,
                    authority,
                )
                return
        repository.fail_job_atomic(
            job_id,
            ErrorDetail(
                error.code,
                error.category,
                error.message,
                error.details,
            ),
            attempt_id=(attempt.attempt_id if attempt is not None else None),
            authority=authority,
        )
    except Exception:
        return
    finally:
        if authority is not None:
            try:
                repository.release_job_claim(authority)
            except Exception:
                pass


def _validated_workspace(
    paths: RuntimePaths,
    instance: WorkspaceInstance,
    inspect_workspace: Callable[[Path], str],
) -> Path:
    try:
        workspace_root = instance.canonical_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "workspace_instance_unavailable",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Registered Workspace is unavailable",
        ) from error
    if workspace_root != instance.canonical_root or not workspace_root.is_dir():
        raise DomainError(
            "workspace_instance_unavailable",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Registered Workspace is unavailable",
        )
    paths.assert_outside_workspace(workspace_root)
    try:
        observed_identity = inspect_workspace(workspace_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "workspace_instance_unavailable",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Registered Workspace is unavailable",
        ) from error
    if observed_identity != instance.workspace_identity:
        raise DomainError(
            "workspace_instance_identity_mismatch",
            ErrorCategory.POLICY_DENIED,
            "Registered Workspace identity has changed",
        )
    return workspace_root


def _load_config_snapshot(
    repository: SqliteJobRepository,
    job_id: str,
) -> JobConfigSnapshot | None:
    events = tuple(
        event
        for event in repository.list_events(job_id)
        if event.event_type == JOB_CONFIG_SNAPSHOT_EVENT
    )
    if not events:
        return None
    if len(events) != 1:
        raise DomainError(
            "config_snapshot_invalid",
            ErrorCategory.INTERNAL,
            "Stored Job configuration snapshot is invalid",
        )
    try:
        payload = json.loads(events[0].payload_json)
        if type(payload) is not dict or frozenset(payload) != frozenset(
            {"snapshot_version", "values", "digest", "semantic_digest"}
        ):
            raise ValueError("config_snapshot_invalid")
        return JobConfigSnapshot(
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


def _default_workspace_inspector(workspace_root: Path) -> str:
    from iwiki.workspace import open_workspace

    return open_workspace(
        workspace_root,
        writable=False,
    ).manifest.workspace_id


def _is_typed_job_id(value: object) -> bool:
    if type(value) is not str or not value.startswith("job_"):
        return False
    try:
        parsed = UUID(value[4:])
    except ValueError:
        return False
    return (
        parsed.version == 7
        and parsed.variant == RFC_4122
        and str(parsed) == value[4:]
    )


def main(
    argv: list[str] | None = None,
    *,
    stdin_payload: bytes | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="alltonote-engine-job-worker-private")
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--workspace-instance-id", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    try:
        payload = (
            stdin_payload
            if stdin_payload is not None
            else sys.stdin.buffer.read(MAX_WORKER_LAUNCH_BYTES + 1)
        )
        launch = EngineWorkerLaunchV1.from_bytes(payload)
        if (
            launch.reference.workspace_instance_id != args.workspace_instance_id
            or launch.reference.job_id != args.job_id
        ):
            raise EngineProtocolError("engine_worker_launch_invalid")
        execute_engine_job(
            RuntimePaths(
                config_dir=args.config_dir,
                data_dir=args.data_dir,
                cache_dir=args.cache_dir,
                state_dir=args.state_dir,
                log_dir=args.log_dir,
            ),
            workspace_instance_id=args.workspace_instance_id,
            job_id=args.job_id,
            launch=launch,
        )
    except KeyboardInterrupt:
        return 130
    except DomainError as error:
        return _EXIT_CODES[error.category]
    except EngineProtocolError:
        return 2
    except Exception:
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["execute_engine_job", "main", "open_engine_job_store"]
