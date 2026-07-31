from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.core.domain.ids import sha256_digest
from app.core.domain.video import GeneratedVideoDraft, ScreenshotPolicy, ScreenshotRequest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import ExternalOperation, ExternalOutcome
from app.core.portable.jsonio import encode_json
from app.core.portable.identity import is_executor_identity
from app.core.ports.model import KnowledgeModelRequest
from app.core.ports.source import CancellationTokenPort
from app.core.recipes.video.chunking import TranscriptChunk, plan_transcript_chunks
from app.core.recipes.video.citation_parser import ParsedModelOutput, parse_model_output
from app.core.recipes.video.prompt import build_video_prompt


_SAFE_OPERATION_ID = re.compile(r"op_[0-9A-Za-z-]+\Z")
_SAFE_PROVIDER_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESULT_SCHEMA = "alltonote.model-chunk-result.v1"


def _model_error(code: str, category: ErrorCategory, message: str) -> DomainError:
    return DomainError(code, category, message)


class LegacyKnownRetryableModelFailure(Exception):
    """A provider explicitly returned a known, retryable failure."""


class LegacyReturnedInvalidResponse(Exception):
    """The provider returned, but its response could not satisfy the bridge DTO."""


@dataclass(frozen=True)
class LegacyModelResponse:
    """Sanitized result of exactly one logical provider request."""

    markdown: str
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    actual_model: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.markdown, str) or not self.markdown.strip():
            raise _model_error(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Model response must contain Markdown",
            )
        if self.provider_request_id is not None and (
            not isinstance(self.provider_request_id, str)
            or _SAFE_PROVIDER_REQUEST_ID.fullmatch(self.provider_request_id) is None
        ):
            raise _model_error(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Model provider request ID is invalid",
            )
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise _model_error(
                    "model_response_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "Model token usage is invalid",
                )
        if self.actual_model is not None and (
            not is_executor_identity(self.actual_model)
        ):
            raise _model_error(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Actual model identity is invalid",
            )
        if (
            not isinstance(self.warnings, tuple)
            or any(
                type(warning) is not str or not warning.strip()
                for warning in self.warnings
            )
            or len(self.warnings) != len(set(self.warnings))
        ):
            raise _model_error(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Model response warnings are invalid",
            )


class LegacyCompletionBridge(Protocol):
    """A primitive where one call is one logical provider request.

    Production facades must disable SDK retries themselves. This adapter neither
    imports nor wraps the legacy multi-call ``GPT.summarize`` workflow.
    """

    def complete_once(
        self,
        prompt: str,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> LegacyModelResponse: ...


@dataclass(frozen=True)
class LegacyModelCapabilities:
    screenshot_requests: bool = False


@dataclass(frozen=True)
class LegacyModelBinding:
    provider_kind: str
    model_identity: str
    bridge: LegacyCompletionBridge | None = field(repr=False, compare=False)
    capabilities: LegacyModelCapabilities

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_kind, str)
            or not is_executor_identity(self.provider_kind)
            or not is_executor_identity(self.model_identity)
            or not isinstance(self.capabilities, LegacyModelCapabilities)
        ):
            raise _model_error(
                "model_binding_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Model binding is invalid",
            )


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

    def fail(self, operation_id: str, *, summary_json: str) -> ExternalOperation: ...

    def unknown(self, operation_id: str, *, summary_json: str) -> ExternalOperation: ...


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


@dataclass(frozen=True)
class _StoredModelResult:
    relative_path: str
    sha256: str


