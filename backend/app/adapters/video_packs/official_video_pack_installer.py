"""Secure logical activation for the two fixed official Video Packs."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from filelock import Timeout

from app.adapters.documents.document_basic_pack_installer import (
    _PackFileLock,
    _cleanup_orphans,
    _lexically_exists,
    _materialize_source,
    _ordinary_directory,
    _ordinary_file,
    _remove_stage,
    _sync_directory,
    _validate_source_location,
)
from app.adapters.documents.document_basic_pack_verifier import (
    _is_link_or_reparse,
    _read_regular_file,
)
from app.adapters.video_packs.official_video_pack import OfficialVideoPackContract
from app.adapters.video_packs.official_video_pack_verifier import (
    PackTrustKey,
    VerifiedOfficialVideoPack,
    canonical_receipt_bytes,
    verify_official_video_pack_generation,
    verify_official_video_pack_source,
)
from app.core.errors import DomainError, ErrorCategory
from app.runtime_paths import RuntimePaths


_LOCK_TIMEOUT_SECONDS = 30
_CONTROL_FILE_LIMIT = 16 * 1024
_ACTIVE_KEYS = frozenset(
    {"schema_version", "pack_id", "pack_version", "manifest_sha256"}
)
_SHA256_PATTERN = re.compile(r"sha256:([0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class OfficialVideoPackInstallResult:
    pack_id: str
    pack_version: str
    manifest_sha256: str
    generation: Path
    active_pointer: Path
    result: str


@dataclass(frozen=True, slots=True)
class _ActiveState:
    kind: str
    digest: str | None = None
    raw: bytes | None = None


def _install_conflict(contract: OfficialVideoPackContract) -> DomainError:
    return DomainError(
        "pack_install_conflict",
        ErrorCategory.CONFLICT,
        f"The {contract.pack_id} Pack installation conflicts with managed state",
    )


def _install_failed(contract: OfficialVideoPackContract) -> DomainError:
    return DomainError(
        "pack_install_failed",
        ErrorCategory.RETRYABLE_RUNTIME,
        f"The {contract.pack_id} Pack could not be installed",
    )


def _ensure_managed_roots(
    paths: RuntimePaths,
    contract: OfficialVideoPackContract,
) -> tuple[Path, Path]:
    pack_root = paths.data_dir / "packs" / contract.pack_id / contract.pack_version
    installs_root = pack_root / "installs"
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        if not _ordinary_directory(paths.data_dir):
            raise _install_conflict(contract)
        current = paths.data_dir
        for part in (
            "packs",
            contract.pack_id,
            contract.pack_version,
            "installs",
        ):
            current /= part
            current.mkdir(exist_ok=True)
            if not _ordinary_directory(current):
                raise _install_conflict(contract)
    except DomainError:
        raise
    except OSError as error:
        raise _install_failed(contract) from error
    return pack_root, installs_root


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _read_active_state(
    path: Path,
    contract: OfficialVideoPackContract,
) -> _ActiveState:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _ActiveState("absent")
    except OSError:
        return _ActiveState("unsafe")
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > _CONTROL_FILE_LIMIT
    ):
        return _ActiveState("unsafe")
    raw: bytes | None = None
    try:
        raw = _read_regular_file(path, _CONTROL_FILE_LIMIT)
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non_finite_json")
            ),
        )
    except (OSError, UnicodeError, ValueError):
        return _ActiveState("malformed", raw=raw)
    if type(payload) is not dict or frozenset(payload) != _ACTIVE_KEYS:
        return _ActiveState("malformed", raw=raw)
    manifest_sha256 = payload.get("manifest_sha256")
    match = (
        _SHA256_PATTERN.fullmatch(manifest_sha256)
        if type(manifest_sha256) is str
        else None
    )
    if (
        payload.get("schema_version") != 1
        or payload.get("pack_id") != contract.pack_id
        or payload.get("pack_version") != contract.pack_version
        or match is None
    ):
        return _ActiveState("malformed", raw=raw)
    return _ActiveState("valid", digest=match.group(1), raw=raw)


def _control_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_receipt(
    stage: Path,
    *,
    contract: OfficialVideoPackContract,
    manifest_sha256: str,
) -> None:
    with (stage / "receipt.json").open("xb") as stream:
        stream.write(canonical_receipt_bytes(contract, manifest_sha256))
        stream.flush()
        os.fsync(stream.fileno())


def _write_active_pointer(
    pack_root: Path,
    active_path: Path,
    *,
    contract: OfficialVideoPackContract,
    manifest_sha256: str,
    expected_state: _ActiveState,
) -> None:
    if _read_active_state(active_path, contract) != expected_state:
        raise _install_conflict(contract)
    temporary = pack_root / f".active-{uuid4().hex}.tmp"
    payload = _control_bytes(
        {
            "schema_version": 1,
            "pack_id": contract.pack_id,
            "pack_version": contract.pack_version,
            "manifest_sha256": manifest_sha256,
        }
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, active_path)
        try:
            _sync_directory(pack_root)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verified_existing_generation(
    generation: Path,
    *,
    contract: OfficialVideoPackContract,
    manifest_sha256: str,
    trusted_keys: Mapping[str, PackTrustKey],
    platform_tag: str | None,
) -> bool:
    if not _lexically_exists(generation):
        return False
    if not _ordinary_directory(generation):
        raise _install_conflict(contract)
    try:
        verified = verify_official_video_pack_generation(
            generation,
            contract=contract,
            trusted_keys=trusted_keys,
            platform_tag=platform_tag,
        )
    except DomainError as error:
        raise _install_conflict(contract) from error
    if verified.manifest_sha256 != manifest_sha256:
        raise _install_conflict(contract)
    return True


def install_official_video_pack(
    source: Path,
    *,
    contract: OfficialVideoPackContract,
    paths: RuntimePaths,
    trusted_keys: Mapping[str, PackTrustKey],
    probe: Callable[[VerifiedOfficialVideoPack, Path], None],
    repair: bool = False,
    environ: Mapping[str, str] | None = None,
    platform_tag: str | None = None,
) -> OfficialVideoPackInstallResult:
    environment = os.environ if environ is None else environ
    override_name = f"ALLTONOTE_{contract.pack_id.replace('-', '_').upper()}_ROOT"
    if environment.get(override_name):
        raise DomainError(
            "pack_override_active",
            ErrorCategory.CONFLICT,
            f"Managed {contract.pack_id} Pack installation is blocked by an active override",
        )

    pack_root = paths.data_dir / "packs" / contract.pack_id / contract.pack_version
    source_root = _validate_source_location(Path(source), pack_root)
    pack_root, installs_root = _ensure_managed_roots(paths, contract)
    lock_path = pack_root / "install.lock"
    try:
        with _PackFileLock(lock_path, timeout=_LOCK_TIMEOUT_SECONDS):
            pack_root, installs_root = _ensure_managed_roots(paths, contract)
            if not _ordinary_file(lock_path):
                raise _install_conflict(contract)
            _cleanup_orphans(pack_root, installs_root)
            stage = installs_root / f".stage-{uuid4().hex}"
            stage.mkdir()
            if not _ordinary_directory(stage):
                raise _install_conflict(contract)
            try:
                _materialize_source(source_root, stage)
                verified = verify_official_video_pack_source(
                    stage,
                    contract=contract,
                    trusted_keys=trusted_keys,
                    platform_tag=platform_tag,
                )
                probe(verified, stage)
                _write_receipt(
                    stage,
                    contract=contract,
                    manifest_sha256=verified.manifest_sha256,
                )
                verify_official_video_pack_generation(
                    stage,
                    contract=contract,
                    trusted_keys=trusted_keys,
                    platform_tag=platform_tag,
                )

                digest = verified.manifest_sha256.removeprefix("sha256:")
                generation = installs_root / digest
                active_path = pack_root / "active.json"
                active = _read_active_state(active_path, contract)
                if active.kind == "unsafe":
                    raise _install_conflict(contract)
                if active.kind == "malformed" and not repair:
                    raise _install_conflict(contract)
                if active.kind == "valid" and active.digest != digest:
                    raise _install_conflict(contract)

                generation_exists = _verified_existing_generation(
                    generation,
                    contract=contract,
                    manifest_sha256=verified.manifest_sha256,
                    trusted_keys=trusted_keys,
                    platform_tag=platform_tag,
                )
                if active.kind == "valid" and not generation_exists and not repair:
                    raise _install_conflict(contract)
                if not generation_exists:
                    stage.rename(generation)
                    _sync_directory(installs_root)

                if active.kind == "absent":
                    _write_active_pointer(
                        pack_root,
                        active_path,
                        contract=contract,
                        manifest_sha256=verified.manifest_sha256,
                        expected_state=active,
                    )
                    result = "installed"
                elif active.kind == "malformed":
                    _write_active_pointer(
                        pack_root,
                        active_path,
                        contract=contract,
                        manifest_sha256=verified.manifest_sha256,
                        expected_state=active,
                    )
                    result = "repaired"
                elif generation_exists:
                    result = "already_active"
                else:
                    result = "repaired"
                return OfficialVideoPackInstallResult(
                    pack_id=contract.pack_id,
                    pack_version=contract.pack_version,
                    manifest_sha256=verified.manifest_sha256,
                    generation=generation,
                    active_pointer=active_path,
                    result=result,
                )
            finally:
                _remove_stage(stage, installs_root)
    except Timeout as error:
        raise DomainError(
            "pack_install_busy",
            ErrorCategory.RETRYABLE_RUNTIME,
            f"Another {contract.pack_id} Pack installation is in progress",
        ) from error
    except DomainError:
        raise
    except OSError as error:
        raise _install_failed(contract) from error


__all__ = [
    "OfficialVideoPackInstallResult",
    "install_official_video_pack",
]
