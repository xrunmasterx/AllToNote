from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from app.cli.contracts import ApplicationResult
from app.core.errors import DomainError, ErrorCategory


class PackService(Protocol):
    def doctor(self, *, dynamic: bool) -> Mapping[str, object]: ...

    def install(self, source: Path, *, repair: bool) -> Mapping[str, object]: ...


class _DefaultPackService:
    def doctor(self, *, dynamic: bool) -> Mapping[str, object]:
        from app.adapters.documents.document_basic_pack import (
            PACK_ID,
            PACK_VERSION,
            _lexically_exists,
            _read_control_file,
            resolve_document_basic_pack_paths,
        )
        from app.runtime_paths import resolve_runtime_paths

        paths = resolve_runtime_paths()
        environment = os.environ
        pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
        active_path = pack_root / "active.json"
        python_override = environment.get("ALLTONOTE_DOCUMENT_BASIC_PYTHON")
        artifacts_override = environment.get("ALLTONOTE_DOCUMENT_BASIC_ARTIFACTS")
        override_present = bool(python_override) or bool(artifacts_override)
        active_present = _lexically_exists(active_path)
        resolved = resolve_document_basic_pack_paths(paths, environment)
        installed = bool(
            resolved is not None
            and resolved[0].is_file()
            and resolved[1].is_dir()
        )
        manifest_sha256: str | None = None

        if override_present:
            static_status = "warn" if installed else "fail"
            static_action = (
                "Use the paired document-basic override or remove both override values"
                if installed
                else "Configure both document-basic override values or remove the partial override"
            )
        elif active_present:
            pointer = _read_control_file(active_path)
            if pointer is not None and type(pointer.get("manifest_sha256")) is str:
                manifest_sha256 = pointer["manifest_sha256"]
            static_status = "pass" if installed else "fail"
            static_action = (
                "No action required"
                if installed
                else "Repair or reinstall the managed document-basic Pack"
            )
        else:
            static_status = "warn" if installed else "fail"
            static_action = (
                "Install the managed document-basic Pack when convenient"
                if installed
                else "Install the document-basic Pack from an official signed source"
            )

        checks: list[dict[str, object]] = [
            {
                "code": "pack.document-basic.static",
                "status": static_status,
                "action": static_action,
                "dynamic": False,
            }
        ]
        if dynamic and static_status != "fail" and resolved is not None:
            try:
                self._dynamic_doctor(*resolved)
            except DomainError:
                checks.append(
                    {
                        "code": "pack.document-basic.dynamic",
                        "status": "fail",
                        "action": "Repair or reinstall the document-basic Pack",
                        "dynamic": True,
                    }
                )
            else:
                checks.append(
                    {
                        "code": "pack.document-basic.dynamic",
                        "status": "pass",
                        "action": "No action required",
                        "dynamic": True,
                    }
                )
        healthy = all(check["status"] != "fail" for check in checks)
        return {
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "installed": installed,
            "healthy": healthy,
            "dynamic": dynamic,
            "manifest_sha256": manifest_sha256,
            "checks": tuple(checks),
        }

    def install(self, source: Path, *, repair: bool) -> Mapping[str, object]:
        from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
        from app.adapters.documents.document_basic_pack_installer import (
            install_document_basic_pack,
        )
        from app.adapters.documents.document_basic_pack_trust import (
            official_document_pack_trust_keys,
        )
        from app.runtime_paths import resolve_runtime_paths

        trusted_keys = official_document_pack_trust_keys()
        if not trusted_keys:
            raise DomainError(
                "pack_trust_unconfigured",
                ErrorCategory.POLICY_DENIED,
                "No official document-basic Pack signing key is configured",
            )
        installed = install_document_basic_pack(
            source,
            paths=resolve_runtime_paths(),
            trusted_keys=trusted_keys,
            probe=self._dynamic_doctor,
            repair=repair,
            environ=os.environ,
        )
        return {
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "manifest_sha256": installed.manifest_sha256,
            "result": installed.result,
        }

    @staticmethod
    def _dynamic_doctor(python_executable: Path, artifacts_path: Path) -> None:
        from app.adapters.documents.docling_worker_parser import (
            DoclingWorkerConfig,
            DoclingWorkerParser,
        )

        backend_root = Path(__file__).resolve().parents[2]
        DoclingWorkerParser(
            DoclingWorkerConfig(
                python_executable=python_executable,
                artifacts_path=artifacts_path,
                backend_root=backend_root,
            )
        ).doctor()


def pack_command_result(
    args: object,
    correlation_id: str,
    *,
    service: PackService | None = None,
    versions: Mapping[str, object] | None = None,
) -> ApplicationResult:
    active_service = service or _DefaultPackService()
    command_name = str(getattr(args, "pack_command"))
    command = f"pack {command_name}"
    if command_name == "doctor":
        raw = active_service.doctor(dynamic=bool(getattr(args, "dynamic")))
        data = {
            key: raw.get(key)
            for key in (
                "pack_id",
                "pack_version",
                "installed",
                "healthy",
                "dynamic",
                "manifest_sha256",
                "checks",
            )
        }
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data=data,
            versions=versions or {},
            human_lines=(
                f"document-basic healthy: {'yes' if data['healthy'] is True else 'no'}",
            ),
        )

    raw = active_service.install(
        Path(getattr(args, "source")),
        repair=bool(getattr(args, "repair")),
    )
    data = {
        key: raw.get(key)
        for key in ("pack_id", "pack_version", "manifest_sha256", "result")
    }
    labels = {
        "installed": "installed",
        "already_active": "already installed",
        "repaired": "repaired",
    }
    result = str(data["result"])
    return ApplicationResult(
        command=command,
        correlation_id=correlation_id,
        ok=True,
        data=data,
        versions=versions or {},
        human_lines=(
            f"document-basic {data['pack_version']}: {labels.get(result, result)}",
        ),
    )


__all__ = ["PackService", "pack_command_result"]