class ModelChunkResultStore:
    """Attempt-private durable anchor for a validated model response."""

    def __init__(self, root: Path) -> None:
        requested_root = Path(root).absolute()
        if _path_chain_has_reparse_point(requested_root):
            raise self._unavailable()
        try:
            requested_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise self._unavailable() from None
        if _path_chain_has_reparse_point(requested_root) or not requested_root.is_dir():
            raise self._unavailable()
        try:
            self._root = requested_root.resolve(strict=True)
        except OSError:
            raise self._unavailable() from None

    def save(
        self,
        operation_id: str,
        response: LegacyModelResponse,
    ) -> _StoredModelResult:
        target = self._target(operation_id)
        self._check_root()
        payload = self._encode(response)
        if target.exists():
            if _is_reparse_point(target) or not target.is_file():
                raise self._unavailable()
            try:
                existing = target.read_bytes()
            except OSError:
                raise self._unavailable() from None
            self._check_root()
            if existing != payload:
                raise DomainError(
                    "external_result_conflict",
                    ErrorCategory.CONFLICT,
                    "Anchored model result conflicts with the existing result",
                )
            return _StoredModelResult(target.name, sha256_digest(payload))

        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._check_root()
            if _is_reparse_point(temporary) or not temporary.is_file():
                raise self._unavailable()
            os.replace(temporary, target)
            self._check_root()
            if _is_reparse_point(target) or not target.is_file():
                raise self._unavailable()
        except OSError:
            raise self._unavailable() from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return _StoredModelResult(target.name, sha256_digest(payload))

    def load(self, operation_id: str, summary_json: str) -> LegacyModelResponse:
        try:
            summary = json.loads(summary_json)
            if type(summary) is not dict or set(summary) != {"operation", "result"}:
                raise TypeError
            if summary["operation"] != "generate-model-chunk":
                raise TypeError
            result = summary["result"]
            if type(result) is not dict or set(result) != {"path", "sha256"}:
                raise TypeError
            expected_path = self._target(operation_id)
            self._check_root()
            if result["path"] != expected_path.name or type(result["sha256"]) is not str:
                raise TypeError
            if _is_reparse_point(expected_path) or not expected_path.is_file():
                raise TypeError
            payload = expected_path.read_bytes()
            self._check_root()
            if sha256_digest(payload) != result["sha256"]:
                raise TypeError
            return self._decode(payload)
        except MemoryError:
            raise
        except (DomainError, OSError, TypeError, UnicodeError, ValueError):
            raise self._unavailable() from None

    def _target(self, operation_id: str) -> Path:
        if type(operation_id) is not str or _SAFE_OPERATION_ID.fullmatch(operation_id) is None:
            raise self._unavailable()
        self._check_root()
        target = self._root / f"{operation_id}.model.json"
        if not target.resolve(strict=False).is_relative_to(self._root):
            raise self._unavailable()
        return target

    def _check_root(self) -> None:
        if _path_chain_has_reparse_point(self._root) or not self._root.is_dir():
            raise self._unavailable()
        try:
            if self._root.resolve(strict=True) != self._root:
                raise self._unavailable()
        except OSError:
            raise self._unavailable() from None

    @staticmethod
    def _encode(response: LegacyModelResponse) -> bytes:
        return encode_json(
            {
                "actual_model": response.actual_model,
                "input_tokens": response.input_tokens,
                "markdown": response.markdown,
                "output_tokens": response.output_tokens,
                "provider_request_id": response.provider_request_id,
                "schema": _RESULT_SCHEMA,
            }
        )

    @staticmethod
    def _decode(payload: bytes) -> LegacyModelResponse:
        try:
            value = json.loads(payload)
            if type(value) is not dict or set(value) != {
                "actual_model",
                "input_tokens",
                "markdown",
                "output_tokens",
                "provider_request_id",
                "schema",
            }:
                raise TypeError
            if value["schema"] != _RESULT_SCHEMA:
                raise TypeError
            return LegacyModelResponse(
                markdown=value["markdown"],
                provider_request_id=value["provider_request_id"],
                input_tokens=value["input_tokens"],
                output_tokens=value["output_tokens"],
                actual_model=value["actual_model"],
            )
        except MemoryError:
            raise
        except (DomainError, TypeError, UnicodeError, ValueError):
            raise ModelChunkResultStore._unavailable() from None

    @staticmethod
    def _unavailable() -> DomainError:
        return DomainError(
            "external_result_unavailable",
            ErrorCategory.CONFLICT,
            "The anchored external result is unavailable or invalid",
        )


@dataclass(frozen=True)
class ModelExecutionBinding:
    guard: _OperationGuard = field(repr=False, compare=False)
    result_store: ModelChunkResultStore = field(repr=False, compare=False)
    job_id: str
    step_id: str
    attempt_id: str


