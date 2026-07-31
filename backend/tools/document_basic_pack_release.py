from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from filelock import FileLock, Timeout
from platformdirs import user_data_path

from app.adapters.documents.document_basic_pack import (
    DOCLING_SLIM_VERSION,
    LAYOUT_MODEL_DIRECTORY,
    LAYOUT_MODEL_REVISION,
    LAYOUT_MODEL_WEIGHTS_SHA256,
    LAYOUT_MODEL_WEIGHTS_SIZE,
    NUMPY_VERSION,
    OPENCV_HEADLESS_VERSION,
    PACK_ID,
    PACK_VERSION,
    SCIPY_VERSION,
    TABLE_MODEL_CONFIG_RELATIVE_PATH,
    TABLE_MODEL_CONFIG_SHA256,
    TABLE_MODEL_CONFIG_SIZE,
    TABLE_MODEL_DIRECTORY,
    TABLE_MODEL_REVISION,
    TABLE_MODEL_WEIGHTS_RELATIVE_PATH,
    TABLE_MODEL_WEIGHTS_SHA256,
    TABLE_MODEL_WEIGHTS_SIZE,
    TORCH_VERSION,
)
from app.adapters.documents.document_basic_pack_verifier import (
    DocumentPackTrustKey,
    VerifiedDocumentPack,
    canonical_manifest_bytes,
    verify_document_basic_pack_source,
)
from app.adapters.documents.docling_worker_parser import (
    DoclingWorkerConfig,
    DoclingWorkerParser,
)
from app.core.errors import DomainError


_PUBLISHER = "alltonote-official"
_PLATFORM = "windows-x86_64"
_PYTHON_VERSION = "3.11.15"
_SIGNING_SERVICE = "AllToNote/release/document-basic/ed25519"
_KEY_PREFIX = "ed25519-raw-v1:"
_KEY_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")
_REQUIRED_DISTRIBUTIONS = {
    "docling-slim": DOCLING_SLIM_VERSION,
    "numpy": NUMPY_VERSION,
    "opencv-python-headless": OPENCV_HEADLESS_VERSION,
    "scipy": SCIPY_VERSION,
    "torch": TORCH_VERSION,
}
_LICENSE_CLASSIFIERS = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
}


class ReleaseError(RuntimeError):
    pass


class _Vault(Protocol):
    persist: object

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


Doctor = Callable[[Path, Path, Path], None]
PythonIdentityProbe = Callable[[Path], Mapping[str, object]]

_MAX_FILE_COUNT = 100_000
_MAX_TREE_ENTRY_COUNT = 2 * _MAX_FILE_COUNT + 2
_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def _validate_key_id(key_id: str) -> None:
    if _KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise ReleaseError("invalid signing key id")


def _windows_vault(vault: _Vault | None = None) -> _Vault:
    if vault is None:
        if os.name != "nt":
            raise ReleaseError("Windows Credential Manager is required")
        try:
            from keyring.backends.Windows import WinVaultKeyring

            vault = WinVaultKeyring()
        except (ImportError, RuntimeError) as error:
            raise ReleaseError("Windows Credential Manager is unavailable") from error
    try:
        vault.persist = "local machine"
    except Exception as error:
        raise ReleaseError("Windows Credential Manager cannot be configured") from error
    return vault


def _private_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def initialize_signing_key(
    key_id: str,
    *,
    vault: _Vault | None = None,
) -> bytes:
    """Create one DPAPI-protected exportable seed and return only its public key."""

    _validate_key_id(key_id)
    if vault is not None:
        return _initialize_signing_key(key_id, _windows_vault(vault))
    lock_root = Path(user_data_path("AllToNote", roaming=False)) / "release"
    lock_root.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(lock_root / "document-basic-key-init.lock", timeout=30):
            return _initialize_signing_key(key_id, _windows_vault())
    except Timeout as error:
        raise ReleaseError("another signing-key initialization is running") from error


