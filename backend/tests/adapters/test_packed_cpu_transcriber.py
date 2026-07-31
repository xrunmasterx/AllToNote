from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.video_packs.official_video_pack import TRANSCRIBE_CPU
from app.adapters.video_packs.official_video_pack_resolver import (
    ResolvedOfficialVideoPack,
)
from app.adapters.video_packs.packed_cpu_transcriber import (
    PackedCpuTranscriber,
)
from app.adapters.video_packs.transcribe_cpu_worker import MODEL_REVISION
from app.core.errors import DomainError
from app.core.ports.transcript import MediaInput


class _Token:
    def __init__(self) -> None:
        self.calls = 0

    def raise_if_cancelled(self) -> None:
        self.calls += 1


def _resolved(tmp_path: Path) -> ResolvedOfficialVideoPack:
    generation = tmp_path / "generation"
    python = generation / "python" / "python.exe"
    model = generation / "models" / "small"
    python.parent.mkdir(parents=True)
    model.mkdir(parents=True)
    python.write_bytes(b"python")
    return ResolvedOfficialVideoPack(
        pack_id=TRANSCRIBE_CPU.pack_id,
        pack_version=TRANSCRIBE_CPU.pack_version,
        platform="windows-x86_64",
        manifest_sha256="sha256:" + "a" * 64,
        generation=generation.resolve(),
        entrypoints={"python": python.resolve()},
    )


def _result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "identity": {
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
        },
        "language": "zh",
        "segments": [
            {"start": 0.0, "end": 1.25, "text": "第一段"},
            {"start": 1.25, "end": 2.5, "text": "second"},
        ],
    }


def test_adapter_invokes_only_frozen_pack_worker(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path)
    media = tmp_path / "attempt" / "audio.mp3"
    media.parent.mkdir()
    media.write_bytes(b"audio")
    captured: dict[str, object] = {}

    def runner(command, request, **kwargs):
        captured.update(command=command, request=request, **kwargs)
        return _result()

    token = _Token()
    adapter = PackedCpuTranscriber(
        resolved,
        cpu_threads=8,
        runner=runner,
        backend_root=Path(__file__).resolve().parents[2],
    )
    transcript = adapter.transcribe(MediaInput(media_path=media), token)

    assert captured["command"][:3] == (
        str(resolved.entrypoints["python"]),
        "-B",
        "-m",
    )
    assert captured["request"] == {
        "schema_version": 1,
        "media_path": str(media.resolve()),
        "model_path": str((resolved.generation / "models" / "small").resolve()),
        "cpu_threads": 8,
    }
    assert captured["check_cancelled"] == token.raise_if_cancelled
    assert token.calls >= 1
    assert transcript.language == "zh"
    assert tuple(segment.text for segment in transcript.segments) == (
        "第一段",
        "second",
    )
    assert adapter.identity.endswith(f"@{MODEL_REVISION}")


def test_adapter_rejects_worker_identity_drift(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path)
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"audio")
    result = _result()
    result["identity"] = {**result["identity"], "compute_type": "float32"}
    adapter = PackedCpuTranscriber(
        resolved,
        runner=lambda *_args, **_kwargs: result,
        backend_root=Path(__file__).resolve().parents[2],
    )

    with pytest.raises(DomainError) as caught:
        adapter.transcribe(MediaInput(media_path=media), _Token())

    assert caught.value.code == "transcription_result_invalid"


def test_provided_transcript_fast_path_does_not_call_worker(tmp_path: Path) -> None:
    from app.core.domain.video import TranscriptDocument, TranscriptSegment

    transcript = TranscriptDocument(
        "en",
        (TranscriptSegment("seg_000001", 0, 1000, "source"),),
    )
    adapter = PackedCpuTranscriber(
        _resolved(tmp_path),
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker called")
        ),
        backend_root=Path(__file__).resolve().parents[2],
    )

    assert (
        adapter.transcribe(MediaInput(provided_transcript=transcript), _Token())
        is transcript
    )
