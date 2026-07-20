from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from typing import TextIO

from app.cli.contracts import ApplicationResult, CLI_PROTOCOL_VERSION, CliError
from app.core.sensitive_identifiers import is_sensitive_identifier


_OMIT = object()
_SENSITIVE_OUTPUT_FIELDS = frozenset(
    {
        "credential_path",
        "credential_store_path",
        "prompt",
        "provider_raw",
        "raw_provider_response",
        "system_prompt",
        "user_prompt",
    }
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|cookie|api[_-]?key|access[_-]?token|password|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_COMMON_TOKEN_VALUE = re.compile(
    r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b",
    re.IGNORECASE,
)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:\\|\\\\)[^\r\n\t\"']+")


def _is_absolute_path(value: str) -> bool:
    if "://" in value:
        return False
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()


def _redact_text(value: str, *, show_paths: bool = False) -> str:
    redacted = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", value)
    redacted = _BEARER_VALUE.sub("Bearer [REDACTED]", redacted)
    redacted = _COMMON_TOKEN_VALUE.sub("[REDACTED]", redacted)
    if show_paths:
        return redacted
    if _is_absolute_path(redacted):
        return "[PATH_REDACTED]"
    return _WINDOWS_PATH.sub("[PATH_REDACTED]", redacted)


def _is_sensitive_output_field(identifier: str) -> bool:
    normalized = identifier.casefold().replace("-", "_")
    return is_sensitive_identifier(identifier) or normalized in _SENSITIVE_OUTPUT_FIELDS


def _project_mapping(
    value: Mapping[object, object], *, show_paths: bool = False
) -> dict[str, object]:
    projected: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            continue
        if _is_sensitive_output_field(key):
            projected[key] = "[REDACTED]"
            continue
        safe_item = _project_value(item, show_paths=show_paths)
        if safe_item is not _OMIT:
            projected[key] = safe_item
    return projected


def _project_value(value: object, *, show_paths: bool = False) -> object:
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        return _redact_text(value, show_paths=show_paths)
    if type(value) is float:
        return value if math.isfinite(value) else _OMIT
    if isinstance(value, Mapping):
        return _project_mapping(value, show_paths=show_paths)
    if isinstance(value, (list, tuple)):
        projected = []
        for item in value:
            safe_item = _project_value(item, show_paths=show_paths)
            if safe_item is not _OMIT:
                projected.append(safe_item)
        return projected
    return _OMIT


def _safe_mapping(
    value: Mapping[str, object], *, show_paths: bool = False
) -> dict[str, object]:
    try:
        projected = _project_mapping(value, show_paths=show_paths)
        json.dumps(projected, ensure_ascii=False, allow_nan=False)
        return projected
    except Exception:
        return {}


def _safe_sequence(
    value: Sequence[Mapping[str, object]],
    *,
    show_paths: bool = False,
) -> list[dict[str, object]]:
    return [_safe_mapping(item, show_paths=show_paths) for item in value]


def _safe_warnings(
    warnings: Sequence[str], *, show_paths: bool = False
) -> list[str]:
    try:
        return [_redact_text(warning, show_paths=show_paths) for warning in warnings]
    except Exception:
        return []


def _error_payload(error: CliError, *, show_paths: bool = False) -> dict[str, object]:
    return {
        "code": error.code,
        "category": error.category,
        "message": _redact_text(error.message, show_paths=show_paths),
        "retryable": error.retryable,
        "next_actions": _safe_warnings(error.next_actions, show_paths=show_paths),
        "details": _safe_mapping(error.details, show_paths=show_paths),
    }


def json_envelope(
    result: ApplicationResult, *, show_paths: bool = False
) -> dict[str, object]:
    return {
        "alltonote_cli_protocol_version": CLI_PROTOCOL_VERSION,
        "ok": result.ok,
        "command": result.command,
        "correlation_id": result.correlation_id,
        "data": _safe_mapping(result.data, show_paths=show_paths),
        "error": (
            _error_payload(result.error, show_paths=show_paths)
            if result.error is not None
            else None
        ),
        "warnings": _safe_warnings(result.warnings, show_paths=show_paths),
        "job": (
            _safe_mapping(result.job, show_paths=show_paths)
            if result.job is not None
            else None
        ),
        "artifacts": _safe_sequence(result.artifacts, show_paths=show_paths),
        "capabilities": _safe_sequence(result.capabilities, show_paths=show_paths),
        "versions": _safe_mapping(result.versions, show_paths=show_paths),
    }


def render_result(
    result: ApplicationResult,
    *,
    json_mode: bool,
    show_paths: bool = False,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    output = stdout or sys.stdout
    diagnostics = stderr or sys.stderr
    if json_mode:
        print(
            json.dumps(
                json_envelope(result, show_paths=show_paths),
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=output,
        )
        return

    if result.ok:
        for line in result.human_lines:
            print(_redact_text(line, show_paths=show_paths), file=output)
    else:
        assert result.error is not None
        print(
            f"Error [{result.error.code}]: "
            f"{_redact_text(result.error.message, show_paths=show_paths)}",
            file=diagnostics,
        )
        for action in result.error.next_actions:
            print(
                f"Action: {_redact_text(action, show_paths=show_paths)}",
                file=diagnostics,
            )
    for warning in result.warnings:
        print(
            f"Warning: {_redact_text(warning, show_paths=show_paths)}",
            file=diagnostics,
        )


def render_json_lines(
    records: Sequence[Mapping[str, object]],
    *,
    stdout: TextIO | None = None,
) -> None:
    output = stdout or sys.stdout
    for record in records:
        print(
            json.dumps(
                _safe_mapping(record),
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=output,
        )


__all__ = ["json_envelope", "render_json_lines", "render_result"]
