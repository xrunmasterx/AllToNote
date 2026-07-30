"""Frozen identity and installation layout of the first practical Document Pack."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

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
    return path.is_symlink() or bool(
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


def _read_control_file(path: Path) -> dict[str, object] | None:
    try:
        if not _ordinary_file(path) or path.stat().st_size > _CONTROL_FILE_LIMIT:
            return None
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_non_finite_json,
        )
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if type(payload) is dict else None


def _managed_pack_paths(pack_root: Path) -> tuple[Path, Path] | None:
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
        pointer.get("schema_version") != 1
        or pointer.get("pack_id") != PACK_ID
        or pointer.get("pack_version") != PACK_VERSION
        or match is None
    ):
        return None

    installs_root = pack_root / "installs"
    generation = installs_root / match.group(1)
    receipt = _read_control_file(generation / "receipt.json")
    if (
        not _ordinary_directory(installs_root)
        or not _ordinary_directory(generation)
        or receipt is None
        or frozenset(receipt) != _RECEIPT_KEYS
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
        resolved_generation = generation.resolve(strict=True)
        if not resolved_generation.is_relative_to(pack_root.resolve(strict=True)):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    if not _ordinary_file(python_executable) or not _ordinary_directory(
        artifacts_path
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
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    active_pointer = pack_root / "active.json"
    if _lexically_exists(active_pointer):
        return _managed_pack_paths(pack_root)
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
