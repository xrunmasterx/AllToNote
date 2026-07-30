from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout
from iwiki.errors import IWikiError
from iwiki.workspace import open_workspace

from app.core.errors import DomainError, ErrorCategory


_CONTRACT_DIRECTORIES = (
    "raw",
    "raw/common",
    "raw/personal",
    "wiki",
    "wiki/common",
    "wiki/personal",
    ".cache",
)


if os.name == "nt":
    import msvcrt

    class _WorkspaceFileLock(FileLock):
        """Keep the lock inode stable between Windows contenders."""

        def _release(self) -> None:
            file_descriptor = self._context.lock_file_fd
            if file_descriptor is None:
                return
            self._context.lock_file_fd = None
            msvcrt.locking(file_descriptor, msvcrt.LK_UNLCK, 1)
            os.close(file_descriptor)

else:
    _WorkspaceFileLock = FileLock


@dataclass(frozen=True)
class WorkspaceInitialization:
    workspace_id: str
    name: str
    schema_version: int
    created: bool


def _is_link_or_reparse(path: Path, metadata: os.stat_result | None = None) -> bool:
    metadata = metadata or path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _reject_linked_ancestors(path: Path) -> None:
    candidate = path.absolute()
    while True:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise DomainError(
                "workspace_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Workspace path components cannot be inspected",
            ) from error
        else:
            if _is_link_or_reparse(candidate, metadata):
                raise DomainError(
                    "workspace_path_invalid",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Workspace path components must not be linked or reparse paths",
                )
        if candidate.parent == candidate:
            return
        candidate = candidate.parent


def _resolve_root(root: Path) -> Path:
    try:
        candidate = Path(root).expanduser()
        _reject_linked_ancestors(candidate)
        resolved = candidate.resolve(strict=False)
    except DomainError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "workspace_path_invalid",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Workspace root cannot be resolved",
        ) from error
    if resolved == Path(resolved.anchor):
        raise DomainError(
            "workspace_path_invalid",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Filesystem root cannot be initialized as a Workspace",
        )
    if resolved.exists() and not resolved.is_dir():
        raise DomainError(
            "workspace_path_invalid",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Workspace root must be a directory",
        )
    return resolved


def _validate_name(name: str) -> str:
    normalized = name.strip() if type(name) is str else ""
    if not normalized:
        raise DomainError(
            "workspace_name_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Workspace name must not be blank",
        )
    return normalized


def _preflight_paths(root: Path) -> None:
    control = root / ".llm-wiki"
    targets = (
        control,
        control / "manifest.yaml",
        *(root / relative for relative in _CONTRACT_DIRECTORIES),
    )
    for target in targets:
        try:
            if not _lexically_exists(target):
                continue
            expects_directory = target.name != "manifest.yaml"
            if _is_link_or_reparse(target) or (
                expects_directory and not target.is_dir()
            ):
                raise DomainError(
                    "workspace_path_invalid",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Workspace contract paths must be ordinary directories",
                )
        except DomainError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise DomainError(
                "workspace_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Workspace contract paths cannot be inspected",
            ) from error


def _require_existing_contract(root: Path) -> None:
    for relative in _CONTRACT_DIRECTORIES:
        target = root / relative
        if not _lexically_exists(target) or not target.is_dir():
            raise DomainError(
                "workspace_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Existing Workspace is missing required contract directories",
            )


def _manifest_bytes(workspace_id: str, name: str) -> bytes:
    quoted_id = json.dumps(workspace_id, ensure_ascii=False)
    quoted_name = json.dumps(name, ensure_ascii=False)
    return (
        "\n".join(
            (
                "schema_version: 2",
                f"workspace_id: {quoted_id}",
                f"name: {quoted_name}",
                "paths:",
                "  raw_common: raw/common",
                "  raw_personal: raw/personal",
                "  wiki_common: wiki/common",
                "  wiki_personal: wiki/personal",
                "  cache: .cache",
                "defaults:",
                "  publish_scope: personal",
                "  visibility: private",
                "  encoding: utf-8",
                "  link_style: relative",
                'description: "Created by AllToNote"',
                "",
            )
        )
    ).encode("utf-8")


