from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path

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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(output: Path, payload: dict[str, object]) -> None:
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _failure(output: Path, problem: str, component: str) -> int:
    _write(
        output,
        {
            "schema_version": 1,
            "ok": False,
            "problem": problem,
            "component": component,
        },
    )
    return 1


def _matches(path: Path, *, size: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == size and _sha256(path) == sha256


def main() -> int:
    args = _arguments()
    locked_distributions = {
        "docling-slim": DOCLING_SLIM_VERSION,
        "scipy": SCIPY_VERSION,
        "torch": TORCH_VERSION,
        "numpy": NUMPY_VERSION,
        "opencv-python-headless": OPENCV_HEADLESS_VERSION,
    }
    for distribution, expected in locked_distributions.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            return _failure(args.output, "dependency_missing", distribution)
        if actual != expected:
            return _failure(args.output, "dependency_version", distribution)

    for component, module in (
        ("scipy", "scipy"),
        ("torch", "torch"),
        ("numpy", "numpy"),
        ("opencv-python-headless", "cv2"),
        ("docling-slim", "docling.document_converter"),
    ):
        try:
            importlib.import_module(module)
        except Exception:
            return _failure(args.output, "dependency_import", component)

    try:
        model_root = (
            args.artifacts_path.resolve(strict=True) / LAYOUT_MODEL_DIRECTORY
        )
        layout_weights = model_root / "model.safetensors"
        revision_marker = (
            model_root
            / ".cache"
            / "huggingface"
            / "trees"
            / f"{LAYOUT_MODEL_REVISION}.json"
        )
        if not model_root.is_dir() or not revision_marker.is_file():
            return _failure(args.output, "model_revision", LAYOUT_MODEL_DIRECTORY)
        if not _matches(
            layout_weights,
            size=LAYOUT_MODEL_WEIGHTS_SIZE,
            sha256=LAYOUT_MODEL_WEIGHTS_SHA256,
        ):
            return _failure(args.output, "model_weights", LAYOUT_MODEL_DIRECTORY)

        table_root = args.artifacts_path.resolve(strict=True) / TABLE_MODEL_DIRECTORY
        table_revision_marker = (
            table_root
            / ".cache"
            / "huggingface"
            / "trees"
            / f"{TABLE_MODEL_REVISION}.json"
        )
        if not table_root.is_dir() or not table_revision_marker.is_file():
            return _failure(args.output, "model_revision", TABLE_MODEL_DIRECTORY)
        if not _matches(
            table_root / TABLE_MODEL_WEIGHTS_RELATIVE_PATH,
            size=TABLE_MODEL_WEIGHTS_SIZE,
            sha256=TABLE_MODEL_WEIGHTS_SHA256,
        ) or not _matches(
            table_root / TABLE_MODEL_CONFIG_RELATIVE_PATH,
            size=TABLE_MODEL_CONFIG_SIZE,
            sha256=TABLE_MODEL_CONFIG_SHA256,
        ):
            return _failure(args.output, "model_weights", TABLE_MODEL_DIRECTORY)
    except OSError:
        return _failure(args.output, "model_unavailable", LAYOUT_MODEL_DIRECTORY)

    _write(
        args.output,
        {
            "schema_version": 1,
            "ok": True,
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