def _initialize_signing_key(key_id: str, backend: _Vault) -> bytes:
    if backend.get_password(_SIGNING_SERVICE, key_id) is not None:
        raise ReleaseError(f"signing key {key_id!r} already exists")
    private_key = Ed25519PrivateKey.generate()
    encoded = base64.b64encode(_private_bytes(private_key)).decode("ascii")
    backend.set_password(_SIGNING_SERVICE, key_id, _KEY_PREFIX + encoded)
    loaded = load_signing_key(key_id, vault=backend)
    if _private_bytes(loaded) != _private_bytes(private_key):
        raise ReleaseError("signing key could not be verified after storage")
    return _public_bytes(private_key)


def load_signing_key(
    key_id: str,
    *,
    vault: _Vault | None = None,
) -> Ed25519PrivateKey:
    _validate_key_id(key_id)
    backend = _windows_vault(vault)
    stored = backend.get_password(_SIGNING_SERVICE, key_id)
    if not stored or not stored.startswith(_KEY_PREFIX):
        raise ReleaseError(f"signing key {key_id!r} is unavailable")
    try:
        raw = base64.b64decode(stored[len(_KEY_PREFIX) :], validate=True)
        if len(raw) != 32:
            raise ValueError("invalid key length")
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (TypeError, ValueError) as error:
        raise ReleaseError(f"signing key {key_id!r} is invalid") from error


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _ordinary_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReleaseError(f"directory is unavailable: {path}") from error
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseError(f"unsafe filesystem entry at {path}")
    return metadata


def _assert_ordinary_ancestors(path: Path) -> None:
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(chain):
        _ordinary_directory(item)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        common = os.path.commonpath((str(left), str(right)))
    except ValueError:
        return False
    normalized_common = os.path.normcase(os.path.abspath(common))
    return normalized_common in {
        os.path.normcase(str(left)),
        os.path.normcase(str(right)),
    }


def _source_root(path: Path) -> Path:
    root = _absolute(path)
    _assert_ordinary_ancestors(root.parent)
    _ordinary_directory(root)
    return root


def _copy_file_snapshot(
    source: Path,
    target: Path,
    *,
    remaining_bytes: int,
) -> tuple[int, str]:
    try:
        metadata = source.lstat()
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_FILE_BYTES
            or metadata.st_size > remaining_bytes
        ):
            raise OSError("unsafe source file")
        digest = hashlib.sha256()
        byte_count = 0
        with source.open("rb") as source_stream, target.open("xb") as target_stream:
            opened = os.fstat(source_stream.fileno())
            if not _same_file(metadata, opened):
                raise OSError("source changed before copy")
            for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                byte_count += len(chunk)
                if byte_count > _MAX_FILE_BYTES:
                    raise OSError("source file limit")
                digest.update(chunk)
                target_stream.write(chunk)
            finished = os.fstat(source_stream.fileno())
            target_stream.flush()
            target_finished = os.fstat(target_stream.fileno())
        final_metadata = source.lstat()
        if (
            byte_count != metadata.st_size
            or target_finished.st_size != byte_count
            or not _same_file(opened, finished)
            or not _same_file(metadata, final_metadata)
        ):
            raise OSError("source changed during copy")
        return byte_count, "sha256:" + digest.hexdigest()
    except OSError as error:
        try:
            target.unlink()
        except OSError:
            pass
        raise ReleaseError(f"unsafe filesystem entry under {source}") from error