def _open_initialized(root: Path, expected_name: str) -> WorkspaceInitialization:
    try:
        workspace = open_workspace(root, writable=True)
    except IWikiError as error:
        raise DomainError(
            "workspace_invalid",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Existing Workspace does not satisfy the writable V2 contract",
        ) from error
    if workspace.manifest.name != expected_name:
        raise DomainError(
            "workspace_already_initialized",
            ErrorCategory.CONFLICT,
            "Workspace is already initialized with a different name",
        )
    return WorkspaceInitialization(
        workspace_id=workspace.manifest.workspace_id,
        name=workspace.manifest.name,
        schema_version=workspace.manifest.schema_version,
        created=False,
    )


def _remove_empty_directories(paths: list[Path]) -> None:
    for path in reversed(paths):
        try:
            path.rmdir()
        except OSError:
            pass


def initialize_workspace(
    root: Path,
    name: str,
    *,
    lock_root: Path,
) -> WorkspaceInitialization:
    resolved_root = _resolve_root(root)
    normalized_name = _validate_name(name)
    try:
        resolved_lock_root = Path(lock_root).expanduser().resolve(strict=False)
        if (
            resolved_lock_root == resolved_root
            or resolved_lock_root.is_relative_to(resolved_root)
            or resolved_root.is_relative_to(resolved_lock_root)
        ):
            raise DomainError(
                "workspace_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Workspace initialization locks must remain outside the Workspace",
            )
        resolved_lock_root.mkdir(parents=True, exist_ok=True)
    except DomainError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "workspace_create_failed",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Workspace initialization lock directory is unavailable",
        ) from error
    identity = hashlib.sha256(
        os.path.normcase(str(resolved_root)).encode("utf-8")
    ).hexdigest()
    lock_path = resolved_lock_root / f"workspace-init-{identity}.lock"
    lock = _WorkspaceFileLock(str(lock_path))
    try:
        lock.acquire(timeout=5)
    except Timeout as error:
        raise DomainError(
            "workspace_init_busy",
            ErrorCategory.CONFLICT,
            "Another process is initializing this Workspace",
        ) from error
    except OSError as error:
        raise DomainError(
            "workspace_create_failed",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Workspace initialization lock could not be acquired",
        ) from error

    created_directories: list[Path] = []
    root_created = False
    control_created = False
    control = resolved_root / ".llm-wiki"
    manifest = control / "manifest.yaml"
    temporary: Path | None = None
    manifest_created = False
    try:
        _preflight_paths(resolved_root)
        root_created = not resolved_root.exists()
        resolved_root.mkdir(parents=True, exist_ok=True)
        control_created = not control.exists()
        control.mkdir(exist_ok=True)
        _preflight_paths(resolved_root)

        if manifest.exists():
            _require_existing_contract(resolved_root)
            result = _open_initialized(resolved_root, normalized_name)
            return result

        occupied = tuple(control.iterdir())
        if occupied:
            raise DomainError(
                "workspace_control_conflict",
                ErrorCategory.CONFLICT,
                "Workspace control directory is occupied but has no manifest",
            )

        for relative in _CONTRACT_DIRECTORIES:
            directory = resolved_root / relative
            if not directory.exists():
                directory.mkdir()
                created_directories.append(directory)

        workspace_id = str(uuid4())
        temporary = control / f".manifest.{uuid4().hex}.tmp"
        with temporary.open("xb") as stream:
            stream.write(_manifest_bytes(workspace_id, normalized_name))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest)
        temporary = None
        manifest_created = True
        initialized = _open_initialized(resolved_root, normalized_name)
        return WorkspaceInitialization(
            workspace_id=initialized.workspace_id,
            name=initialized.name,
            schema_version=initialized.schema_version,
            created=True,
        )
    except DomainError:
        if manifest_created:
            try:
                manifest.unlink(missing_ok=True)
            except OSError:
                pass
        _remove_empty_directories(created_directories)
        if control_created:
            _remove_empty_directories([control])
        if root_created:
            _remove_empty_directories([resolved_root])
        raise
    except (OSError, RuntimeError, ValueError) as error:
        if manifest_created:
            try:
                manifest.unlink(missing_ok=True)
            except OSError:
                pass
        _remove_empty_directories(created_directories)
        if control_created:
            _remove_empty_directories([control])
        if root_created:
            _remove_empty_directories([resolved_root])
        raise DomainError(
            "workspace_create_failed",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Workspace could not be initialized",
        ) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        lock.release()


__all__ = ["WorkspaceInitialization", "initialize_workspace"]
