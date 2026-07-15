from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.core.domain.ids import sha256_digest
from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import ExternalOperation, ExternalOutcome
from app.core.portable.jsonio import encode_json
from app.core.ports.source import CancellationTokenPort
from app.core.ports.transcript import MediaInput


class _LegacyTranscriber(Protocol):
    def transcript(self, file_path: str) -> object: ...


class _OperationGuard(Protocol):
    def prepare(self, **fields: object) -> ExternalOperation: ...

    def start(self, operation_id: str) -> ExternalOperation: ...

    def succeed(
        self,
        operation_id: str,
        *,
        provider_request_id: str | None,
        summary_json: str,
    ) -> ExternalOperation: ...

    def fail(
        self,
        operation_id: str,
        *,
        summary_json: str,
    ) -> ExternalOperation: ...

    def unknown(
        self,
        operation_id: str,
        *,
        summary_json: str,
    ) -> ExternalOperation: ...


class ExternalOutcomeUnknownError(Exception):
    """The legacy call may have reached its provider, but no outcome is known."""


class LegacyRemoteCallFailed(Exception):
    """The provider explicitly rejected a call without an ambiguous side effect."""


@dataclass(frozen=True)
class _ProviderSpec:
    module_name: str
    class_name: str
    remote: bool


_PROVIDERS = {
    "bcut": _ProviderSpec("app.transcriber.bcut", "BcutTranscriber", True),
    "fast-whisper": _ProviderSpec(
        "app.transcriber.whisper", "WhisperTranscriber", False
    ),
    "groq": _ProviderSpec("app.transcriber.groq", "GroqTranscriber", True),
    "kuaishou": _ProviderSpec(
        "app.transcriber.kuaishou", "KuaishouTranscriber", True
    ),
    "mlx-whisper": _ProviderSpec(
        "app.transcriber.mlx_whisper_transcriber",
        "MLXWhisperTranscriber",
        False,
    ),
}
_SAFE_OPERATION_ID = re.compile(r"op_[0-9A-Za-z-]+\Z")
_RESULT_SCHEMA = "alltonote.remote-transcript-result.v1"
_PREPARED_SUMMARY = '{"operation":"transcribe"}'


def _transcript_error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.RECIPE_FAILED, message)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except AttributeError:
        return path.is_symlink()
    return bool(attributes & 0x400)


def _path_chain_has_reparse_point(path: Path) -> bool:
    absolute = path.absolute()
    try:
        return any(
            _is_reparse_point(component)
            for component in (absolute, *absolute.parents)
            if component.exists() or component.is_symlink()
        )
    except OSError:
        return True


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        try:
            return value[name]
        except KeyError:
            raise _transcript_error(
                "transcript_segment_invalid",
                f"Transcript segment is missing {name}",
            ) from None
    try:
        return getattr(value, name)
    except AttributeError:
        raise _transcript_error(
            "transcript_segment_invalid",
            f"Transcript segment is missing {name}",
        ) from None


def _milliseconds(value: object, rounding: str) -> int:
    if isinstance(value, bool):
        raise _transcript_error(
            "transcript_segment_invalid",
            "Transcript segment time must be a finite number",
        )
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _transcript_error(
            "transcript_segment_invalid",
            "Transcript segment time must be a finite number",
        ) from None
    if not seconds.is_finite() or seconds < 0:
        raise _transcript_error(
            "transcript_segment_invalid",
            "Transcript segment time must be finite and non-negative",
        )
    return int((seconds * 1000).to_integral_value(rounding=rounding))


