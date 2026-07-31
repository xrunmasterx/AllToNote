from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

from app.adapters.transcription.legacy_transcriber import (
    normalize_legacy_transcript,
)
from app.adapters.video_packs.official_pack_process import (
    minimal_worker_environment,
    run_json_worker,
)
from app.adapters.video_packs.official_video_pack import TRANSCRIBE_CPU
from app.adapters.video_packs.official_video_pack_resolver import (
    ResolvedOfficialVideoPack,
)
from app.adapters.video_packs.transcribe_cpu_worker import MODEL_REVISION
from app.core.domain.video import TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.source import CancellationTokenPort
from app.core.ports.transcript import MediaInput


_EXPECTED_IDENTITY = {
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
_RESULT_KEYS = frozenset({"schema_version", "identity", "language", "segments"})
_SEGMENT_KEYS = frozenset({"start", "end", "text"})
_MAXIMUM_OUTPUT_BYTES = 32 * 1024 * 1024

WorkerRunner = Callable[..., dict[str, object]]


def _result_invalid() -> DomainError:
    return DomainError(
        "transcription_result_invalid",
        ErrorCategory.RECIPE_FAILED,
        "The transcribe-cpu Pack returned an invalid transcript",
    )


class PackedCpuTranscriber:
    def __init__(
        self,
        pack: ResolvedOfficialVideoPack,
        *,
        cpu_threads: int = 8,
        timeout_seconds: int = 1800,
        runner: WorkerRunner = run_json_worker,
        backend_root: Path | None = None,
    ) -> None:
        if (
            not isinstance(pack, ResolvedOfficialVideoPack)
            or pack.pack_id != TRANSCRIBE_CPU.pack_id
            or pack.pack_version != TRANSCRIBE_CPU.pack_version
            or "python" not in pack.entrypoints
            or type(cpu_threads) is not int
            or not 1 <= cpu_threads <= 32
            or type(timeout_seconds) is not int
            or timeout_seconds < 1
        ):
            raise ValueError("transcribe-cpu Pack binding is invalid")
        root = (
            Path(__file__).resolve().parents[3]
            if backend_root is None
            else Path(backend_root).resolve(strict=True)
        )
        if not root.is_dir():
            raise ValueError("Runtime package root is invalid")
        self._pack = pack
        self._python = pack.entrypoints["python"]
        self._model = pack.generation / "models" / "small"
        self._cpu_threads = cpu_threads
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        self._backend_root = root

    @property
    def identity(self) -> str:
        return (
            "faster-whisper/1.1.1/small/cpu-int8"
            f"@{MODEL_REVISION}"
        )

    def transcribe(
        self,
        media: MediaInput,
        token: CancellationTokenPort,
    ) -> TranscriptDocument:
        token.raise_if_cancelled()
        if media.provided_transcript is not None:
            token.raise_if_cancelled()
            return media.provided_transcript
        media_path = media.media_path
        assert media_path is not None
        try:
            resolved_media = media_path.resolve(strict=True)
            resolved_model = self._model.resolve(strict=True)
        except OSError as error:
            raise DomainError(
                "transcription_input_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The frozen media or transcribe-cpu model is unavailable",
            ) from error
        result = self._runner(
            (
                str(self._python),
                "-B",
                "-m",
                "app.adapters.video_packs.transcribe_cpu_worker",
            ),
            {
                "schema_version": 1,
                "media_path": str(resolved_media),
                "model_path": str(resolved_model),
                "cpu_threads": self._cpu_threads,
            },
            cwd=resolved_media.parent,
            environment=minimal_worker_environment(
                overrides={
                    "PYTHONPATH": str(self._backend_root),
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "OMP_NUM_THREADS": str(self._cpu_threads),
                }
            ),
            timeout_seconds=self._timeout_seconds,
            maximum_output_bytes=_MAXIMUM_OUTPUT_BYTES,
            check_cancelled=token.raise_if_cancelled,
        )
        return self._decode_result(result, token)

    @staticmethod
    def _decode_result(
        result: Mapping[str, object],
        token: CancellationTokenPort,
    ) -> TranscriptDocument:
        if (
            type(result) is not dict
            or frozenset(result) != _RESULT_KEYS
            or result.get("schema_version") != 1
            or result.get("identity") != _EXPECTED_IDENTITY
            or type(result.get("language")) is not str
            or not result["language"].strip()
        ):
            raise _result_invalid()
        segments = result.get("segments")
        if (
            not isinstance(segments, Sequence)
            or isinstance(segments, (str, bytes, bytearray))
            or not segments
        ):
            raise _result_invalid()
        normalized_segments: list[dict[str, object]] = []
        for value in segments:
            token.raise_if_cancelled()
            if type(value) is not dict or frozenset(value) != _SEGMENT_KEYS:
                raise _result_invalid()
            normalized_segments.append(value)
        try:
            return normalize_legacy_transcript(
                SimpleNamespace(
                    language=result["language"],
                    segments=normalized_segments,
                ),
                check_cancelled=token.raise_if_cancelled,
            )
        except DomainError as error:
            if error.category is ErrorCategory.CANCELLED:
                raise
            raise _result_invalid() from error


__all__ = ["PackedCpuTranscriber"]