def _copy_tree_snapshot(source: Path, target: Path) -> list[dict[str, object]]:
    source_metadata = _ordinary_directory(source)
    if target.exists():
        _ordinary_directory(target)
        if any(target.iterdir()):
            raise ReleaseError(f"snapshot target is not empty: {target}")
    else:
        target.mkdir()
    files: list[dict[str, object]] = []
    entry_count = 0
    total_bytes = 0
    try:
        for current, directories, filenames in os.walk(source, followlinks=False):
            directories.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            entry_count += len(directories) + len(filenames)
            if entry_count > _MAX_TREE_ENTRY_COUNT:
                raise ReleaseError("Pack tree entry limit exceeded")
            current_path = Path(current)
            current_metadata = _ordinary_directory(current_path)
            relative_directory = current_path.relative_to(source)
            target_directory = target / relative_directory
            if relative_directory != Path("."):
                target_directory.mkdir(exist_ok=False)
            for directory in directories:
                _ordinary_directory(current_path / directory)
            for filename in filenames:
                source_file = current_path / filename
                target_file = target_directory / filename
                byte_length, sha256 = _copy_file_snapshot(
                    source_file,
                    target_file,
                    remaining_bytes=_MAX_TOTAL_BYTES - total_bytes,
                )
                total_bytes += byte_length
                if len(files) >= _MAX_FILE_COUNT or total_bytes > _MAX_TOTAL_BYTES:
                    raise ReleaseError("Pack payload limit exceeded")
                files.append(
                    {
                        "path": source_file.relative_to(source).as_posix(),
                        "byte_length": byte_length,
                        "sha256": sha256,
                    }
                )
            if not _same_directory(current_metadata, current_path.lstat()):
                raise ReleaseError(f"source directory changed during copy: {current_path}")
        if not _same_directory(source_metadata, source.lstat()):
            raise ReleaseError(f"source root changed during copy: {source}")
    except OSError as error:
        raise ReleaseError(f"unsafe filesystem entry under {source}") from error
    return files


