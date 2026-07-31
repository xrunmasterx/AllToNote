from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.adapters.documents.document_basic_pack_verifier import (
    DocumentPackTrustKey as PackTrustKey,
    _is_link_or_reparse,
    _normalized_relative_path,
    _read_regular_file,
    _same_open_file,
    current_pack_platform,
)
from app.adapters.video_packs.official_video_pack import OfficialVideoPackContract
from app.core.errors import DomainError, ErrorCategory


_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "pack_id",
        "version",
        "platform",
        "runtime_api",
        "recipe_contracts",
        "capabilities",
        "entrypoints",
        "files",
        "licenses",
        "sbom",
        "publisher",
        "signature",
    }
)
_FILE_KEYS = frozenset({"path", "byte_length", "sha256"})
_SIGNATURE_KEYS = frozenset({"algorithm", "key_id", "value"})
_ENTRYPOINT_KEYS = frozenset({"name", "type", "relative_path"})
_LICENSE_KEYS = frozenset({"component", "spdx", "file"})
_SBOM_KEYS = frozenset({"format", "file", "sha256"})
_RUNTIME_KEYS = frozenset({"min", "max"})
_RECEIPT_KEYS = frozenset(
    {"schema_version", "pack_id", "pack_version", "manifest_sha256", "verified"}
)
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_FILE_COUNT = 100_000
_MAX_TREE_ENTRY_COUNT = 2 * _MAX_FILE_COUNT + 2
_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 6 * 1024 * 1024 * 1024
_SHA256_PREFIX = "sha256:"


@dataclass(frozen=True, slots=True)
class OfficialVideoPackFile:
    relative_path: str
    byte_length: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedOfficialVideoPack:
    source_root: Path
    pack_id: str
    pack_version: str
    platform: str
    publisher: str
    signature_key_id: str
    manifest_sha256: str
    entrypoints: Mapping[str, str]
    files: tuple[OfficialVideoPackFile, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entrypoints",
            MappingProxyType(dict(self.entrypoints)),
        )


def canonical_manifest_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _manifest_invalid() from error


