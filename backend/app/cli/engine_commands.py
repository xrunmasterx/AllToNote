from __future__ import annotations

from typing import Protocol

from app.cli.contracts import ApplicationResult
from app.engine.contracts import ENGINE_PROTOCOL_VERSION
from app.engine.instance import engine_lifecycle_supported
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


class EngineStatusView(Protocol):
    state: object
    running: bool
    engine_id: str | None
    started_at: str | None
    started: bool
    stopped: bool


class EngineClient(Protocol):
    def status(self) -> EngineStatusView: ...

    def ensure(self) -> EngineStatusView: ...

    def stop(self) -> EngineStatusView: ...


def engine_command_result(
    command_name: str,
    correlation_id: str,
    *,
    client: EngineClient | None = None,
    paths: RuntimePaths | None = None,
) -> ApplicationResult:
    active_client = client
    if active_client is None:
        from app.engine.client import LocalEngineClient

        active_client = LocalEngineClient(paths or resolve_runtime_paths())
    status = (
        active_client.status()
        if command_name == "status"
        else active_client.stop()
        if command_name == "stop"
        else active_client.ensure()
    )
    state = getattr(status.state, "value", status.state)
    data: dict[str, object] = {
        "supported": engine_lifecycle_supported(),
        "running": status.running,
        "state": state,
        "engine_protocol_version": ENGINE_PROTOCOL_VERSION,
        "engine_id": status.engine_id,
        "started_at": status.started_at,
    }
    if command_name in {"start", "ensure"}:
        data["started"] = status.started
    if command_name == "stop":
        data["stopped"] = status.stopped
    summary = (
        "Engine running"
        if status.running
        else "Engine stopped"
        if state == "stopped"
        else f"Engine {state}"
    )
    return ApplicationResult(
        command=f"engine {command_name}",
        correlation_id=correlation_id,
        ok=True,
        data=data,
        human_lines=(summary,),
    )


__all__ = ["EngineClient", "EngineStatusView", "engine_command_result"]
