from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.adapters.pack_layout import (
    legacy_generation_root,
    managed_generation_root,
    managed_pack_root,
)
from app.adapters.video_packs.official_video_pack import OfficialVideoPackContract
from app.adapters.video_packs.official_video_pack_installer import (
    _lexically_exists,
    _read_active_state,
)
from app.adapters.video_packs.official_video_pack_verifier import (
    PackTrustKey,
    verify_official_video_pack_generation,
)
from app.core.errors import DomainError, ErrorCategory
from app.runtime_paths import RuntimePaths


@dataclass(frozen=True, slots=True)
class ResolvedOfficialVideoPack:
    pack_id: str
    pack_version: str
    platform: str
    manifest_sha256: str
    generation: Path
    entrypoints: Mapping[str, Path]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entrypoints",
            MappingProxyType(dict(self.entrypoints)),
        )


class OfficialVideoPackResolver:
    """Resolve only fixed, installed generations and cache a completed verification."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        trusted_keys: Mapping[str, PackTrustKey],
        platform_tag: str | None = None,
    ) -> None:
        self._paths = paths
        self._trusted_keys = dict(trusted_keys)
        self._platform_tag = platform_tag
        self._cache: dict[
            tuple[str, str, str],
            ResolvedOfficialVideoPack,
        ] = {}
        self._lock = threading.RLock()

    def resolve_active(
        self,
        contract: OfficialVideoPackContract,
    ) -> ResolvedOfficialVideoPack:
        pack_root = managed_pack_root(
            self._paths.data_dir, contract.pack_id, contract.pack_version
        )
        state = _read_active_state(pack_root / "active.json", contract)
        if state.kind == "absent":
            raise DomainError(
                "pack_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The {contract.pack_id} Pack is not installed",
            )
        if state.kind != "valid" or state.digest is None:
            raise DomainError(
                "pack_active_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The {contract.pack_id} Pack active pointer is invalid",
            )
        try:
            return self.resolve_exact(contract, f"sha256:{state.digest}")
        except DomainError as error:
            raise DomainError(
                "pack_active_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The {contract.pack_id} Pack active generation is invalid",
            ) from error

    def resolve_exact(
        self,
        contract: OfficialVideoPackContract,
        manifest_sha256: str,
    ) -> ResolvedOfficialVideoPack:
        if (
            type(manifest_sha256) is not str
            or len(manifest_sha256) != 71
            or not manifest_sha256.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in manifest_sha256[7:]
            )
        ):
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The exact {contract.pack_id} Pack generation is unavailable",
            )
        key = (contract.pack_id, contract.pack_version, manifest_sha256)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            resolved = self._verify_generation(contract, manifest_sha256)
            self._cache[key] = resolved
            return resolved

    def _verify_generation(
        self,
        contract: OfficialVideoPackContract,
        manifest_sha256: str,
    ) -> ResolvedOfficialVideoPack:
        installs_root = managed_generation_root(
            self._paths.data_dir, contract.pack_id
        )
        generation = installs_root / manifest_sha256.removeprefix("sha256:")
        if not _lexically_exists(generation):
            installs_root = legacy_generation_root(
                self._paths.data_dir,
                contract.pack_id,
                contract.pack_version,
            )
            generation = installs_root / manifest_sha256.removeprefix("sha256:")
        try:
            resolved_data = self._paths.data_dir.resolve(strict=True)
            resolved_installs = installs_root.resolve(strict=True)
            resolved_generation = generation.resolve(strict=True)
        except OSError as error:
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The exact {contract.pack_id} Pack generation is unavailable",
            ) from error
        if (
            not resolved_installs.is_relative_to(resolved_data)
            or not resolved_generation.is_relative_to(resolved_installs)
            or not resolved_generation.is_dir()
        ):
            raise DomainError(
                "pack_generation_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The exact {contract.pack_id} Pack generation is unavailable",
            )
        try:
            verified = verify_official_video_pack_generation(
                resolved_generation,
                contract=contract,
                trusted_keys=self._trusted_keys,
                platform_tag=self._platform_tag,
            )
        except DomainError as error:
            raise DomainError(
                "pack_generation_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The exact {contract.pack_id} Pack generation is invalid",
            ) from error
        if verified.manifest_sha256 != manifest_sha256:
            raise DomainError(
                "pack_generation_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The exact {contract.pack_id} Pack generation is invalid",
            )
        entrypoints: dict[str, Path] = {}
        try:
            for name, relative_path in verified.entrypoints.items():
                target = resolved_generation.joinpath(
                    *relative_path.split("/")
                ).resolve(strict=True)
                if (
                    not target.is_relative_to(resolved_generation)
                    or not target.is_file()
                ):
                    raise OSError("invalid_entrypoint")
                entrypoints[name] = target
        except OSError as error:
            raise DomainError(
                "pack_generation_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                f"The exact {contract.pack_id} Pack generation is invalid",
            ) from error
        return ResolvedOfficialVideoPack(
            pack_id=contract.pack_id,
            pack_version=contract.pack_version,
            platform=verified.platform,
            manifest_sha256=manifest_sha256,
            generation=resolved_generation,
            entrypoints=entrypoints,
        )


__all__ = [
    "OfficialVideoPackResolver",
    "ResolvedOfficialVideoPack",
]
