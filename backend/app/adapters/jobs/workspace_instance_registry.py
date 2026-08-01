from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from app.core.errors import DomainError, ErrorCategory


_REGISTRY_VERSION = 1
_REGISTRY_KEYS = frozenset({"version", "instances"})
_ENTRY_KEYS = frozenset(
    {"instance_id", "workspace_identity", "canonical_root"}
)
_INSTANCE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_MAXIMUM_REGISTRY_BYTES = 1024 * 1024
_MAXIMUM_REGISTRY_INSTANCES = 4096


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


@dataclass(frozen=True)
class WorkspaceInstance:
    instance_id: str
    workspace_identity: str
    canonical_root: Path
    machine_root: Path


class WorkspaceInstanceRegistry:
    def __init__(
        self,
        local_app_data: Path,
        *,
        inspect_workspace: Callable[[Path], str],
    ) -> None:
        try:
            trusted_local_root = Path(local_app_data).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            self._raise_root_unsafe(error)
        if not trusted_local_root.is_dir():
            self._raise_root_unsafe()
        self._trusted_local_root = trusted_local_root
        self._app_root = trusted_local_root / "AllToNote"
        self._registry_path = self._app_root / "workspace-instances.json"
        self._lock_path = self._app_root / "workspace-instances.json.lock"
        self._inspect_workspace = inspect_workspace

    def resolve(self, workspace_root: Path) -> WorkspaceInstance:
        try:
            canonical = Path(workspace_root).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("workspace_root_not_directory") from error
        if not canonical.is_dir():
            raise ValueError("workspace_root_not_directory")

        workspace_identity = self._inspect_workspace(canonical)
        if not isinstance(workspace_identity, str) or not workspace_identity.strip():
            raise ValueError("workspace_identity_invalid")

        normalized_root = Path(os.path.normcase(os.path.normpath(str(canonical))))
        self._app_root = self._ensure_contained_directory(self._app_root)
        machine_parent = self._ensure_contained_directory(
            self._app_root / "workspaces"
        )
        self._registry_path = self._app_root / "workspace-instances.json"
        self._lock_path = self._app_root / "workspace-instances.json.lock"
        with FileLock(self._lock_path):
            registry = self._read_registry()
            instance = self._find_instance(
                registry["instances"], workspace_identity, normalized_root
            )
            if instance is None:
                if len(registry["instances"]) >= _MAXIMUM_REGISTRY_INSTANCES:
                    raise ValueError("workspace_instance_registry_full")
                instance = {
                    "instance_id": uuid4().hex,
                    "workspace_identity": workspace_identity,
                    "canonical_root": str(normalized_root),
                }
                resolved_instance = self._to_workspace_instance(
                    instance, machine_parent
                )
                registry["instances"].append(instance)
                registry["instances"].sort(
                    key=lambda item: (
                        item["workspace_identity"],
                        os.path.normcase(item["canonical_root"]),
                    )
                )
                self._write_registry(registry)
            else:
                resolved_instance = self._to_workspace_instance(
                    instance, machine_parent
                )

        return resolved_instance

    def get(self, instance_id: str) -> WorkspaceInstance | None:
        if (
            type(instance_id) is not str
            or _INSTANCE_ID_PATTERN.fullmatch(instance_id) is None
        ):
            raise ValueError("workspace_instance_id_invalid")
        for instance in self.list():
            if instance.instance_id == instance_id:
                return instance
        return None

    def list(self) -> tuple[WorkspaceInstance, ...]:
        registry = self._read_registry()
        instances = registry["instances"]
        if not instances:
            return ()
        try:
            machine_parent = (self._app_root / "workspaces").resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            self._raise_root_unsafe(error)
        if not machine_parent.is_relative_to(self._trusted_local_root):
            self._raise_root_unsafe()
        return tuple(
            self._to_workspace_instance(instance, machine_parent)
            for instance in instances
        )

    def _ensure_contained_directory(self, path: Path) -> Path:
        try:
            before = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            self._raise_root_unsafe(error)
        if not before.is_relative_to(self._trusted_local_root):
            self._raise_root_unsafe()

        try:
            path.mkdir(exist_ok=True)
            after = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            self._raise_root_unsafe(error)
        if (
            not after.is_dir()
            or not after.is_relative_to(self._trusted_local_root)
        ):
            self._raise_root_unsafe()
        return after

    @staticmethod
    def _raise_root_unsafe(error: BaseException | None = None) -> None:
        raise DomainError(
            "workspace_instance_root_unsafe",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Workspace instance root is outside the trusted local root",
        ) from error

    def _read_registry(self) -> dict[str, object]:
        self._validate_read_only_paths()
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._registry_path, flags)
            if not self._registry_handle_matches(descriptor):
                raise ValueError("workspace_instance_registry_invalid")
            chunks: list[bytes] = []
            remaining = _MAXIMUM_REGISTRY_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > _MAXIMUM_REGISTRY_BYTES or not (
                self._registry_handle_matches(descriptor)
            ):
                raise ValueError("workspace_instance_registry_invalid")
            registry = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
            )
        except FileNotFoundError:
            return {"version": _REGISTRY_VERSION, "instances": []}
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            raise ValueError("workspace_instance_registry_invalid") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self._validate_registry(registry)
        return registry

    def _registry_handle_matches(self, descriptor: int) -> bool:
        try:
            path_metadata = self._registry_path.lstat()
            opened_metadata = os.fstat(descriptor)
        except OSError:
            return False
        is_reparse = bool(
            getattr(path_metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        return (
            not is_reparse
            and stat.S_ISREG(path_metadata.st_mode)
            and path_metadata.st_nlink == 1
            and path_metadata.st_dev == opened_metadata.st_dev
            and path_metadata.st_ino == opened_metadata.st_ino
        )

    def _validate_read_only_paths(self) -> None:
        controlled_paths = (
            (self._app_root, "directory"),
            (self._app_root / "workspaces", "directory"),
            (self._registry_path, "file"),
        )
        for path, expected_kind in controlled_paths:
            try:
                resolved = path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError) as error:
                self._raise_root_unsafe(error)
            if resolved != path or not resolved.is_relative_to(
                self._trusted_local_root
            ):
                self._raise_root_unsafe()
            if not os.path.lexists(path):
                continue
            try:
                metadata = path.lstat()
            except OSError as error:
                self._raise_root_unsafe(error)
            is_reparse = bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if is_reparse:
                self._raise_root_unsafe()
            if expected_kind == "directory":
                if not stat.S_ISDIR(metadata.st_mode):
                    self._raise_root_unsafe()
            elif not stat.S_ISREG(metadata.st_mode):
                raise ValueError("workspace_instance_registry_invalid")
            elif metadata.st_nlink != 1:
                self._raise_root_unsafe()

    @staticmethod
    def _validate_registry(registry: object) -> None:
        if (
            type(registry) is not dict
            or frozenset(registry) != _REGISTRY_KEYS
            or type(registry["version"]) is not int
            or registry["version"] != _REGISTRY_VERSION
            or type(registry["instances"]) is not list
            or len(registry["instances"]) > _MAXIMUM_REGISTRY_INSTANCES
        ):
            raise ValueError("workspace_instance_registry_invalid")

        registry_keys: set[tuple[str, str]] = set()
        instance_ids: set[str] = set()
        for entry in registry["instances"]:
            if type(entry) is not dict or frozenset(entry) != _ENTRY_KEYS:
                raise ValueError("workspace_instance_registry_invalid")
            values = tuple(entry[key] for key in _ENTRY_KEYS)
            if any(type(value) is not str or not value for value in values):
                raise ValueError("workspace_instance_registry_invalid")

            instance_id = entry["instance_id"]
            workspace_identity = entry["workspace_identity"]
            canonical_root = entry["canonical_root"]
            normalized_root = os.path.normcase(os.path.normpath(canonical_root))
            registry_key = (workspace_identity, canonical_root)
            if (
                _INSTANCE_ID_PATTERN.fullmatch(instance_id) is None
                or not Path(canonical_root).is_absolute()
                or canonical_root != normalized_root
                or registry_key in registry_keys
                or instance_id in instance_ids
            ):
                raise ValueError("workspace_instance_registry_invalid")
            registry_keys.add(registry_key)
            instance_ids.add(instance_id)

    @staticmethod
    def _find_instance(
        instances: list[dict[str, str]],
        workspace_identity: str,
        canonical_root: Path,
    ) -> dict[str, str] | None:
        normalized_root = os.path.normcase(str(canonical_root))
        for instance in instances:
            if (
                instance.get("workspace_identity") == workspace_identity
                and os.path.normcase(instance.get("canonical_root", ""))
                == normalized_root
            ):
                return instance
        return None

    def _write_registry(self, registry: dict[str, object]) -> None:
        temporary_path: Path | None = None
        payload = (
            json.dumps(
                registry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > _MAXIMUM_REGISTRY_BYTES:
            raise ValueError("workspace_instance_registry_full")
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                prefix=f"{self._registry_path.name}.",
                suffix=".tmp",
                dir=self._app_root,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self._registry_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _to_workspace_instance(
        self,
        instance: dict[str, str],
        machine_parent: Path,
    ) -> WorkspaceInstance:
        instance_id = instance["instance_id"]
        try:
            machine_root = (machine_parent / instance_id).resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            self._raise_root_unsafe(error)
        if (
            machine_root.parent != machine_parent
            or machine_root.name != instance_id
            or not machine_root.is_relative_to(self._trusted_local_root)
        ):
            self._raise_root_unsafe()
        return WorkspaceInstance(
            instance_id=instance_id,
            workspace_identity=instance["workspace_identity"],
            canonical_root=Path(instance["canonical_root"]),
            machine_root=machine_root,
        )