def canonical_receipt_bytes(
    contract: OfficialVideoPackContract,
    manifest_sha256: str,
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "pack_id": contract.pack_id,
            "pack_version": contract.pack_version,
            "manifest_sha256": manifest_sha256,
            "verified": True,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest_invalid() -> DomainError:
    return DomainError(
        "pack_manifest_invalid",
        ErrorCategory.INVALID_REQUEST,
        "The official Video Pack manifest is invalid",
    )


def _reject_constant(_value: str) -> None:
    raise ValueError("non_finite_json")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _load_manifest(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        content = _read_regular_file(path, _MAX_MANIFEST_BYTES)
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise _manifest_invalid() from error
    if type(payload) is not dict or frozenset(payload) != _MANIFEST_KEYS:
        raise _manifest_invalid()
    if content != canonical_manifest_bytes(payload):
        raise _manifest_invalid()
    return payload, content


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == len(_SHA256_PREFIX) + 64
        and value.startswith(_SHA256_PREFIX)
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise _manifest_invalid()
    return value


def _manifest_files(payload: object) -> tuple[OfficialVideoPackFile, ...]:
    if type(payload) is not list or not payload or len(payload) > _MAX_FILE_COUNT:
        raise _manifest_invalid()
    files: list[OfficialVideoPackFile] = []
    collision_keys: set[str] = set()
    total_bytes = 0
    for item in payload:
        mapping = _exact_mapping(item, _FILE_KEYS)
        relative_path = _normalized_relative_path(mapping.get("path"))
        byte_length = mapping.get("byte_length")
        sha256 = mapping.get("sha256")
        if (
            type(byte_length) is not int
            or byte_length < 0
            or byte_length > _MAX_FILE_BYTES
            or not _valid_sha256(sha256)
        ):
            raise _manifest_invalid()
        collision_key = unicodedata.normalize("NFC", relative_path).casefold()
        if collision_key in collision_keys:
            raise _manifest_invalid()
        collision_keys.add(collision_key)
        total_bytes += byte_length
        if total_bytes > _MAX_TOTAL_BYTES:
            raise _manifest_invalid()
        files.append(
            OfficialVideoPackFile(relative_path, byte_length, sha256)
        )
    return tuple(files)


def _validate_contract(
    manifest: dict[str, object],
    files: tuple[OfficialVideoPackFile, ...],
    *,
    contract: OfficialVideoPackContract,
    expected_platform: str,
) -> dict[str, str]:
    if (
        type(manifest.get("manifest_version")) is not int
        or manifest.get("manifest_version") != 1
        or manifest.get("pack_id") != contract.pack_id
    ):
        raise _manifest_invalid()
    if manifest.get("version") != contract.pack_version:
        raise DomainError(
            "pack_version_unsupported",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            f"The {contract.pack_id} Pack version is not supported",
        )
    if manifest.get("platform") != expected_platform:
        raise DomainError(
            "pack_platform_incompatible",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            f"The {contract.pack_id} Pack platform is incompatible",
        )
    runtime_api = _exact_mapping(manifest.get("runtime_api"), _RUNTIME_KEYS)
    if (
        type(runtime_api.get("min")) is not int
        or type(runtime_api.get("max")) is not int
        or not runtime_api["min"] <= 1 <= runtime_api["max"]
    ):
        raise DomainError(
            "pack_runtime_incompatible",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            f"The {contract.pack_id} Pack Runtime API is incompatible",
        )
    expected_recipes = {
        recipe_id: list(versions)
        for recipe_id, versions in contract.recipe_contracts.items()
    }
    if manifest.get("recipe_contracts") != expected_recipes:
        raise DomainError(
            "pack_recipe_incompatible",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            f"The {contract.pack_id} Pack Recipe contract is incompatible",
        )
    if manifest.get("capabilities") != list(contract.capabilities):
        raise _manifest_invalid()

    expected_entrypoints = contract.entrypoints(expected_platform)
    entrypoints = manifest.get("entrypoints")
    if type(entrypoints) is not list or len(entrypoints) != len(expected_entrypoints):
        raise _manifest_invalid()
    parsed_entrypoints: dict[str, str] = {}
    for item in entrypoints:
        entrypoint = _exact_mapping(item, _ENTRYPOINT_KEYS)
        name = entrypoint.get("name")
        relative_path = _normalized_relative_path(entrypoint.get("relative_path"))
        if (
            type(name) is not str
            or not name
            or name in parsed_entrypoints
            or entrypoint.get("type") != "process"
        ):
            raise _manifest_invalid()
        parsed_entrypoints[name] = relative_path
    if parsed_entrypoints != expected_entrypoints:
        raise _manifest_invalid()

    file_paths = {item.relative_path: item for item in files}
    required_paths = {
        *expected_entrypoints.values(),
        *contract.required_payload_files,
    }
    if not required_paths <= file_paths.keys():
        raise _manifest_invalid()

    licenses = manifest.get("licenses")
    if type(licenses) is not list or not licenses:
        raise _manifest_invalid()
    for item in licenses:
        license_item = _exact_mapping(item, _LICENSE_KEYS)
        license_path = _normalized_relative_path(license_item.get("file"))
        if (
            type(license_item.get("component")) is not str
            or not license_item["component"]
            or type(license_item.get("spdx")) is not str
            or not license_item["spdx"]
            or license_path not in file_paths
        ):
            raise _manifest_invalid()

    sbom = _exact_mapping(manifest.get("sbom"), _SBOM_KEYS)
    sbom_path = _normalized_relative_path(sbom.get("file"))
    if (
        sbom.get("format") != "cyclonedx-json"
        or sbom_path not in file_paths
        or sbom.get("sha256") != file_paths[sbom_path].sha256
    ):
        raise _manifest_invalid()
    return parsed_entrypoints


def _verify_signature(
    manifest: dict[str, object],
    trusted_keys: Mapping[str, PackTrustKey],
) -> str:
    publisher = manifest.get("publisher")
    signature = _exact_mapping(manifest.get("signature"), _SIGNATURE_KEYS)
    key_id = signature.get("key_id")
    if (
        type(publisher) is not str
        or not publisher
        or signature.get("algorithm") != "ed25519"
        or type(key_id) is not str
        or type(signature.get("value")) is not str
    ):
        raise _manifest_invalid()
    key = trusted_keys.get(key_id)
    if key is None or key.publisher != publisher:
        raise DomainError(
            "pack_signature_untrusted",
            ErrorCategory.POLICY_DENIED,
            "The official Video Pack signing key is not trusted",
        )
    unsigned = dict(manifest)
    unsigned.pop("signature")
    try:
        value = base64.b64decode(signature["value"], validate=True)
        Ed25519PublicKey.from_public_bytes(key.public_key).verify(
            value,
            canonical_manifest_bytes(unsigned),
        )
    except (InvalidSignature, TypeError, ValueError) as error:
        raise DomainError(
            "pack_signature_invalid",
            ErrorCategory.POLICY_DENIED,
            "The official Video Pack signature is invalid",
        ) from error
    return key_id


def _source_files(root: Path) -> dict[str, Path]:
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The official Video Pack source is unavailable",
        ) from error
    if _is_link_or_reparse(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise DomainError(
            "pack_archive_unsafe",
            ErrorCategory.INVALID_REQUEST,
            "The official Video Pack source contains unsafe filesystem entries",
        )

    files: dict[str, Path] = {}
    collision_keys: set[str] = set()
    stack = [root]
    entry_count = 0
    total_bytes = 0
    try:
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > _MAX_TREE_ENTRY_COUNT:
                        raise OSError("too_many_entries")
                    path = Path(entry.path)
                    metadata = path.lstat()
                    if _is_link_or_reparse(metadata):
                        raise OSError("unsafe_entry")
                    if stat.S_ISDIR(metadata.st_mode):
                        stack.append(path)
                        continue
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise OSError("unsafe_entry")
                    total_bytes += metadata.st_size
                    if (
                        total_bytes
                        > _MAX_TOTAL_BYTES + _MAX_MANIFEST_BYTES + 16 * 1024
                    ):
                        raise OSError("source_total_limit")
                    relative_path = _normalized_relative_path(
                        path.relative_to(root).as_posix()
                    )
                    collision_key = unicodedata.normalize(
                        "NFC", relative_path
                    ).casefold()
                    if collision_key in collision_keys:
                        raise OSError("path_collision")
                    collision_keys.add(collision_key)
                    files[relative_path] = path
    except (DomainError, OSError, RuntimeError, ValueError) as error:
        if isinstance(error, DomainError):
            raise
        raise DomainError(
            "pack_archive_unsafe",
            ErrorCategory.INVALID_REQUEST,
            "The official Video Pack source contains unsafe filesystem entries",
        ) from error
    return files


def _sha256(path: Path, expected_size: int) -> str:
    metadata = path.lstat()
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != expected_size
    ):
        raise OSError("unsafe_file")
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _same_open_file(metadata, opened):
            raise OSError("file_changed_before_read")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            byte_count += len(chunk)
            digest.update(chunk)
        finished = os.fstat(stream.fileno())
    if byte_count != expected_size or not _same_open_file(opened, finished):
        raise OSError("file_changed_during_read")
    return _SHA256_PREFIX + digest.hexdigest()


def _validate_receipt(
    path: Path,
    *,
    contract: OfficialVideoPackContract,
    manifest_sha256: str,
) -> None:
    try:
        content = _read_regular_file(path, 16 * 1024)
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise _manifest_invalid() from error
    if (
        type(payload) is not dict
        or frozenset(payload) != _RECEIPT_KEYS
        or payload.get("schema_version") != 1
        or payload.get("pack_id") != contract.pack_id
        or payload.get("pack_version") != contract.pack_version
        or payload.get("manifest_sha256") != manifest_sha256
        or payload.get("verified") is not True
        or content != canonical_receipt_bytes(contract, manifest_sha256)
    ):
        raise _manifest_invalid()


def _verify_tree(
    source: Path,
    *,
    contract: OfficialVideoPackContract,
    trusted_keys: Mapping[str, PackTrustKey],
    platform_tag: str | None,
    receipt_required: bool,
) -> VerifiedOfficialVideoPack:
    try:
        root = Path(os.path.abspath(os.fspath(Path(source).expanduser())))
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The official Video Pack source is unavailable",
        ) from error
    source_files = _source_files(root)
    manifest, manifest_bytes = _load_manifest(root / "manifest.json")
    files = _manifest_files(manifest.get("files"))
    expected_platform = platform_tag or current_pack_platform()
    entrypoints = _validate_contract(
        manifest,
        files,
        contract=contract,
        expected_platform=expected_platform,
    )
    signature_key_id = _verify_signature(manifest, trusted_keys)
    manifest_sha256 = _SHA256_PREFIX + hashlib.sha256(manifest_bytes).hexdigest()

    expected_files = {item.relative_path for item in files} | {"manifest.json"}
    if receipt_required:
        expected_files.add("receipt.json")
    if set(source_files) != expected_files:
        raise _manifest_invalid()
    for item in files:
        try:
            if _sha256(source_files[item.relative_path], item.byte_length) != item.sha256:
                raise OSError("hash_mismatch")
        except OSError as error:
            raise DomainError(
                "pack_hash_mismatch",
                ErrorCategory.POLICY_DENIED,
                "The official Video Pack file could not be verified",
                {"component": item.relative_path},
            ) from error
    if receipt_required:
        _validate_receipt(
            source_files["receipt.json"],
            contract=contract,
            manifest_sha256=manifest_sha256,
        )
    return VerifiedOfficialVideoPack(
        source_root=root,
        pack_id=contract.pack_id,
        pack_version=contract.pack_version,
        platform=expected_platform,
        publisher=manifest["publisher"],
        signature_key_id=signature_key_id,
        manifest_sha256=manifest_sha256,
        entrypoints=entrypoints,
        files=files,
    )


def verify_official_video_pack_source(
    source: Path,
    *,
    contract: OfficialVideoPackContract,
    trusted_keys: Mapping[str, PackTrustKey],
    platform_tag: str | None = None,
) -> VerifiedOfficialVideoPack:
    return _verify_tree(
        source,
        contract=contract,
        trusted_keys=trusted_keys,
        platform_tag=platform_tag,
        receipt_required=False,
    )


def verify_official_video_pack_generation(
    source: Path,
    *,
    contract: OfficialVideoPackContract,
    trusted_keys: Mapping[str, PackTrustKey],
    platform_tag: str | None = None,
) -> VerifiedOfficialVideoPack:
    return _verify_tree(
        source,
        contract=contract,
        trusted_keys=trusted_keys,
        platform_tag=platform_tag,
        receipt_required=True,
    )


__all__ = [
    "PackTrustKey",
    "VerifiedOfficialVideoPack",
    "canonical_manifest_bytes",
    "canonical_receipt_bytes",
    "current_pack_platform",
    "verify_official_video_pack_generation",
    "verify_official_video_pack_source",
]
