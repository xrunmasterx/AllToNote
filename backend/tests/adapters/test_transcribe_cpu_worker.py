from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.adapters.video_packs.transcribe_cpu_worker import (
    MODEL_REVISION,
    transcribe_request,
)


@dataclass
class _Segment:
    start: float
    end: float
    text: str


@dataclass
class _Info:
    language: str


class _Model:
    def transcribe(self, media_path: str, *, beam_size: int):
        assert media_path.endswith("audio.mp3")
        assert beam_size == 5
        return (
            iter(
                (
                    _Segment(0.0, 1.25, " 第一段 "),
                    _Segment(1.25, 2.5, "second"),
                )
            ),
            _Info("zh"),
        )


def test_worker_forces_frozen_cpu_int8_model(
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"audio")
    model = tmp_path / "small"
    model.mkdir()
    captured: dict[str, object] = {}

    def factory(model_path: str, **kwargs):
        captured.update(model_path=model_path, **kwargs)
        return _Model()

    result = transcribe_request(
        {
            "schema_version": 1,
            "media_path": str(media),
            "model_path": str(model),
            "cpu_threads": 8,
        },
        model_factory=factory,
        versions={
            "faster-whisper": "1.1.1",
            "ctranslate2": "4.6.0",
            "av": "14.2.0",
            "tokenizers": "0.21.1",
        },
    )

    assert captured == {
        "model_path": str(model.resolve()),
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": 8,
    }
    assert result["identity"] == {
        "worker_protocol_version": 1,
        "engine": "faster-whisper",
        "engine_version": "1.1.1",
        "ctranslate2_version": "4.6.0",
        "av_version": "14.2.0",
        "tokenizers_version": "0.21.1",
        "model": "small",
        "model_revision": MODEL_REVISION,
        "device": "cpu",
        "compute_type": "int8",
    }
    assert result["language"] == "zh"
    assert result["segments"] == [
        {"start": 0.0, "end": 1.25, "text": "第一段"},
        {"start": 1.25, "end": 2.5, "text": "second"},
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {
            "schema_version": 2,
            "media_path": "x",
            "model_path": "y",
            "cpu_threads": 1,
        },
        {
            "schema_version": 1,
            "media_path": "x",
            "model_path": "y",
            "cpu_threads": 0,
        },
    ),
)
def test_worker_rejects_malformed_request(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="request"):
        transcribe_request(payload, model_factory=lambda *_args, **_kwargs: _Model())


def test_worker_rejects_invalid_or_unbounded_segments(tmp_path: Path) -> None:
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"audio")
    model = tmp_path / "small"
    model.mkdir()

    class InvalidModel:
        def transcribe(self, _media_path: str, *, beam_size: int):
            del beam_size
            return iter((_Segment(2.0, 1.0, "bad"),)), _Info("en")

    with pytest.raises(ValueError, match="segment"):
        transcribe_request(
            {
                "schema_version": 1,
                "media_path": str(media),
                "model_path": str(model),
                "cpu_threads": 1,
            },
            model_factory=lambda *_args, **_kwargs: InvalidModel(),
            versions={
                "faster-whisper": "1.1.1",
                "ctranslate2": "4.6.0",
                "av": "14.2.0",
                "tokenizers": "0.21.1",
            },
        )


def test_worker_result_is_json_safe(tmp_path: Path) -> None:
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"audio")
    model = tmp_path / "small"
    model.mkdir()
    result = transcribe_request(
        {
            "schema_version": 1,
            "media_path": str(media),
            "model_path": str(model),
            "cpu_threads": 1,
        },
        model_factory=lambda *_args, **_kwargs: _Model(),
        versions={
            "faster-whisper": "1.1.1",
            "ctranslate2": "4.6.0",
            "av": "14.2.0",
            "tokenizers": "0.21.1",
        },
    )
    json.dumps(result, allow_nan=False)