def _canonical_control_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _distribution_inventory(
    python_root: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    site_packages = python_root / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise ReleaseError("prepared Python is missing Lib/site-packages")
    inventory: list[dict[str, object]] = []
    sbom_components: list[dict[str, object]] = []
    discovered_versions: dict[str, str] = {}
    for metadata_path in sorted(
        site_packages.glob("*.dist-info/METADATA"),
        key=lambda path: path.as_posix().casefold(),
    ):
        try:
            message = BytesParser(policy=policy.compat32).parsebytes(
                metadata_path.read_bytes()
            )
        except OSError as error:
            raise ReleaseError(f"cannot read distribution metadata {metadata_path}") from error
        name = message.get("Name")
        version = message.get("Version")
        if not name or not version:
            raise ReleaseError(f"invalid distribution metadata {metadata_path}")
        normalized_name = _normalized_distribution_name(name)
        if normalized_name in discovered_versions:
            raise ReleaseError(f"duplicate distribution {normalized_name}")
        discovered_versions[normalized_name] = version
        dist_info = metadata_path.parent
        license_paths = {
            "python/" + path.relative_to(python_root).as_posix()
            for path in sorted(
                (candidate for candidate in dist_info.rglob("*") if candidate.is_file()),
                key=lambda path: path.as_posix().casefold(),
            )
            if path.name.lower().startswith(
                ("license", "licence", "copying", "notice", "authors")
            )
        }
        declared_license_files = message.get_all("License-File", [])
        for declared in declared_license_files:
            candidates = (dist_info / declared, dist_info / "licenses" / declared)
            resolved = next((path for path in candidates if path.is_file()), None)
            if resolved is None:
                raise ReleaseError(
                    f"distribution {normalized_name} is missing declared license {declared}"
                )
            license_paths.add(
                "python/" + resolved.relative_to(python_root).as_posix()
            )
        license_files = sorted(license_paths, key=str.casefold)
        explicit_expression = message.get("License-Expression")
        legacy_license = message.get("License")
        classifier_expression = next(
            (
                spdx
                for classifier, spdx in _LICENSE_CLASSIFIERS.items()
                if classifier in message.get_all("Classifier", [])
            ),
            None,
        )
        license_expression = (
            explicit_expression
            or legacy_license
            or classifier_expression
            or "NOASSERTION"
        )
        license_source = (
            "license-expression"
            if explicit_expression
            else "legacy-license"
            if legacy_license
            else "classifier"
            if classifier_expression
            else "files-only"
            if license_files
            else "missing"
        )
        if license_expression == "NOASSERTION" and not license_files:
            raise ReleaseError(
                f"distribution {normalized_name} has no usable license evidence"
            )
        inventory.append(
            {
                "name": normalized_name,
                "version": version,
                "license_expression": license_expression,
                "license_source": license_source,
                "declared_license_files": declared_license_files,
                "license_files": license_files,
            }
        )
        sbom_components.append(
            {
                "type": "library",
                "name": normalized_name,
                "version": version,
                "purl": (
                    f"pkg:pypi/{quote(normalized_name, safe='')}@"
                    f"{quote(version, safe='')}"
                ),
                "properties": [
                    {
                        "name": "alltonote:license-expression",
                        "value": license_expression,
                    },
                    {
                        "name": "alltonote:license-files",
                        "value": json.dumps(
                            license_files,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
            }
        )
    if not inventory:
        raise ReleaseError("prepared Python has no distribution metadata")
    for name, expected_version in _REQUIRED_DISTRIBUTIONS.items():
        if discovered_versions.get(name) != expected_version:
            raise ReleaseError(
                f"prepared Python distribution {name} must be {expected_version}"
            )
    return inventory, sbom_components


def _probe_python_identity(python_executable: Path) -> Mapping[str, object]:
    allowed_environment = {
        key: value
        for key in (
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS",
        )
        if (value := os.environ.get(key)) is not None
    }
    allowed_environment["PYTHONNOUSERSITE"] = "1"
    allowed_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            str(python_executable),
            "-I",
            "-B",
            "-c",
            (
                "import json,platform,struct,sys;"
                "print(json.dumps({"
                "'implementation':sys.implementation.name,"
                "'platform':sys.platform,"
                "'machine':platform.machine().lower(),"
                "'pointer_bits':struct.calcsize('P')*8,"
                "'version':platform.python_version()"
                "},sort_keys=True,separators=(',',':')))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=allowed_environment,
        timeout=30,
    )
    if result.returncode != 0:
        raise ReleaseError("prepared Python cannot report its identity")
    try:
        identity = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseError("prepared Python returned an invalid identity") from error
    if type(identity) is not dict:
        raise ReleaseError("prepared Python returned an invalid identity")
    machine = identity.get("machine")
    identity["machine"] = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
    }.get(machine, machine)
    return identity


def _run_doctor(
    python_executable: Path,
    artifacts_root: Path,
    backend_root: Path,
) -> None:
    DoclingWorkerParser(
        DoclingWorkerConfig(
            python_executable=python_executable,
            artifacts_path=artifacts_root,
            backend_root=backend_root,
        )
    ).doctor()


def _write_controls(
    root: Path,
    *,
    python_identity: Mapping[str, object],
    inventory: list[dict[str, object]],
    sbom_components: list[dict[str, object]],
) -> None:
    cpython_license = root / "python" / "LICENSE.txt"
    if not cpython_license.is_file():
        raise ReleaseError("prepared Python is missing LICENSE.txt")
    native_license_files = [
        "python/" + path.relative_to(root / "python").as_posix()
        for path in sorted(
            (
                candidate
                for candidate in (root / "python").rglob("*")
                if candidate.is_file()
                and candidate.name.lower().startswith(
                    ("license", "licence", "copying", "notice", "authors")
                )
            ),
            key=lambda path: path.as_posix().casefold(),
        )
    ]
    distributions = {item["name"]: item["version"] for item in inventory}
    lock = {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "platform": _PLATFORM,
        "python": {
            **dict(python_identity),
            "distributions": distributions,
        },
        "models": [
            {
                "name": LAYOUT_MODEL_DIRECTORY,
                "revision": LAYOUT_MODEL_REVISION,
                "weights": {
                    "path": "model.safetensors",
                    "byte_length": LAYOUT_MODEL_WEIGHTS_SIZE,
                    "sha256": "sha256:" + LAYOUT_MODEL_WEIGHTS_SHA256,
                },
            },
            {
                "name": TABLE_MODEL_DIRECTORY,
                "revision": TABLE_MODEL_REVISION,
                "weights": {
                    "path": TABLE_MODEL_WEIGHTS_RELATIVE_PATH,
                    "byte_length": TABLE_MODEL_WEIGHTS_SIZE,
                    "sha256": "sha256:" + TABLE_MODEL_WEIGHTS_SHA256,
                },
                "config": {
                    "path": TABLE_MODEL_CONFIG_RELATIVE_PATH,
                    "byte_length": TABLE_MODEL_CONFIG_SIZE,
                    "sha256": "sha256:" + TABLE_MODEL_CONFIG_SHA256,
                },
            },
        ],
    }
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": PACK_ID,
                "version": PACK_VERSION,
            }
        },
        "components": [
            {
                "type": "application",
                "name": "CPython",
                "version": python_identity["version"],
                "licenses": [{"license": {"id": "PSF-2.0"}}],
                "properties": [
                    {
                        "name": "alltonote:license-file",
                        "value": "python/LICENSE.txt",
                    }
                ],
            },
            *sbom_components,
            {
                "type": "machine-learning-model",
                "name": LAYOUT_MODEL_DIRECTORY,
                "version": LAYOUT_MODEL_REVISION,
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            },
            {
                "type": "machine-learning-model",
                "name": TABLE_MODEL_DIRECTORY,
                "version": TABLE_MODEL_REVISION,
                "licenses": [
                    {"license": {"id": "CDLA-Permissive-2.0"}}
                ],
            },
            {
                "type": "library",
                "name": "bundled-native-and-vendored-runtime-notices",
                "version": PACK_VERSION,
                "properties": [
                    {
                        "name": "alltonote:license-file",
                        "value": "licenses/native-runtime-files.json",
                    }
                ],
            },
        ],
    }
    licenses = root / "licenses"
    licenses.mkdir()
    (root / "document-basic-lock.json").write_bytes(_canonical_control_bytes(lock))
    (root / "sbom.cdx.json").write_bytes(_canonical_control_bytes(sbom))
    (licenses / "python-packages.json").write_bytes(
        _canonical_control_bytes(
            {
                "schema_version": 1,
                "components": inventory,
            }
        )
    )
    (licenses / "native-runtime-files.json").write_bytes(
        _canonical_control_bytes(
            {
                "schema_version": 1,
                "files": native_license_files,
            }
        )
    )


