from __future__ import annotations

import importlib.metadata
import json
import math
import os
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
_REQUEST_KEYS = frozenset(
    {"schema_version", "media_path", "model_path", "cpu_threads"}
)
_EXPECTED_VERSIONS = {
    "faster-whisper": "1.1.1",
    "ctranslate2": "4.6.0",
    "av": "14.2.0",
    "tokenizers": "0.21.1",
}
_MAXIMUM_REQUEST_BYTES = 64 * 1024
_MAXIMUM_RESULT_BYTES = 32 * 1024 * 1024
_MAXIMUM_SEGMENTS = 100_000
_MAXIMUM_TEXT_BYTES = 16 * 1024 * 1024


def _is_reparse_or_link(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_path(value: object, *, directory: bool) -> Path:
    if type(value) is not str or not value:
        raise ValueError("worker request path is invalid")
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("worker request path is invalid") from error
    if _is_reparse_or_link(path):
        raise ValueError("worker request path is invalid")
    if (directory and not path.is_dir()) or (not directory and not path.is_file()):
        raise ValueError("worker request path is invalid")
    return path


def _installed_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in _EXPECTED_VERSIONS
    }


def _default_model_factory(model_path: str, **kwargs: object) -> object:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from faster_whisper import WhisperModel

    return WhisperModel(model_size_or_path=model_path, **kwargs)


def transcribe_request(
    request: Mapping[str, object],
    *,
    model_factory: Callable[..., object] = _default_model_factory,
    versions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if (
        type(request) is not dict
        or frozenset(request) != _REQUEST_KEYS
        or request.get("schema_version") != 1
    ):
        raise ValueError("worker request is invalid")
    cpu_threads = request.get("cpu_threads")
    if type(cpu_threads) is not int or not 1 <= cpu_threads <= 32:
        raise ValueError("worker request is invalid")
    media_path = _safe_path(request.get("media_path"), directory=False)
    model_path = _safe_path(request.get("model_path"), directory=True)
    actual_versions = dict(versions or _installed_versions())
    if actual_versions != _EXPECTED_VERSIONS:
        raise ValueError("worker dependency identity is invalid")

    model = model_factory(
        str(model_path),
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
    )
    response = model.transcribe(str(media_path), beam_size=5)  # type: ignore[attr-defined]
    if (
        not isinstance(response, tuple)
        or len(response) != 2
    ):
        raise ValueError("worker transcription result is invalid")
    raw_segments, info = response
    language = getattr(info, "language", None)
    if type(language) is not str or not language.strip():
        language = "und"

    segments: list[dict[str, object]] = []
    total_text_bytes = 0
    previous_start = -1.0
    for raw in raw_segments:
        if len(segments) >= _MAXIMUM_SEGMENTS:
            raise ValueError("worker segment limit exceeded")
        start = getattr(raw, "start", None)
        end = getattr(raw, "end", None)
        text = getattr(raw, "text", None)
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(start) < previous_start
            or float(end) <= float(start)
            or type(text) is not str
            or not text.strip()
        ):
            raise ValueError("worker segment is invalid")
        normalized_text = text.strip()
        total_text_bytes += len(normalized_text.encode("utf-8"))
        if total_text_bytes > _MAXIMUM_TEXT_BYTES:
            raise ValueError("worker segment text limit exceeded")
        previous_start = float(start)
        segments.append(
            {
                "start": float(start),
                "end": float(end),
                "text": normalized_text,
            }
        )
    if not segments:
        raise ValueError("worker transcription result is empty")
    return {
        "schema_version": 1,
        "identity": {
            "worker_protocol_version": 1,
            "engine": "faster-whisper",
            "engine_version": actual_versions["faster-whisper"],
            "ctranslate2_version": actual_versions["ctranslate2"],
            "av_version": actual_versions["av"],
            "tokenizers_version": actual_versions["tokenizers"],
            "model": "small",
            "model_revision": MODEL_REVISION,
            "device": "cpu",
            "compute_type": "int8",
        },
        "language": language.strip(),
        "segments": segments,
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(_MAXIMUM_REQUEST_BYTES + 1)
        if len(payload) > _MAXIMUM_REQUEST_BYTES:
            raise ValueError("worker request is too large")
        request = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non_finite_json")
            ),
        )
        result = transcribe_request(request)
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAXIMUM_RESULT_BYTES:
            raise ValueError("worker result is too large")
    except Exception:
        return 1
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MODEL_REVISION", "main", "transcribe_request"]
