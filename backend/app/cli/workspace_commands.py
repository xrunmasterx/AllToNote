from __future__ import annotations

from pathlib import Path

from app.cli.contracts import ApplicationResult
from app.runtime_config import RuntimeConfigService
from app.workspace_initializer import initialize_workspace


def workspace_init_result(
    root: Path,
    name: str,
    *,
    set_default: bool,
    correlation_id: str,
    config_service: RuntimeConfigService | None,
) -> ApplicationResult:
    active_config_service = config_service or RuntimeConfigService()
    active_config_service.paths.assert_workspace_location(root)
    initialized = initialize_workspace(
        root,
        name,
        lock_root=active_config_service.paths.state_dir / "locks",
    )
    if set_default:
        active_config_service.set_value(
            "default_workspace",
            str(Path(root).expanduser().resolve(strict=False)),
        )
    state = "initialized" if initialized.created else "already initialized"
    lines = [f"Workspace {state}: {initialized.name}"]
    if set_default:
        lines.append("Workspace set as the default")
    return ApplicationResult(
        command="workspace init",
        correlation_id=correlation_id,
        ok=True,
        data={
            "workspace_id": initialized.workspace_id,
            "name": initialized.name,
            "schema_version": initialized.schema_version,
            "created": initialized.created,
            "default_set": set_default,
        },
        human_lines=tuple(lines),
    )


__all__ = ["workspace_init_result"]