def _unsigned_manifest(
    root: Path,
    files: list[dict[str, object]],
) -> dict[str, object]:
    layout_readme = f"artifacts/{LAYOUT_MODEL_DIRECTORY}/README.md"
    table_readme = f"artifacts/{TABLE_MODEL_DIRECTORY}/README.md"
    for required in (
        "python/python.exe",
        "python/LICENSE.txt",
        "document-basic-lock.json",
        "licenses/native-runtime-files.json",
        "licenses/python-packages.json",
        "sbom.cdx.json",
        layout_readme,
        table_readme,
    ):
        if not root.joinpath(*required.split("/")).is_file():
            raise ReleaseError(f"prepared Pack is missing {required}")
    if any(item["path"] == "manifest.json" for item in files):
        raise ReleaseError("assembled Pack already contains manifest.json")
    sbom_entry = next(item for item in files if item["path"] == "sbom.cdx.json")
    return {
        "manifest_version": 1,
        "pack_id": PACK_ID,
        "version": PACK_VERSION,
        "platform": _PLATFORM,
        "runtime_api": {"min": 1, "max": 1},
        "recipe_contracts": {"alltonote.document-note": [1]},
        "capabilities": ["recipe.document.parse.pdf"],
        "entrypoints": [
            {
                "name": "python",
                "type": "process",
                "relative_path": "python/python.exe",
            }
        ],
        "files": files,
        "licenses": [
            {
                "component": "cpython",
                "spdx": "PSF-2.0",
                "file": "python/LICENSE.txt",
            },
            {
                "component": "docling-layout-heron",
                "spdx": "Apache-2.0",
                "file": layout_readme,
            },
            {
                "component": "docling-models",
                "spdx": "CDLA-Permissive-2.0",
                "file": table_readme,
            },
            {
                "component": "python-dependencies",
                "spdx": "NOASSERTION",
                "file": "licenses/python-packages.json",
            },
            {
                "component": "native-and-vendored-runtime",
                "spdx": "NOASSERTION",
                "file": "licenses/native-runtime-files.json",
            },
        ],
        "sbom": {
            "format": "cyclonedx-json",
            "file": "sbom.cdx.json",
            "sha256": sbom_entry["sha256"],
        },
        "publisher": _PUBLISHER,
    }


