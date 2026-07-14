from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from filelock import FileLock


_REGISTRY_VERSION = 1


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
        self._app_root = Path(local_app_data) / "AllToNote"
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
        self._app_root.mkdir(parents=True, exist_ok=True)
        with FileLock(self._lock_path):
            registry = self._read_registry()
            instance = self._find_instance(
                registry["instances"], workspace_identity, normalized_root
            )
            if instance is None:
                instance = {
                    "instance_id": uuid4().hex,
                    "workspace_identity": workspace_identity,
                    "canonical_root": str(normalized_root),
                }
                registry["instances"].append(instance)
                registry["instances"].sort(
                    key=lambda item: (
                        item["workspace_identity"],
                        os.path.normcase(item["canonical_root"]),
                    )
                )
                self._write_registry(registry)

        return self._to_workspace_instance(instance)

    def _read_registry(self) -> dict[str, object]:
        if not self._registry_path.exists():
            return {"version": _REGISTRY_VERSION, "instances": []}
        with self._registry_path.open("r", encoding="utf-8") as registry_file:
            registry = json.load(registry_file)
        if (
            not isinstance(registry, dict)
            or registry.get("version") != _REGISTRY_VERSION
            or not isinstance(registry.get("instances"), list)
        ):
            raise ValueError("workspace_instance_registry_invalid")
        return registry

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
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                prefix=f"{self._registry_path.name}.",
                suffix=".tmp",
                dir=self._app_root,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(
                    registry,
                    temporary_file,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self._registry_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _to_workspace_instance(
        self, instance: dict[str, str]
    ) -> WorkspaceInstance:
        instance_id = instance["instance_id"]
        return WorkspaceInstance(
            instance_id=instance_id,
            workspace_identity=instance["workspace_identity"],
            canonical_root=Path(instance["canonical_root"]),
            machine_root=self._app_root / "workspaces" / instance_id,
        )