class LegacyKnowledgeModelAdapter:
    def __init__(
        self,
        *,
        model: LegacyModelBinding,
        execution: ModelExecutionBinding,
        max_prompt_bytes: int,
    ) -> None:
        self._model = model
        self._execution = execution
        self._max_prompt_bytes = max_prompt_bytes

    def generate(
        self,
        request: KnowledgeModelRequest,
        token: CancellationTokenPort,
    ) -> GeneratedVideoDraft:
        bridge = self._model.bridge
        if bridge is None or not callable(getattr(bridge, "complete_once", None)):
            raise _model_error(
                "model_bridge_required",
                ErrorCategory.POLICY_DENIED,
                "A single-request model bridge must be explicitly provided",
            )
        if (
            request.screenshot_policy is ScreenshotPolicy.ON_DEMAND
            and not self._model.capabilities.screenshot_requests
        ):
            raise _model_error(
                "model_screenshot_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "The selected model binding cannot request screenshots",
            )

        plan = plan_transcript_chunks(request, max_prompt_bytes=self._max_prompt_bytes)
        transcript_digest = self._transcript_digest(request)
        markdown_parts: list[str] = []
        citations: list[str] = []
        screenshots: list[ScreenshotRequest] = []
        responses: list[LegacyModelResponse] = []
        for chunk in plan.chunks:
            token.raise_if_cancelled()
            response, parsed = self._generate_chunk(
                request,
                chunk,
                transcript_digest,
                bridge,
                token,
            )
            markdown_parts.append(parsed.markdown)
            citations.extend(parsed.cited_segment_ids)
            screenshots.extend(parsed.screenshot_requests)
            responses.append(response)

        usage, warnings = self._usage(responses)
        return GeneratedVideoDraft(
            markdown="\n\n".join(markdown_parts),
            cited_segment_ids=tuple(citations),
            screenshot_requests=tuple(screenshots),
            model_identity=self._model.model_identity,
            usage=usage,
            warnings=warnings,
        )

    def _generate_chunk(
        self,
        request: KnowledgeModelRequest,
        chunk: TranscriptChunk,
        transcript_digest: str,
        bridge: LegacyCompletionBridge,
        token: CancellationTokenPort,
    ) -> tuple[LegacyModelResponse, ParsedModelOutput]:
        prompt = build_video_prompt(request, chunk.segments)
        prompt_sha256 = sha256_digest(prompt)
        request_hash = self._request_hash(
            request,
            chunk,
            transcript_digest,
            prompt_sha256,
        )
        prepared_summary = self._prepared_summary(
            request,
            chunk,
            transcript_digest,
            prompt_sha256,
        )
        binding = self._execution
        operation = binding.guard.prepare(
            job_id=binding.job_id,
            step_id=binding.step_id,
            attempt_id=binding.attempt_id,
            provider=self._model.provider_kind,
            request_hash=request_hash,
            operation_idempotency_key=None,
            summary_json=prepared_summary,
            max_attempts=2,
        )
        if operation.outcome is ExternalOutcome.SUCCEEDED:
            response = binding.result_store.load(
                operation.operation_id,
                operation.summary_json,
            )
            return response, self._validate_returned_response(request, chunk, response)

        token.raise_if_cancelled()
        binding.guard.start(operation.operation_id)
        try:
            response = bridge.complete_once(
                prompt,
                check_cancelled=token.raise_if_cancelled,
            )
        except LegacyKnownRetryableModelFailure:
            binding.guard.fail(operation.operation_id, summary_json=prepared_summary)
            raise _model_error(
                "model_generation_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "The model provider rejected the request",
            ) from None
        except LegacyReturnedInvalidResponse:
            self._succeed_without_result(operation)
            raise _model_error(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "The model provider returned an invalid response",
            ) from None
        except DomainError as error:
            if error.category is ErrorCategory.CANCELLED:
                binding.guard.unknown(
                    operation.operation_id,
                    summary_json=prepared_summary,
                )
                raise
            binding.guard.unknown(
                operation.operation_id,
                summary_json=prepared_summary,
            )
            raise _model_error(
                "external_outcome_unknown",
                ErrorCategory.CONFLICT,
                "The model provider outcome is unknown",
            ) from None
        except Exception:
            binding.guard.unknown(operation.operation_id, summary_json=prepared_summary)
            raise _model_error(
                "external_outcome_unknown",
                ErrorCategory.CONFLICT,
                "The model provider outcome is unknown",
            ) from None

        try:
            parsed = self._validate_returned_response(request, chunk, response)
        except (DomainError, TypeError):
            self._succeed_without_result(operation)
            raise
        try:
            stored = binding.result_store.save(operation.operation_id, response)
        except DomainError:
            self._succeed_without_result(operation)
            raise
        binding.guard.succeed(
            operation.operation_id,
            provider_request_id=response.provider_request_id,
            summary_json=self._success_summary(stored),
        )
        token.raise_if_cancelled()
        return response, parsed

    def _validate_returned_response(
        self,
        request: KnowledgeModelRequest,
        chunk: TranscriptChunk,
        response: LegacyModelResponse,
    ) -> ParsedModelOutput:
        if not isinstance(response, LegacyModelResponse):
            raise _model_error(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Model bridge returned an invalid response contract",
            )
        if (
            response.actual_model is not None
            and response.actual_model != self._model.model_identity
        ):
            raise _model_error(
                "model_identity_mismatch",
                ErrorCategory.RECIPE_FAILED,
                "Actual model identity does not match the frozen binding",
            )
        return parse_model_output(
            response.markdown,
            known_segment_ids=tuple(segment.segment_id for segment in chunk.segments),
            allow_screenshots=request.screenshot_policy is ScreenshotPolicy.ON_DEMAND,
        )

    def _succeed_without_result(self, operation: ExternalOperation) -> None:
        self._execution.guard.succeed(
            operation.operation_id,
            provider_request_id=None,
            summary_json='{"operation":"generate-model-chunk","result":null}',
        )

    def _request_hash(
        self,
        request: KnowledgeModelRequest,
        chunk: TranscriptChunk,
        transcript_digest: str,
        prompt_sha256: str,
    ) -> str:
        return sha256_digest(
            encode_json(
                {
                    "chunk_ordinal": chunk.ordinal,
                    "max_prompt_bytes": self._max_prompt_bytes,
                    "model_identity": self._model.model_identity,
                    "output_language": request.output_language,
                    "provider_kind": self._model.provider_kind,
                    "prompt_sha256": prompt_sha256,
                    "quality_preset": request.quality_preset,
                    "recipe_id": request.recipe_id,
                    "recipe_version": request.recipe_version,
                    "screenshot_policy": request.screenshot_policy.value,
                    "style": request.style,
                    "transcript_digest": transcript_digest,
                }
            )
        )

    def _prepared_summary(
        self,
        request: KnowledgeModelRequest,
        chunk: TranscriptChunk,
        transcript_digest: str,
        prompt_sha256: str,
    ) -> str:
        return encode_json(
            {
                "chunk_ordinal": chunk.ordinal,
                "model_identity": self._model.model_identity,
                "operation": "generate-model-chunk",
                "prompt_sha256": prompt_sha256,
                "recipe_version": request.recipe_version,
                "transcript_digest": transcript_digest,
            }
        ).decode("utf-8").rstrip("\n")

    @staticmethod
    def _success_summary(stored: _StoredModelResult) -> str:
        return encode_json(
            {
                "operation": "generate-model-chunk",
                "result": {"path": stored.relative_path, "sha256": stored.sha256},
            }
        ).decode("utf-8").rstrip("\n")

    @staticmethod
    def _transcript_digest(request: KnowledgeModelRequest) -> str:
        return sha256_digest(
            encode_json(
                {
                    "language": request.transcript.language,
                    "segments": [
                        {
                            "end_ms": segment.end_ms,
                            "segment_id": segment.segment_id,
                            "start_ms": segment.start_ms,
                            "text": segment.text,
                        }
                        for segment in request.transcript.segments
                    ],
                }
            )
        )

    @staticmethod
    def _usage(
        responses: list[LegacyModelResponse],
    ) -> tuple[dict[str, int], tuple[str, ...]]:
        if any(
            response.input_tokens is None or response.output_tokens is None
            for response in responses
        ):
            return {}, ("legacy_model_usage_unavailable",)
        return (
            {
                "input_tokens": sum(response.input_tokens or 0 for response in responses),
                "output_tokens": sum(response.output_tokens or 0 for response in responses),
            },
            (),
        )


__all__ = [
    "LegacyCompletionBridge",
    "LegacyKnowledgeModelAdapter",
    "LegacyKnownRetryableModelFailure",
    "LegacyModelBinding",
    "LegacyModelCapabilities",
    "LegacyModelResponse",
    "LegacyReturnedInvalidResponse",
    "ModelChunkResultStore",
    "ModelExecutionBinding",
]
