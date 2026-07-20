from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.core.errors import DomainError, ErrorCategory
from app.core.ports.source import CancellationTokenPort


_PROVIDER_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class ModelOutputMode(StrEnum):
    TEXT = "text"
    JSON_SCHEMA = "json_schema"


class ModelFinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


def _require_text(value: object, *, code: str, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise DomainError(
            code,
            ErrorCategory.INVALID_REQUEST,
            f"{field_name} must not be empty",
            {"field": field_name},
        )


def _require_positive_int(value: object, *, code: str, field_name: str) -> None:
    if type(value) is not int or value < 1:
        raise DomainError(
            code,
            ErrorCategory.INVALID_REQUEST,
            f"{field_name} must be a positive integer",
            {"field": field_name},
        )


def _require_positive_number(value: object, *, code: str, field_name: str) -> None:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise DomainError(
            code,
            ErrorCategory.INVALID_REQUEST,
            f"{field_name} must be a positive finite number",
            {"field": field_name},
        )


@dataclass(frozen=True)
class ModelExecutionBinding:
    schema_version: int
    provider_type: str
    model_identity: str
    credential_profile_ref: str
    context_window_tokens: int
    max_output_tokens: int
    max_concurrency: int
    supports_structured_output: bool
    supports_temperature: bool
    timeout_seconds: int | float

    def __post_init__(self) -> None:
        code = "model_execution_binding_invalid"
        for field_name in (
            "provider_type",
            "model_identity",
            "credential_profile_ref",
        ):
            _require_text(getattr(self, field_name), code=code, field_name=field_name)
        for field_name in (
            "schema_version",
            "context_window_tokens",
            "max_output_tokens",
            "max_concurrency",
        ):
            _require_positive_int(
                getattr(self, field_name), code=code, field_name=field_name
            )
        if self.max_output_tokens > self.context_window_tokens:
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Maximum output tokens cannot exceed the context window",
            )
        if type(self.supports_structured_output) is not bool or type(
            self.supports_temperature
        ) is not bool:
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Model capabilities must be boolean values",
            )
        _require_positive_number(
            self.timeout_seconds,
            code=code,
            field_name="timeout_seconds",
        )


@dataclass(frozen=True)
class ModelExecutionRequest:
    schema_version: int
    stage_id: str
    stage_version: int
    prompt_id: str
    prompt_version: int
    system_instruction: str = field(repr=False)
    user_content: str = field(repr=False)
    output_mode: ModelOutputMode
    max_output_tokens: int
    timeout_seconds: int | float
    response_schema_json: str | None = field(default=None, repr=False)
    temperature: int | float | None = None

    def __post_init__(self) -> None:
        code = "model_execution_request_invalid"
        for field_name in (
            "stage_id",
            "prompt_id",
            "system_instruction",
            "user_content",
        ):
            _require_text(getattr(self, field_name), code=code, field_name=field_name)
        for field_name in (
            "schema_version",
            "stage_version",
            "prompt_version",
            "max_output_tokens",
        ):
            _require_positive_int(
                getattr(self, field_name), code=code, field_name=field_name
            )
        if not isinstance(self.output_mode, ModelOutputMode):
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Output mode must use the Core model contract",
            )
        if self.temperature is not None and (
            type(self.temperature) not in (int, float)
            or not math.isfinite(self.temperature)
            or self.temperature < 0
        ):
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Temperature must be a non-negative finite number",
            )
        _require_positive_number(
            self.timeout_seconds,
            code=code,
            field_name="timeout_seconds",
        )
        if self.output_mode is ModelOutputMode.TEXT:
            if self.response_schema_json is not None:
                raise DomainError(
                    code,
                    ErrorCategory.INVALID_REQUEST,
                    "Text output must not declare a response schema",
                )
            return
        try:
            response_schema = json.loads(self.response_schema_json or "")
            if type(response_schema) is not dict:
                raise ValueError
            canonical_schema = json.dumps(
                response_schema,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Structured output requires a valid JSON object schema",
            ) from None
        object.__setattr__(self, "response_schema_json", canonical_schema)


@dataclass(frozen=True)
class ModelExecutionResult:
    text: str = field(repr=False)
    actual_model_identity: str
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: ModelFinishReason
    provider_request_id: str | None
    warnings: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def __post_init__(self) -> None:
        code = "model_execution_result_invalid"
        _require_text(self.text, code=code, field_name="text")
        _require_text(
            self.actual_model_identity,
            code=code,
            field_name="actual_model_identity",
        )
        for field_name in ("input_tokens", "output_tokens"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise DomainError(
                    code,
                    ErrorCategory.INVALID_REQUEST,
                    f"{field_name} must be a non-negative integer when available",
                )
        if not isinstance(self.finish_reason, ModelFinishReason):
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Finish reason must use the normalized Core contract",
            )
        if self.provider_request_id is not None and (
            type(self.provider_request_id) is not str
            or _PROVIDER_REQUEST_ID_PATTERN.fullmatch(self.provider_request_id) is None
        ):
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Provider request ID must be a safe opaque identifier",
            )
        warnings = tuple(self.warnings)
        if any(type(warning) is not str or not warning.strip() for warning in warnings):
            raise DomainError(
                code,
                ErrorCategory.INVALID_REQUEST,
                "Warnings must contain non-empty normalized text",
            )
        object.__setattr__(self, "warnings", warnings)


class ModelExecutorPort(Protocol):
    """Boundary for exactly one provider model completion."""

    def complete(
        self,
        request: ModelExecutionRequest,
        token: CancellationTokenPort,
    ) -> ModelExecutionResult: ...


__all__ = [
    "ModelExecutionBinding",
    "ModelExecutionRequest",
    "ModelExecutionResult",
    "ModelExecutorPort",
    "ModelFinishReason",
    "ModelOutputMode",
]
