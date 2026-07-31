"""Frozen identity and installation layout of the first practical Document Pack."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from app.adapters.pack_layout import (
    legacy_generation_root,
    managed_generation_root,
    managed_pack_root,
)

if TYPE_CHECKING:
    from app.runtime_paths import RuntimePaths

PACK_ID = "document-basic"
PACK_VERSION = "docling-2.117.0-tableformer-v2.3.0"

DOCLING_SLIM_VERSION = "2.117.0"
SCIPY_VERSION = "1.17.1"
TORCH_VERSION = "2.13.0+cpu"
NUMPY_VERSION = "2.2.6"
OPENCV_HEADLESS_VERSION = "4.12.0.88"

LAYOUT_MODEL_DIRECTORY = "docling-project--docling-layout-heron"
LAYOUT_MODEL_REVISION = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
LAYOUT_MODEL_WEIGHTS_SHA256 = (
    "00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c"
)
LAYOUT_MODEL_WEIGHTS_SIZE = 171_658_996

TABLE_MODEL_DIRECTORY = "docling-project--docling-models"
TABLE_MODEL_REVISION = "fc0f2d45e2218ea24bce5045f58a389aed16dc23"
TABLE_MODEL_WEIGHTS_RELATIVE_PATH = (
    "model_artifacts/tableformer/accurate/tableformer_accurate.safetensors"
)
TABLE_MODEL_WEIGHTS_SHA256 = (
    "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9"
)
TABLE_MODEL_WEIGHTS_SIZE = 212_758_388
TABLE_MODEL_CONFIG_RELATIVE_PATH = (
    "model_artifacts/tableformer/accurate/tm_config.json"
)
TABLE_MODEL_CONFIG_SHA256 = (
    "984e122ceb8ccf84d84c9d2882f6f2302a44b4f1e577babd6289892c36f3cffd"
)
TABLE_MODEL_CONFIG_SIZE = 7_060

PARSER_MODEL_REVISION = (
    f"layout:{LAYOUT_MODEL_REVISION}+tableformer:{TABLE_MODEL_REVISION}"
)

_ACTIVE_KEYS = frozenset(
    {"schema_version", "pack_id", "pack_version", "manifest_sha256"}
)
_RECEIPT_KEYS = _ACTIVE_KEYS | {"verified"}
_SHA256_PATTERN = re.compile(r"sha256:([0-9a-f]{64})\Z")
_CONTROL_FILE_LIMIT = 16 * 1024


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _ordinary_file(path: Path) -> bool:
    return not _is_link_or_reparse(path) and path.is_file()


def _ordinary_directory(path: Path) -> bool:
    return not _is_link_or_reparse(path) and path.is_dir()


def _reject_non_finite_json(_value: str) -> None:
    raise ValueError("non_finite_json")


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate_json_key")
        payload[key] = value
    return payload


def _same_open_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _read_control_file(path: Path) -> dict[str, object] | None:
    try:
        metadata = path.lstat()
        if (
            _is_link_or_reparse(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _CONTROL_FILE_LIMIT
        ):
            return None
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_open_file(metadata, opened):
                return None
            content = stream.read(_CONTROL_FILE_LIMIT + 1)
            finished = os.fstat(stream.fileno())
        if (
            len(content) > _CONTROL_FILE_LIMIT
            or len(content) != finished.st_size
            or not _same_open_file(opened, finished)
        ):
            return None
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_finite_json,
        )
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if type(payload) is dict else None


def _ordinary_directory_chain(root: Path, target: Path) -> bool:
    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    current = root
    if not _ordinary_directory(current):
        return False
    for part in relative.parts:
        current /= part
        if not _ordinary_directory(current):
            return False
    return True


def _managed_pack_paths(
    pack_root: Path,
    trusted_data_root: Path,
) -> tuple[Path, Path] | None:
    if not _ordinary_directory_chain(trusted_data_root, pack_root):
        return None
    pointer = _read_control_file(pack_root / "active.json")
    if pointer is None or frozenset(pointer) != _ACTIVE_KEYS:
        return None
    manifest_sha256 = pointer.get("manifest_sha256")
    match = (
        _SHA256_PATTERN.fullmatch(manifest_sha256)
        if type(manifest_sha256) is str
        else None
    )
    if (
        type(pointer.get("schema_version")) is not int
        or pointer.get("schema_version") != 1
        or pointer.get("pack_id") != PACK_ID
        or pointer.get("pack_version") != PACK_VERSION
        or match is None
    ):
        return None

    installs_root = managed_generation_root(trusted_data_root, PACK_ID)
    generation = installs_root / match.group(1)
    if not _lexically_exists(generation):
        installs_root = legacy_generation_root(
            trusted_data_root, PACK_ID, PACK_VERSION
        )
        generation = installs_root / match.group(1)
    receipt = _read_control_file(generation / "receipt.json")
    if (
        not _ordinary_directory(installs_root)
        or not _ordinary_directory(generation)
        or receipt is None
        or frozenset(receipt) != _RECEIPT_KEYS
        or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != 1
        or receipt.get("pack_id") != PACK_ID
        or receipt.get("pack_version") != PACK_VERSION
        or receipt.get("manifest_sha256") != manifest_sha256
        or receipt.get("verified") is not True
    ):
        return None

    python_relative = (
        Path("python/python.exe") if os.name == "nt" else Path("python/bin/python")
    )
    python_executable = generation / python_relative
    artifacts_path = generation / "artifacts"
    try:
        resolved_data_root = trusted_data_root.resolve(strict=True)
        resolved_pack_root = pack_root.resolve(strict=True)
        resolved_installs_root = installs_root.resolve(strict=True)
        resolved_generation = generation.resolve(strict=True)
        resolved_python = python_executable.resolve(strict=True)
        resolved_artifacts = artifacts_path.resolve(strict=True)
        if (
            not resolved_pack_root.is_relative_to(resolved_data_root)
            or not resolved_installs_root.is_relative_to(resolved_data_root)
            or not resolved_generation.is_relative_to(resolved_installs_root)
            or not resolved_python.is_relative_to(resolved_generation)
            or not resolved_artifacts.is_relative_to(resolved_generation)
        ):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        not _ordinary_directory_chain(installs_root, generation)
        or not _ordinary_directory_chain(generation, python_executable.parent)
        or not _ordinary_file(python_executable)
        or not _ordinary_directory_chain(generation, artifacts_path)
    ):
        return None
    return python_executable, artifacts_path


def resolve_document_basic_pack_paths(
    paths: RuntimePaths,
    environ: Mapping[str, str],
) -> tuple[Path, Path] | None:
    """Return the configured Python and model roots, or None for a partial override."""

    python_override = environ.get("ALLTONOTE_DOCUMENT_BASIC_PYTHON")
    artifacts_override = environ.get("ALLTONOTE_DOCUMENT_BASIC_ARTIFACTS")
    if bool(python_override) != bool(artifacts_override):
        return None
    if python_override and artifacts_override:
        return (
            Path(python_override).expanduser().resolve(strict=False),
            Path(artifacts_override).expanduser().resolve(strict=False),
        )
    pack_root = managed_pack_root(paths.data_dir, PACK_ID, PACK_VERSION)
    active_pointer = pack_root / "active.json"
    if _lexically_exists(active_pointer):
        return _managed_pack_paths(pack_root, paths.data_dir)
    return (
        pack_root
        / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"),
        pack_root / "artifacts",
    )


def document_basic_pack_installed(
    paths: RuntimePaths,
    environ: Mapping[str, str],
) -> bool:
    resolved = resolve_document_basic_pack_paths(paths, environ)
    return bool(
        resolved is not None
        and resolved[0].is_file()
        and resolved[1].is_dir()
    )
