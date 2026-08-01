from __future__ import annotations

import stat
from pathlib import Path

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstance
from app.core.errors import DomainError, ErrorCategory
from app.runtime_paths import RuntimePaths


def open_engine_job_store(
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


__all__ = ["open_engine_job_store"]
