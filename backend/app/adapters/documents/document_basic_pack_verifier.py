from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
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
_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_MAX_RELATIVE_PATH_LENGTH = 512
_MAX_CONTROL_FILE_BYTES = 16 * 1024
_SHA256_PREFIX = "sha256:"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


@dataclass(frozen=True)
class DocumentPackTrustKey:
    key_id: str
    publisher: str
    public_key: bytes


@dataclass(frozen=True)
class DocumentPackFile:
    relative_path: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class VerifiedDocumentPack:
    source_root: Path
    pack_id: str
    pack_version: str
    platform: str
    publisher: str
    signature_key_id: str
    manifest_sha256: str
    python_relative_path: str
    files: tuple[DocumentPackFile, ...]


def current_pack_platform() -> str:
    operating_system = {
        "win32": "windows",
        "darwin": "macos",
        "linux": "linux",
    }.get(os.sys.platform, os.sys.platform)
    architecture = platform.machine().lower()
    normalized_architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(architecture, architecture)
    return f"{operating_system}-{normalized_architecture}"


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


def canonical_receipt_bytes(manifest_sha256: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
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
        "The document-basic Pack manifest is invalid",
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


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _same_open_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _read_regular_file(path: Path, byte_limit: int) -> bytes:
    metadata = path.lstat()
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > byte_limit
    ):
        raise OSError("unsafe_file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _same_open_file(metadata, opened):
            raise OSError("file_changed_before_read")
        content = stream.read(byte_limit + 1)
        finished = os.fstat(stream.fileno())
    if (
        len(content) > byte_limit
        or len(content) != finished.st_size
        or not _same_open_file(opened, finished)
    ):
        raise OSError("file_changed_during_read")
    return content


def _load_manifest(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        content = _read_regular_file(path, _MAX_MANIFEST_BYTES)
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except DomainError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise _manifest_invalid() from error
    if type(payload) is not dict or frozenset(payload) != _MANIFEST_KEYS:
        raise _manifest_invalid()
    if content != canonical_manifest_bytes(payload):
        raise _manifest_invalid()
    return payload, content


def _valid_sha256(value: object) -> bool:
    if type(value) is not str or not value.startswith(_SHA256_PREFIX):
        return False
    digest = value[len(_SHA256_PREFIX) :]
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _normalized_relative_path(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_RELATIVE_PATH_LENGTH:
        raise _manifest_invalid()
    if (
        value.startswith("/")
        or "\\" in value
        or ":" in value
        or "\0" in value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise _manifest_invalid()
    segments = value.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or segment.endswith((".", " "))
        or segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        for segment in segments
    ):
        raise _manifest_invalid()
    return value


def _manifest_files(payload: object) -> tuple[DocumentPackFile, ...]:
    if type(payload) is not list or not payload or len(payload) > _MAX_FILE_COUNT:
        raise _manifest_invalid()
    files: list[DocumentPackFile] = []
    collision_keys: set[str] = set()
    total_bytes = 0
    for item in payload:
        if type(item) is not dict or frozenset(item) != _FILE_KEYS:
            raise _manifest_invalid()
        relative_path = _normalized_relative_path(item.get("path"))
        byte_length = item.get("byte_length")
        sha256 = item.get("sha256")
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
        files.append(DocumentPackFile(relative_path, byte_length, sha256))
    return tuple(files)


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise _manifest_invalid()
    return value


def _validate_contract(
    manifest: dict[str, object],
    files: tuple[DocumentPackFile, ...],
    expected_platform: str,
) -> str:
    if (
        type(manifest.get("manifest_version")) is not int
        or manifest.get("manifest_version") != 1
        or manifest.get("pack_id") != PACK_ID
    ):
        raise _manifest_invalid()
    if manifest.get("version") != PACK_VERSION:
        raise DomainError(
            "pack_version_unsupported",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "The document-basic Pack version is not supported",
        )
    if manifest.get("platform") != expected_platform:
        raise DomainError(
            "pack_platform_incompatible",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "The document-basic Pack platform is incompatible",
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
            "The document-basic Pack Runtime API is incompatible",
        )
    if manifest.get("recipe_contracts") != {"alltonote.document-note": [1]}:
        raise DomainError(
            "pack_recipe_incompatible",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "The document-basic Pack Recipe contract is incompatible",
        )
    if manifest.get("capabilities") != ["recipe.document.parse.pdf"]:
        raise _manifest_invalid()

    entrypoints = manifest.get("entrypoints")
    if type(entrypoints) is not list or len(entrypoints) != 1:
        raise _manifest_invalid()
    entrypoint = _exact_mapping(entrypoints[0], _ENTRYPOINT_KEYS)
    python_relative_path = _normalized_relative_path(entrypoint.get("relative_path"))
    expected_python = (
        "python/python.exe"
        if expected_platform == "windows-x86_64"
        else "python/bin/python"
    )
    if entrypoint != {
        "name": "python",
        "type": "process",
        "relative_path": expected_python,
    }:
        raise _manifest_invalid()

    file_paths = {item.relative_path: item for item in files}
    required_paths = {python_relative_path, "document-basic-lock.json"}
    if not required_paths <= file_paths.keys() or not any(
        path.startswith("artifacts/") for path in file_paths
    ):
        raise _manifest_invalid()

    licenses = manifest.get("licenses")
    if type(licenses) is not list or not licenses:
        raise _manifest_invalid()
    for license_item in licenses:
        license_mapping = _exact_mapping(license_item, _LICENSE_KEYS)
        license_path = _normalized_relative_path(license_mapping.get("file"))
        if (
            type(license_mapping.get("component")) is not str
            or not license_mapping["component"]
            or type(license_mapping.get("spdx")) is not str
            or not license_mapping["spdx"]
            or license_path not in file_paths
        ):
            raise _manifest_invalid()

    sbom = _exact_mapping(manifest.get("sbom"), _SBOM_KEYS)
    sbom_path = _normalized_relative_path(sbom.get("file"))
    if (
        sbom.get("format") != "cyclonedx-json"
        or not _valid_sha256(sbom.get("sha256"))
        or sbom_path not in file_paths
        or file_paths[sbom_path].sha256 != sbom["sha256"]
    ):
        raise _manifest_invalid()
    return python_relative_path


def _verify_signature(
    manifest: dict[str, object],
    trusted_keys: Mapping[str, DocumentPackTrustKey],
) -> str:
    if not trusted_keys:
        raise DomainError(
            "pack_trust_unconfigured",
            ErrorCategory.POLICY_DENIED,
            "No trusted document-basic Pack signing key is configured",
        )
    signature = _exact_mapping(manifest.get("signature"), _SIGNATURE_KEYS)
    key_id = signature.get("key_id")
    publisher = manifest.get("publisher")
    if type(publisher) is not str or publisher != "alltonote-official":
        raise DomainError(
            "pack_publisher_untrusted",
            ErrorCategory.POLICY_DENIED,
            "The document-basic Pack publisher is not trusted",
        )
    trust = trusted_keys.get(key_id) if type(key_id) is str else None
    if trust is None or trust.publisher != publisher:
        raise DomainError(
            "pack_signature_invalid",
            ErrorCategory.POLICY_DENIED,
            "The document-basic Pack signature is invalid",
        )
    try:
        encoded_signature = signature.get("value")
        if signature.get("algorithm") != "ed25519" or type(encoded_signature) is not str:
            raise ValueError
        decoded_signature = base64.b64decode(encoded_signature, validate=True)
        if len(decoded_signature) != 64 or len(trust.public_key) != 32:
            raise ValueError
        unsigned = dict(manifest)
        unsigned.pop("signature")
        Ed25519PublicKey.from_public_bytes(trust.public_key).verify(
            decoded_signature,
            canonical_manifest_bytes(unsigned),
        )
    except (InvalidSignature, TypeError, ValueError) as error:
        raise DomainError(
            "pack_signature_invalid",
            ErrorCategory.POLICY_DENIED,
            "The document-basic Pack signature is invalid",
        ) from error
    return trust.key_id


def _source_files(root: Path) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    entry_count = 0
    total_bytes = 0
    try:
        root_metadata = root.lstat()
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source is unavailable",
        ) from error
    if _is_link_or_reparse(root_metadata):
        raise DomainError(
            "pack_archive_unsafe",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source contains unsafe filesystem entries",
        )
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source must be a directory",
        )
    try:
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *filenames):
                entry_count += 1
                if entry_count > _MAX_TREE_ENTRY_COUNT:
                    raise OSError("source_entry_limit")
                target = current_path / name
                metadata = target.lstat()
                if _is_link_or_reparse(metadata):
                    raise OSError("unsafe_source_entry")
            for filename in filenames:
                target = current_path / filename
                metadata = target.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OSError("unsafe_source_file")
                total_bytes += metadata.st_size
                if total_bytes > _MAX_TOTAL_BYTES + _MAX_MANIFEST_BYTES + _MAX_CONTROL_FILE_BYTES:
                    raise OSError("source_total_limit")
                relative_path = target.relative_to(root).as_posix()
                _normalized_relative_path(relative_path)
                discovered[relative_path] = target
    except DomainError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "pack_archive_unsafe",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source contains unsafe filesystem entries",
        ) from error
    return discovered