def _output_path(output: Path) -> tuple[Path, os.stat_result]:
    output_path = _absolute(output)
    _assert_ordinary_ancestors(output_path.parent)
    parent_metadata = _ordinary_directory(output_path.parent)
    try:
        output_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ReleaseError(f"output is unavailable: {output_path}") from error
    else:
        raise ReleaseError(f"output already exists: {output_path}")
    return output_path, parent_metadata


def _output_staging(
    output_path: Path,
    parent_metadata: os.stat_result,
) -> Path:
    if not _same_directory_identity(parent_metadata, output_path.parent.lstat()):
        raise ReleaseError("output parent changed before staging")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.staging-",
            dir=output_path.parent,
        )
    )
    _ordinary_directory(staging)
    return staging


def _publish_staging(
    staging: Path,
    output_path: Path,
    parent_metadata: os.stat_result,
) -> None:
    for attempt in range(5):
        if not _same_directory_identity(parent_metadata, output_path.parent.lstat()):
            raise ReleaseError("output parent changed before publication")
        try:
            output_path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ReleaseError("output appeared before publication")
        try:
            os.rename(staging, output_path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (2**attempt))


def _cleanup_staging(staging: Path, output_path: Path) -> None:
    if not staging.exists() or staging.parent != output_path.parent:
        return
    try:
        shutil.rmtree(staging)
    except OSError:
        pass


def assemble_document_basic_pack(
    *,
    python_root: Path,
    artifacts_root: Path,
    output: Path,
    backend_root: Path,
    doctor: Doctor = _run_doctor,
    python_identity_probe: PythonIdentityProbe = _probe_python_identity,
) -> Path:
    """Assemble and dynamically validate an unsigned Pack without loading a key."""

    prepared_python = _source_root(python_root)
    prepared_artifacts = _source_root(artifacts_root)
    runtime_backend = _source_root(backend_root)
    output_path, parent_metadata = _output_path(output)
    roots = (prepared_python, prepared_artifacts, runtime_backend, output_path)
    if any(
        _paths_overlap(left, right)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise ReleaseError("Pack inputs and output must be disjoint")
    staging = _output_staging(output_path, parent_metadata)
    try:
        _copy_tree_snapshot(prepared_python, staging / "python")
        _copy_tree_snapshot(prepared_artifacts, staging / "artifacts")
        python_executable = staging / "python" / "python.exe"
        if not python_executable.is_file():
            raise ReleaseError("prepared Python is missing python.exe")
        python_identity = dict(python_identity_probe(python_executable))
        if python_identity != {
            "implementation": "cpython",
            "platform": "win32",
            "machine": "x86_64",
            "pointer_bits": 64,
            "version": _PYTHON_VERSION,
        }:
            raise ReleaseError(
                f"prepared Python must be Windows x86_64 CPython {_PYTHON_VERSION}"
            )
        doctor(python_executable, staging / "artifacts", runtime_backend)
        inventory, sbom_components = _distribution_inventory(staging / "python")
        _write_controls(
            staging,
            python_identity=python_identity,
            inventory=inventory,
            sbom_components=sbom_components,
        )
        _publish_staging(staging, output_path, parent_metadata)
        return output_path
    except BaseException:
        _cleanup_staging(staging, output_path)
        raise


def sign_document_basic_pack(
    *,
    assembled_root: Path,
    output: Path,
    key_id: str,
    private_key: Ed25519PrivateKey,
    trusted_keys: Mapping[str, DocumentPackTrustKey],
) -> VerifiedDocumentPack:
    """Hash and sign an assembled tree without executing any Pack code."""

    _validate_key_id(key_id)
    trust = trusted_keys.get(key_id)
    public_key = _public_bytes(private_key)
    if (
        trust is None
        or trust.key_id != key_id
        or trust.publisher != _PUBLISHER
        or trust.public_key != public_key
    ):
        raise ReleaseError("signing key does not match the embedded trust root")
    assembled = _source_root(assembled_root)
    output_path, parent_metadata = _output_path(output)
    if _paths_overlap(assembled, output_path):
        raise ReleaseError("assembled Pack and signed output must be disjoint")
    staging = _output_staging(output_path, parent_metadata)
    try:
        files = _copy_tree_snapshot(assembled, staging)
        manifest = _unsigned_manifest(staging, files)
        signature = private_key.sign(canonical_manifest_bytes(manifest))
        manifest["signature"] = {
            "algorithm": "ed25519",
            "key_id": key_id,
            "value": base64.b64encode(signature).decode("ascii"),
        }
        (staging / "manifest.json").write_bytes(canonical_manifest_bytes(manifest))
        verified = verify_document_basic_pack_source(
            staging,
            trusted_keys=trusted_keys,
            platform_tag=_PLATFORM,
        )
        _publish_staging(staging, output_path, parent_metadata)
        return VerifiedDocumentPack(
            source_root=output_path,
            pack_id=verified.pack_id,
            pack_version=verified.pack_version,
            platform=verified.platform,
            publisher=verified.publisher,
            signature_key_id=verified.signature_key_id,
            manifest_sha256=verified.manifest_sha256,
            python_relative_path=verified.python_relative_path,
            files=verified.files,
        )
    except BaseException:
        _cleanup_staging(staging, output_path)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release document-basic Pack")
    commands = parser.add_subparsers(dest="command", required=True)
    key_init = commands.add_parser("key-init")
    key_init.add_argument("--key-id", required=True)
    key_public = commands.add_parser("key-public")
    key_public.add_argument("--key-id", required=True)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--python-root", type=Path, required=True)
    assemble.add_argument("--artifacts-root", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).parents[1],
    )
    sign = commands.add_parser("sign")
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--assembled-root", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    return parser


