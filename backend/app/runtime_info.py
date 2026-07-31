from __future__ import annotations

import os
import platform
import shutil
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable

from app.adapters.documents.document_basic_pack import (
    PACK_ID,
    PACK_VERSION,
    document_basic_pack_installed,
)
from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    TRANSCRIBE_CPU,
    official_video_pack_installed,
)
from app.cli.contracts import CLI_PROTOCOL_VERSION
from app.core.errors import DomainError
from app.runtime_capabilities import CapabilityRegistry, RuntimeCapability
from app.runtime_lock import RuntimeLock, load_runtime_lock
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


RUNTIME_VERSION = "0.1.0"
CORE_API_VERSION = 1
CLI_API_VERSION = CLI_PROTOCOL_VERSION


@dataclass(frozen=True)
class RuntimePack:
    pack_id: str
    version: str
    installed: bool
    probe: str = "static"

    def to_mapping(self) -> dict[str, object]:
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "installed": self.installed,
            "probe": self.probe,
        }


@dataclass(frozen=True)
class RuntimeInfo:
    runtime_version: str
    core_api_version: int
    cli_api_version: int
    desktop_api_versions: tuple[int, ...]
    runtime_lock: RuntimeLock
    operating_system: str
    architecture: str
    sqlite_version: str
    sqlite_parallel_jobs_supported: bool
    engine_supported: bool
    engine_running: bool
    capabilities: tuple[RuntimeCapability, ...]
    packs: tuple[RuntimePack, ...]

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
            "storage": {
                "sqlite_version": self.sqlite_version,
                "parallel_job_execution_supported": (
                    self.sqlite_parallel_jobs_supported
                ),
            },
            "engine": {
                "supported": self.engine_supported,
                "running": self.engine_running,
            },
            "packs": tuple(pack.to_mapping() for pack in self.packs),
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


def _sqlite_parallel_jobs_supported(version: str) -> bool:
    try:
        parts = tuple(int(part) for part in version.split("."))
    except (AttributeError, ValueError):
        return False
    if len(parts) != 3 or parts[0] != 3:
        return False
    _major, minor, patch = parts
    return (
        (minor == 44 and patch >= 6)
        or (minor == 50 and patch >= 7)
        or (minor == 51 and patch >= 3)
    )


def build_runtime_info(
    *,
    registry: CapabilityRegistry | None = None,
    lock_loader: Callable[[], RuntimeLock] = load_runtime_lock,
    paths: RuntimePaths | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeInfo:
    runtime_lock = lock_loader()
    operating_system, architecture = _normalized_platform()
    active_paths = paths or resolve_runtime_paths()
    active_environ = os.environ if environ is None else environ
    media_basic_installed = official_video_pack_installed(
        active_paths.data_dir, MEDIA_BASIC
    )
    transcribe_cpu_installed = official_video_pack_installed(
        active_paths.data_dir, TRANSCRIBE_CPU
    )
    capabilities = _capabilities_with_video_packs(
        (registry or CapabilityRegistry()).snapshot(),
        media_basic_installed=media_basic_installed,
        transcribe_cpu_installed=transcribe_cpu_installed,
    )
    return RuntimeInfo(
        runtime_version=RUNTIME_VERSION,
        core_api_version=CORE_API_VERSION,
        cli_api_version=CLI_API_VERSION,
        desktop_api_versions=(),
        runtime_lock=runtime_lock,
        operating_system=operating_system,
        architecture=architecture,
        sqlite_version=sqlite3.sqlite_version,
        sqlite_parallel_jobs_supported=_sqlite_parallel_jobs_supported(
            sqlite3.sqlite_version
        ),
        engine_supported=False,
        engine_running=False,
        capabilities=capabilities,
        packs=(
            RuntimePack(
                PACK_ID,
                PACK_VERSION,
                document_basic_pack_installed(active_paths, active_environ),
            ),
            RuntimePack(
                MEDIA_BASIC.pack_id,
                MEDIA_BASIC.pack_version,
                media_basic_installed,
            ),
            RuntimePack(
                TRANSCRIBE_CPU.pack_id,
                TRANSCRIBE_CPU.pack_version,
                transcribe_cpu_installed,
            ),
        ),
    )


def _capabilities_with_video_packs(
    capabilities: tuple[RuntimeCapability, ...],
    *,
    media_basic_installed: bool,
    transcribe_cpu_installed: bool,
) -> tuple[RuntimeCapability, ...]:
    pack_capabilities = {
        "recipe.video.acquire.bilibili": media_basic_installed,
        "recipe.video.acquire.local": media_basic_installed,
        "recipe.video.transcribe.local.cpu": transcribe_cpu_installed,
    }
    return tuple(
        RuntimeCapability(
            key=capability.key,
            installed=(
                capability.installed
                or pack_capabilities.get(capability.key, False)
            ),
            version=capability.version,
            probe=capability.probe,
        )
        for capability in capabilities
    )


def runtime_doctor(
    *,
    dynamic: bool,
    paths: RuntimePaths | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[RuntimeCheck, ...]:
    checks: list[RuntimeCheck] = []
    active_paths = paths or resolve_runtime_paths()
    active_environ = os.environ if environ is None else environ
    media_basic_installed = official_video_pack_installed(
        active_paths.data_dir, MEDIA_BASIC
    )
    transcribe_cpu_installed = official_video_pack_installed(
        active_paths.data_dir, TRANSCRIBE_CPU
    )
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

    capabilities = _capabilities_with_video_packs(
        CapabilityRegistry().snapshot(),
        media_basic_installed=media_basic_installed,
        transcribe_cpu_installed=transcribe_cpu_installed,
    )
    for capability in capabilities:
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

    pack_installed = document_basic_pack_installed(active_paths, active_environ)
    checks.append(
        RuntimeCheck(
            "pack.document-basic",
            "pass" if pack_installed else "warn",
            (
                None
                if pack_installed
                else "Install or repair the compatible document-basic Pack"
            ),
            False,
        )
    )

    for contract, installed in (
        (MEDIA_BASIC, media_basic_installed),
        (TRANSCRIBE_CPU, transcribe_cpu_installed),
    ):
        checks.append(
            RuntimeCheck(
                f"pack.{contract.pack_id}",
                "pass" if installed else "warn",
                (
                    None
                    if installed
                    else f"Install or repair the compatible {contract.pack_id} Pack"
                ),
                False,
            )
        )

    sqlite_safe = _sqlite_parallel_jobs_supported(sqlite3.sqlite_version)
    checks.append(
        RuntimeCheck(
            "storage.sqlite.parallel-jobs",
            "pass" if sqlite_safe else "warn",
            (
                None
                if sqlite_safe
                else (
                    "Use an AllToNote Runtime validated with SQLite 3.44.6+, "
                    "3.50.7+, or 3.51.3+ on the same release line before "
                    "enabling parallel Job execution"
                )
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
    "RuntimePack",
    "build_runtime_info",
    "runtime_doctor",
]
