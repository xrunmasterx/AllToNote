"""Frozen identity and installation layout of the first practical Document Pack."""

from __future__ import annotations

import os
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