def _public_result(key_id: str, public_key: bytes) -> dict[str, object]:
    return {
        "schema_version": 1,
        "key_id": key_id,
        "algorithm": "ed25519",
        "public_key_hex": public_key.hex(),
        "fingerprint_sha256": hashlib.sha256(public_key).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "key-init":
            result = _public_result(args.key_id, initialize_signing_key(args.key_id))
        elif args.command == "key-public":
            result = _public_result(
                args.key_id,
                _public_bytes(load_signing_key(args.key_id)),
            )
        elif args.command == "assemble":
            output = assemble_document_basic_pack(
                python_root=args.python_root,
                artifacts_root=args.artifacts_root,
                output=args.output,
                backend_root=args.backend_root,
            )
            result = {
                "schema_version": 1,
                "result": "assembled",
                "pack_id": PACK_ID,
                "pack_version": PACK_VERSION,
                "platform": _PLATFORM,
                "output": str(output),
            }
        else:
            from app.adapters.documents.document_basic_pack_trust import (
                official_document_pack_trust_keys,
            )

            verified = sign_document_basic_pack(
                assembled_root=args.assembled_root,
                output=args.output,
                key_id=args.key_id,
                private_key=load_signing_key(args.key_id),
                trusted_keys=official_document_pack_trust_keys(),
            )
            result = {
                "schema_version": 1,
                "result": "built",
                "pack_id": verified.pack_id,
                "pack_version": verified.pack_version,
                "platform": verified.platform,
                "key_id": verified.signature_key_id,
                "manifest_sha256": verified.manifest_sha256,
                "file_count": len(verified.files),
                "output": str(verified.source_root),
            }
    except (DomainError, OSError, ReleaseError) as error:
        payload = {
            "schema_version": 1,
            "result": "failed",
            "error": getattr(error, "code", type(error).__name__),
            "message": str(error),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ReleaseError",
    "assemble_document_basic_pack",
    "initialize_signing_key",
    "load_signing_key",
    "main",
    "sign_document_basic_pack",
]
