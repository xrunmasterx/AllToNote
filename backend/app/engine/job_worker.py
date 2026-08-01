from __future__ import annotations

import argparse
import json
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import RFC_4122, UUID

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import (
    WorkspaceInstance,
    WorkspaceInstanceRegistry,
)
from app.core.config.events import JOB_CONFIG_SNAPSHOT_EVENT
from app.core.config.model import JobConfigSnapshot
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner
from app.job_runtime import LOCAL_CLI_PRINCIPAL
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


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


def execute_engine_job(
    paths: RuntimePaths,
    *,
    workspace_instance_id: str,
    job_id: str,
    inspect_workspace: Callable[[Path], str] | None = None,
    runtime_factory: Callable[..., object] | None = None,
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
    workspace_root = _validated_workspace(paths, instance, inspector)
    repository = _open_existing_job_store(paths, instance)
    job = repository.get_job(job_id)
    if (
        job.execution_owner is not JobExecutionOwner.ENGINE
        or job.principal != LOCAL_CLI_PRINCIPAL
    ):
        raise DomainError(
            "engine_job_authority_denied",
            ErrorCategory.POLICY_DENIED,
            "Engine is not authorized to execute this Job",
        )
    config_snapshot = _load_config_snapshot(repository, job_id)
    if runtime_factory is None:
        from app.job_runtime import create_job_runtime_for_workspace

        runtime_factory = create_job_runtime_for_workspace
    runtime = runtime_factory(
        workspace_root,
        local_app_data=paths.workspace_registry_parent,
        current_config_snapshot=config_snapshot,
        require_existing_job_store=True,
    )
    return runtime.wait_for_job(job_id)


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


def _open_existing_job_store(
    paths: RuntimePaths,
    instance: WorkspaceInstance,
) -> SqliteJobRepository:
    expected_machine_root = paths.workspace_machine_parent / instance.instance_id
    job_store = expected_machine_root / "job-store"
    database = job_store / "jobs.sqlite"
    if instance.machine_root != expected_machine_root:
        _raise_job_store_unavailable()
    for directory in (
        paths.workspace_machine_parent,
        expected_machine_root,
        job_store,
    ):
        _require_exact_ordinary_path(directory, directory=True)
    _require_exact_ordinary_path(database, directory=False)
    return SqliteJobRepository.open_existing(job_store)


def _require_exact_ordinary_path(path: Path, *, directory: bool) -> None:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError):
        _raise_job_store_unavailable()
    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    valid_kind = (
        stat.S_ISDIR(metadata.st_mode)
        if directory
        else stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
    )
    if resolved != path or is_reparse or not valid_kind:
        _raise_job_store_unavailable()


def _raise_job_store_unavailable() -> None:
    raise DomainError(
        "engine_job_store_unavailable",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Engine JobStore is unavailable",
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alltonote-engine-job-worker-private")
    parser.add_argument("--local-data-parent", required=True, type=Path)
    parser.add_argument("--workspace-instance-id", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    try:
        execute_engine_job(
            resolve_runtime_paths(local_data_parent=args.local_data_parent),
            workspace_instance_id=args.workspace_instance_id,
            job_id=args.job_id,
        )
    except KeyboardInterrupt:
        return 130
    except DomainError as error:
        return _EXIT_CODES[error.category]
    except Exception:
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["execute_engine_job", "main"]