def _validate_receipt(path: Path, manifest_sha256: str) -> None:
    try:
        content = _read_regular_file(path, _MAX_CONTROL_FILE_BYTES)
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
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("pack_id") != PACK_ID
        or payload.get("pack_version") != PACK_VERSION
        or payload.get("manifest_sha256") != manifest_sha256
        or payload.get("verified") is not True
        or content != canonical_receipt_bytes(manifest_sha256)
    ):
        raise _manifest_invalid()


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


def _verify_document_basic_pack_tree(
    source: Path,
    *,
    trusted_keys: Mapping[str, DocumentPackTrustKey],
    platform_tag: str | None = None,
    receipt_required: bool,
) -> VerifiedDocumentPack:
    try:
        root = Path(os.path.abspath(os.fspath(Path(source).expanduser())))
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source is unavailable",
        ) from error
    source_files = _source_files(root)
    manifest, manifest_bytes = _load_manifest(root / "manifest.json")
    files = _manifest_files(manifest.get("files"))
    expected_platform = platform_tag or current_pack_platform()
    python_relative_path = _validate_contract(manifest, files, expected_platform)
    signature_key_id = _verify_signature(manifest, trusted_keys)

    manifest_sha256 = _SHA256_PREFIX + hashlib.sha256(manifest_bytes).hexdigest()
    expected_files = {item.relative_path for item in files} | {"manifest.json"}
    if receipt_required:
        expected_files.add("receipt.json")
    if set(source_files) != expected_files:
        raise _manifest_invalid()
    for item in files:
        path = source_files[item.relative_path]
        try:
            if _sha256(path, item.byte_length) != item.sha256:
                raise DomainError(
                    "pack_hash_mismatch",
                    ErrorCategory.POLICY_DENIED,
                    "The document-basic Pack file hash does not match its manifest",
                    {"component": item.relative_path},
                )
        except DomainError:
            raise
        except OSError as error:
            raise DomainError(
                "pack_hash_mismatch",
                ErrorCategory.POLICY_DENIED,
                "The document-basic Pack file could not be verified",
                {"component": item.relative_path},
            ) from error
    if receipt_required:
        _validate_receipt(source_files["receipt.json"], manifest_sha256)

    return VerifiedDocumentPack(
        source_root=root,
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        platform=expected_platform,
        publisher=str(manifest["publisher"]),
        signature_key_id=signature_key_id,
        manifest_sha256=manifest_sha256,
        python_relative_path=python_relative_path,
        files=files,
    )


def verify_document_basic_pack_source(
    source: Path,
    *,
    trusted_keys: Mapping[str, DocumentPackTrustKey],
    platform_tag: str | None = None,
) -> VerifiedDocumentPack:
    return _verify_document_basic_pack_tree(
        source,
        trusted_keys=trusted_keys,
        platform_tag=platform_tag,
        receipt_required=False,
    )


def verify_document_basic_pack_generation(
    source: Path,
    *,
    trusted_keys: Mapping[str, DocumentPackTrustKey],
    platform_tag: str | None = None,
) -> VerifiedDocumentPack:
    return _verify_document_basic_pack_tree(
        source,
        trusted_keys=trusted_keys,
        platform_tag=platform_tag,
        receipt_required=True,
    )


__all__ = [
    "DocumentPackFile",
    "DocumentPackTrustKey",
    "VerifiedDocumentPack",
    "canonical_manifest_bytes",
    "canonical_receipt_bytes",
    "current_pack_platform",
    "verify_document_basic_pack_generation",
    "verify_document_basic_pack_source",
]
