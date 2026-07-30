from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import (
    user_cache_path,
    user_config_path,
    user_data_path,
    user_log_path,
    user_state_path,
)

from app.core.errors import DomainError, ErrorCategory


APP_NAME = "AllToNote"
_ROLE_NAMES = ("config", "data", "cache", "state", "log")


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved machine-local roots shared by every Runtime adapter."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    state_dir: Path
    log_dir: Path

    def __post_init__(self) -> None:
        for field_name in (
            "config_dir",
            "data_dir",
            "cache_dir",
            "state_dir",
            "log_dir",
        ):
            path = Path(getattr(self, field_name)).resolve(strict=False)
            if not path.is_absolute():
                raise ValueError("runtime_path_not_absolute")
            object.__setattr__(self, field_name, path)
        if self.data_dir.name != APP_NAME:
            raise ValueError("runtime_data_dir_invalid")

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def credential_catalog_file(self) -> Path:
        return self.config_dir / "credential-profiles.toml"

    @property
    def workspace_registry_parent(self) -> Path:
        """Parent accepted by the compatibility-preserved registry adapter."""

        return self.data_dir.parent

    @property
    def workspace_registry_file(self) -> Path:
        return self.data_dir / "workspace-instances.json"

    @property
    def workspace_machine_parent(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def pack_registry_file(self) -> Path:
        return self.data_dir / "packs" / "installed.json"

    def role_records(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {"role": role, "path": str(getattr(self, f"{role}_dir"))}
            for role in _ROLE_NAMES
        )

    def assert_outside_workspace(self, workspace_root: Path) -> None:
        try:
            workspace = Path(workspace_root).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise DomainError(
                "workspace_root_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Workspace root is invalid",
            ) from error
        if not workspace.is_dir():
            raise DomainError(
                "workspace_root_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Workspace root is invalid",
            )

        self.assert_workspace_location(workspace)

    def assert_workspace_location(self, workspace_root: Path) -> None:
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            raise DomainError(
                "workspace_root_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Workspace root is invalid",
            ) from error

        for record in self.role_records():
            machine_root = Path(record["path"]).resolve(strict=False)
            if (
                machine_root == workspace
                or machine_root.is_relative_to(workspace)
                or workspace.is_relative_to(machine_root)
            ):
                raise DomainError(
                    "runtime_state_inside_workspace",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Runtime machine state must be outside the Workspace",
                    {"role": record["role"]},
                )


def resolve_runtime_paths(
    *,
    machine_state_root: Path | None = None,
    local_data_parent: Path | None = None,
) -> RuntimePaths:
    """Resolve paths without creating, migrating, or deleting any directory."""

    if machine_state_root is not None and local_data_parent is not None:
        raise ValueError("runtime_path_override_conflict")
    if local_data_parent is not None:
        data_dir = Path(local_data_parent).resolve(strict=False) / APP_NAME
        return RuntimePaths(
            config_dir=data_dir,
            data_dir=data_dir,
            cache_dir=data_dir / "Cache",
            state_dir=data_dir / "State",
            log_dir=data_dir / "Logs",
        )
    if machine_state_root is not None:
        root = Path(machine_state_root).resolve(strict=False)
        return RuntimePaths(
            config_dir=root / "config" / APP_NAME,
            data_dir=root / "data" / APP_NAME,
            cache_dir=root / "cache" / APP_NAME,
            state_dir=root / "state" / APP_NAME,
            log_dir=root / "log" / APP_NAME,
        )

    config_dir = Path(user_config_path(APP_NAME, appauthor=False))
    data_dir = Path(user_data_path(APP_NAME, appauthor=False))
    cache_dir = Path(user_cache_path(APP_NAME, appauthor=False))
    state_dir = Path(user_state_path(APP_NAME, appauthor=False))
    log_dir = Path(user_log_path(APP_NAME, appauthor=False))
    if state_dir in {config_dir, data_dir}:
        state_dir /= "State"
    return RuntimePaths(
        config_dir=config_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
        state_dir=state_dir,
        log_dir=log_dir,
    )


__all__ = ["APP_NAME", "RuntimePaths", "resolve_runtime_paths"]
