from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from typing import Callable

from app.cli.contracts import CLI_PROTOCOL_VERSION
from app.core.errors import DomainError
from app.runtime_capabilities import CapabilityRegistry, RuntimeCapability
from app.runtime_lock import RuntimeLock, load_runtime_lock


RUNTIME_VERSION = "0.1.0"
CORE_API_VERSION = 1
CLI_API_VERSION = CLI_PROTOCOL_VERSION


@dataclass(frozen=True)
class RuntimeInfo:
    runtime_version: str
    core_api_version: int
    cli_api_version: int
    desktop_api_versions: tuple[int, ...]
    runtime_lock: RuntimeLock
    operating_system: str
    architecture: str
    engine_supported: bool
    engine_running: bool
    capabilities: tuple[RuntimeCapability, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "runtime_version": self.runtime_version,
            "core_api_version": self.core_api_version,
            "cli_api_version": self.cli_api_version,
            "desktop_api_versions": self.desktop_api_versions,
            "portable_api_version": self.runtime_lock.portable_api_version,
            "iwiki_contract": {
                "package": self.runtime_lock.iwiki_distribution,
                "package_version": self.runtime_lock.iwiki_version,
                "contract_version": self.runtime_lock.portable_api_version,
                "contract_id": self.runtime_lock.portable_contract_id,
                "schema_id": self.runtime_lock.schema_set_id,
                "schema_hash": self.runtime_lock.schema_sha256,
            },
            "platform": {
                "os": self.operating_system,
                "arch": self.architecture,
            },
            "engine": {
                "supported": self.engine_supported,
                "running": self.engine_running,
            },
            "packs": (),
        }


@dataclass(frozen=True)
class RuntimeCheck:
    code: str
    status: str
    action: str | None
    dynamic: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "code": self.code,
            "status": self.status,
            "action": self.action,
            "dynamic": self.dynamic,
        }


def _normalized_platform() -> tuple[str, str]:
    system = platform.system().casefold()
    operating_system = {
        "darwin": "macos",
        "windows": "windows",
        "linux": "linux",
    }.get(system, system or "unknown")
    machine = platform.machine().casefold()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine or "unknown")
    return operating_system, architecture


def build_runtime_info(
    *,
    registry: CapabilityRegistry | None = None,
    lock_loader: Callable[[], RuntimeLock] = load_runtime_lock,
) -> RuntimeInfo:
    runtime_lock = lock_loader()
    operating_system, architecture = _normalized_platform()
    return RuntimeInfo(
        runtime_version=RUNTIME_VERSION,
        core_api_version=CORE_API_VERSION,
        cli_api_version=CLI_API_VERSION,
        desktop_api_versions=(),
        runtime_lock=runtime_lock,
        operating_system=operating_system,
        architecture=architecture,
        engine_supported=False,
        engine_running=False,
        capabilities=(registry or CapabilityRegistry()).snapshot(),
    )


def runtime_doctor(*, dynamic: bool) -> tuple[RuntimeCheck, ...]:
    checks: list[RuntimeCheck] = []
    try:
        load_runtime_lock()
        checks.append(RuntimeCheck("runtime.contract", "pass", None, False))
    except DomainError:
        checks.append(
            RuntimeCheck(
                "runtime.contract",
                "fail",
                "Repair or reinstall the compatible Runtime package",
                False,
            )
        )

    for capability in CapabilityRegistry().snapshot():
        checks.append(
            RuntimeCheck(
                f"capability.{capability.key}",
                "pass" if capability.installed else "warn",
                (
                    None
                    if capability.installed
                    else "Install the optional dependency or compatible Pack"
                ),
                False,
            )
        )

    if dynamic:
        checks.extend(_dynamic_checks())
    return tuple(checks)


def _dynamic_checks() -> tuple[RuntimeCheck, ...]:
    from app.services.codex_app_server import CodexAppServerStatusService

    codex_status = CodexAppServerStatusService.get_status()
    codex_ready = bool(codex_status.ready and codex_status.default_model)
    ffmpeg_ready = shutil.which("ffmpeg") is not None
    return (
        RuntimeCheck(
            "dynamic.model.codex-app-server",
            "pass" if codex_ready else "warn",
            None if codex_ready else "Install or sign in to the local Codex CLI",
            True,
        ),
        RuntimeCheck(
            "dynamic.tool.ffmpeg",
            "pass" if ffmpeg_ready else "warn",
            None if ffmpeg_ready else "Install or configure FFmpeg",
            True,
        ),
    )


__all__ = [
    "CLI_API_VERSION",
    "CORE_API_VERSION",
    "RUNTIME_VERSION",
    "RuntimeCheck",
    "RuntimeInfo",
    "build_runtime_info",
    "runtime_doctor",
]