def normalize_legacy_transcript(
    response: object,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> TranscriptDocument:
    try:
        raw_segments = getattr(response, "segments")
    except AttributeError:
        raise _transcript_error(
            "transcript_contract_invalid",
            "Legacy transcript response has no segments",
        ) from None
    if not isinstance(raw_segments, Sequence) or isinstance(
        raw_segments, (str, bytes, bytearray)
    ):
        raise _transcript_error(
            "transcript_contract_invalid",
            "Legacy transcript segments must be a sequence",
        )
    if not raw_segments:
        raise _transcript_error(
            "transcript_empty",
            "Transcript must contain at least one segment",
        )

    normalized: list[TranscriptSegment] = []
    previous_start_ms: int | None = None
    for index, legacy_segment in enumerate(raw_segments, start=1):
        if check_cancelled is not None:
            check_cancelled()
        start_ms = _milliseconds(_field(legacy_segment, "start"), ROUND_FLOOR)
        end_ms = _milliseconds(_field(legacy_segment, "end"), ROUND_CEILING)
        text = _field(legacy_segment, "text")
        if not isinstance(text, str) or not text.strip() or end_ms <= start_ms:
            raise _transcript_error(
                "transcript_segment_invalid",
                "Transcript segment must have text and a valid time range",
            )
        if previous_start_ms is not None and start_ms < previous_start_ms:
            raise _transcript_error(
                "transcript_order_invalid",
                "Transcript segments must be ordered by start time",
            )
        normalized.append(
            TranscriptSegment(
                segment_id=f"seg_{index:06d}",
                start_ms=start_ms,
                end_ms=end_ms,
                text=text.strip(),
            )
        )
        previous_start_ms = start_ms

    language = getattr(response, "language", None)
    normalized_language = language.strip() if isinstance(language, str) else ""
    return TranscriptDocument(normalized_language or "und", tuple(normalized))


def normalize_platform_subtitle(
    response: object,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> TranscriptDocument:
    return normalize_legacy_transcript(
        response,
        check_cancelled=check_cancelled,
    )


def _lazy_factory(spec: _ProviderSpec) -> Callable[[], object]:
    def create() -> object:
        module = importlib.import_module(spec.module_name)
        return getattr(module, spec.class_name)()

    return create


@dataclass(frozen=True)
class _StoredTranscript:
    relative_path: str
    sha256: str


class RemoteTranscriptResultStore:
    """Attempt-private result anchor bridging paid success to step checkpointing."""

    def __init__(self, root: Path) -> None:
        requested_root = Path(root).absolute()
        if _path_chain_has_reparse_point(requested_root):
            raise self._unavailable()
        try:
            requested_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise self._unavailable() from None
        if (
            _path_chain_has_reparse_point(requested_root)
            or not requested_root.is_dir()
        ):
            raise self._unavailable()
        try:
            self._root = requested_root.resolve(strict=True)
        except OSError:
            raise self._unavailable() from None

    def save(
        self,
        operation_id: str,
        transcript: TranscriptDocument,
    ) -> _StoredTranscript:
        target = self._target(operation_id)
        payload = self._encode(transcript)
        if target.exists():
            try:
                existing = target.read_bytes()
            except OSError:
                raise self._unavailable() from None
            if existing != payload:
                raise DomainError(
                    "external_result_conflict",
                    ErrorCategory.CONFLICT,
                    "Anchored external result conflicts with the existing result",
                )
            return _StoredTranscript(target.name, sha256_digest(payload))

        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError:
            raise self._unavailable() from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return _StoredTranscript(target.name, sha256_digest(payload))

    def load(
        self,
        operation_id: str,
        summary_json: str,
    ) -> TranscriptDocument:
        try:
            summary = json.loads(summary_json)
            if type(summary) is not dict or set(summary) != {"operation", "result"}:
                raise TypeError
            if summary["operation"] != "transcribe":
                raise TypeError
            result = summary["result"]
            if type(result) is not dict or set(result) != {"path", "sha256"}:
                raise TypeError
            expected_path = self._target(operation_id)
            if result["path"] != expected_path.name:
                raise TypeError
            expected_sha256 = result["sha256"]
            if type(expected_sha256) is not str:
                raise TypeError
            payload = expected_path.read_bytes()
            if sha256_digest(payload) != expected_sha256:
                raise TypeError
            return self._decode(payload)
        except (DomainError, MemoryError):
            raise
        except (OSError, TypeError, UnicodeError, ValueError):
            raise self._unavailable() from None

    def _target(self, operation_id: str) -> Path:
        if (
            type(operation_id) is not str
            or _SAFE_OPERATION_ID.fullmatch(operation_id) is None
        ):
            raise self._unavailable()
        target = self._root / f"{operation_id}.transcript.json"
        if not target.resolve(strict=False).is_relative_to(self._root):
            raise self._unavailable()
        return target

    @staticmethod
    def _encode(transcript: TranscriptDocument) -> bytes:
        return encode_json(
            {
                "language": transcript.language,
                "schema": _RESULT_SCHEMA,
                "segments": [
                    {
                        "end_ms": segment.end_ms,
                        "segment_id": segment.segment_id,
                        "start_ms": segment.start_ms,
                        "text": segment.text,
                    }
                    for segment in transcript.segments
                ],
            }
        )

    @staticmethod
    def _decode(payload: bytes) -> TranscriptDocument:
        try:
            value = json.loads(payload)
            if type(value) is not dict or set(value) != {
                "language",
                "schema",
                "segments",
            }:
                raise TypeError
            if value["schema"] != _RESULT_SCHEMA or type(value["language"]) is not str:
                raise TypeError
            raw_segments = value["segments"]
            if type(raw_segments) is not list:
                raise TypeError
            segments: list[TranscriptSegment] = []
            for item in raw_segments:
                if type(item) is not dict or set(item) != {
                    "end_ms",
                    "segment_id",
                    "start_ms",
                    "text",
                }:
                    raise TypeError
                if (
                    type(item["segment_id"]) is not str
                    or type(item["start_ms"]) is not int
                    or type(item["end_ms"]) is not int
                    or type(item["text"]) is not str
                ):
                    raise TypeError
                segments.append(
                    TranscriptSegment(
                        item["segment_id"],
                        item["start_ms"],
                        item["end_ms"],
                        item["text"],
                    )
                )
            return TranscriptDocument(value["language"], tuple(segments))
        except MemoryError:
            raise
        except (DomainError, TypeError, UnicodeError, ValueError):
            raise RemoteTranscriptResultStore._unavailable() from None

    @staticmethod
    def _unavailable() -> DomainError:
        return DomainError(
            "external_result_unavailable",
            ErrorCategory.CONFLICT,
            "The anchored external result is unavailable or invalid",
        )


@dataclass(frozen=True)
class RemoteTranscriptionBinding:
    guard: _OperationGuard = field(repr=False, compare=False)
    result_store: RemoteTranscriptResultStore = field(repr=False, compare=False)
    job_id: str
    step_id: str
    attempt_id: str
    request_hash: str


class LegacyTranscriberAdapter:
    def __init__(
        self,
        provider_id: str,
        *,
        factories: Mapping[str, Callable[[], object]] | None = None,
        remote_binding: RemoteTranscriptionBinding | None = None,
    ) -> None:
        if provider_id not in _PROVIDERS:
            raise DomainError(
                "transcriber_unsupported",
                ErrorCategory.INVALID_REQUEST,
                "The selected transcriber is not supported",
                {"provider_id": provider_id},
            )
        self._provider_id = provider_id
        self._spec = _PROVIDERS[provider_id]
        self._factories = dict(factories or {})
        self._remote_binding = remote_binding

    def transcribe(
        self,
        media: MediaInput,
        token: CancellationTokenPort,
    ) -> TranscriptDocument:
        token.raise_if_cancelled()
        if media.provided_transcript is not None:
            token.raise_if_cancelled()
            return media.provided_transcript
        if self._spec.remote:
            return self._transcribe_remote(media, token)
        media_path = media.media_path
        assert media_path is not None
        factory = self._factories.get(self._provider_id) or _lazy_factory(self._spec)
        try:
            transcriber = factory()
            token.raise_if_cancelled()
            response = transcriber.transcript(str(media_path))  # type: ignore[attr-defined]
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "transcription_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "The selected transcriber failed",
                {"provider_id": self._provider_id},
            ) from None
        token.raise_if_cancelled()
        if response is None:
            raise _transcript_error(
                "transcript_contract_invalid",
                "Legacy transcriber returned no transcript",
            )
        return normalize_legacy_transcript(
            response,
            check_cancelled=token.raise_if_cancelled,
        )

    def _transcribe_remote(
        self,
        media: MediaInput,
        token: CancellationTokenPort,
    ) -> TranscriptDocument:
        binding = self._remote_binding
        if binding is None:
            raise DomainError(
                "external_operation_context_required",
                ErrorCategory.POLICY_DENIED,
                "Remote transcription requires a durable operation binding",
                {"provider_id": self._provider_id},
            )
        media_path = media.media_path
        assert media_path is not None
        operation = binding.guard.prepare(
            job_id=binding.job_id,
            step_id=binding.step_id,
            attempt_id=binding.attempt_id,
            provider=self._provider_id,
            request_hash=binding.request_hash,
            summary_json=_PREPARED_SUMMARY,
            operation_idempotency_key=None,
        )
        if operation.outcome is ExternalOutcome.SUCCEEDED:
            token.raise_if_cancelled()
            return binding.result_store.load(
                operation.operation_id,
                operation.summary_json,
            )
        factory = self._factories.get(self._provider_id) or _lazy_factory(self._spec)
        try:
            transcriber = factory()
        except Exception:
            raise DomainError(
                "transcriber_load_failed",
                ErrorCategory.POLICY_DENIED,
                "The selected transcriber could not be loaded",
                {"provider_id": self._provider_id},
            ) from None
        token.raise_if_cancelled()
        binding.guard.start(operation.operation_id)
        try:
            response = transcriber.transcript(str(media_path))  # type: ignore[attr-defined]
        except LegacyRemoteCallFailed:
            binding.guard.fail(
                operation.operation_id,
                summary_json=_PREPARED_SUMMARY,
            )
            raise DomainError(
                "transcription_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "The remote transcriber rejected the request",
                {"provider_id": self._provider_id},
            ) from None
        except Exception:
            binding.guard.unknown(
                operation.operation_id,
                summary_json=_PREPARED_SUMMARY,
            )
            raise DomainError(
                "external_outcome_unknown",
                ErrorCategory.CONFLICT,
                "Remote transcription outcome is unknown",
                {"provider_id": self._provider_id},
            ) from None

        try:
            token.raise_if_cancelled()
            transcript = self._normalize_remote_response(
                response,
                check_cancelled=token.raise_if_cancelled,
            )
        except DomainError as error:
            if error.category is ErrorCategory.CANCELLED:
                self._settle_returned_response(
                    binding,
                    operation,
                    response,
                )
            else:
                self._succeed_without_result(binding, operation)
            raise

        try:
            stored = binding.result_store.save(operation.operation_id, transcript)
        except DomainError:
            self._succeed_without_result(binding, operation)
            raise
        binding.guard.succeed(
            operation.operation_id,
            provider_request_id=None,
            summary_json=self._success_summary(stored),
        )
        return transcript

    @staticmethod
    def _normalize_remote_response(
        response: object,
        *,
        check_cancelled: Callable[[], None] | None,
    ) -> TranscriptDocument:
        if response is None:
            raise _transcript_error(
                "transcript_contract_invalid",
                "Legacy transcriber returned no transcript",
            )
        return normalize_legacy_transcript(
            response,
            check_cancelled=check_cancelled,
        )

    @staticmethod
    def _success_summary(stored: _StoredTranscript) -> str:
        return encode_json(
            {
                "operation": "transcribe",
                "result": {
                    "path": stored.relative_path,
                    "sha256": stored.sha256,
                },
            }
        ).decode("utf-8").rstrip("\n")

    @staticmethod
    def _succeed_without_result(
        binding: RemoteTranscriptionBinding,
        operation: ExternalOperation,
    ) -> None:
        binding.guard.succeed(
            operation.operation_id,
            provider_request_id=None,
            summary_json='{"operation":"transcribe","result":null}',
        )

    def _settle_returned_response(
        self,
        binding: RemoteTranscriptionBinding,
        operation: ExternalOperation,
        response: object,
    ) -> None:
        try:
            transcript = self._normalize_remote_response(
                response,
                check_cancelled=None,
            )
            stored = binding.result_store.save(operation.operation_id, transcript)
        except DomainError:
            self._succeed_without_result(binding, operation)
            return
        binding.guard.succeed(
            operation.operation_id,
            provider_request_id=None,
            summary_json=self._success_summary(stored),
        )


__all__ = [
    "ExternalOutcomeUnknownError",
    "LegacyRemoteCallFailed",
    "LegacyTranscriberAdapter",
    "RemoteTranscriptResultStore",
    "RemoteTranscriptionBinding",
    "normalize_legacy_transcript",
    "normalize_platform_subtitle",
]
