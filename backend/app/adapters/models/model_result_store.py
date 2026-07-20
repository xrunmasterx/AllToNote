from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import uuid4

from app.core.application.model_call_coordinator import StoredModelOperationResult
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.jsonio import encode_json
from app.core.ports.model_executor import (
    ModelExecutionResult,
    ModelFinishReason,
)


_SAFE_OPERATION_ID = re.compile(r"op_[0-9A-Za-z-]+\Z")
_RESULT_SCHEMA = "alltonote.model-operation-result.v1"
_OPERATION_NAME = "model-completion"


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


class ModelOperationResultStore:
    """Machine-State store for normalized model results, keyed by operation ID."""

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
        request_hash: str,
        result: ModelExecutionResult,
    ) -> StoredModelOperationResult:
        target = self._target(operation_id)
        self._check_hash(request_hash)
        if not isinstance(result, ModelExecutionResult):
            raise self._unavailable()
        payload = self._encode(request_hash, result)
        self._check_root()
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
            return StoredModelOperationResult(target.name, sha256_digest(payload))

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
        return StoredModelOperationResult(target.name, sha256_digest(payload))

    def load(
        self,
        operation_id: str,
        request_hash: str,
        summary_json: str,
    ) -> ModelExecutionResult:
        try:
            self._check_hash(request_hash)
            summary = json.loads(summary_json)
            if type(summary) is not dict or set(summary) != {
                "operation",
                "result",
                "shard_key",
            }:
                raise TypeError
            if summary["operation"] != _OPERATION_NAME:
                raise TypeError
            result_ref = summary["result"]
            if type(result_ref) is not dict or set(result_ref) != {
                "path",
                "request_hash",
                "sha256",
            }:
                raise TypeError
            expected_path = self._target(operation_id)
            if (
                result_ref["path"] != expected_path.name
                or result_ref["request_hash"] != request_hash
                or type(result_ref["sha256"]) is not str
            ):
                raise TypeError
            self._check_root()
            if _is_reparse_point(expected_path) or not expected_path.is_file():
                raise TypeError
            payload = expected_path.read_bytes()
            self._check_root()
            if sha256_digest(payload) != result_ref["sha256"]:
                raise TypeError
            return self._decode(payload, request_hash)
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
    def _check_hash(request_hash: str) -> None:
        if (
            type(request_hash) is not str
            or not request_hash.startswith("sha256:")
            or len(request_hash) != 71
            or any(
                character not in "0123456789abcdef"
                for character in request_hash.removeprefix("sha256:")
            )
        ):
            raise ModelOperationResultStore._unavailable()

    @staticmethod
    def _encode(request_hash: str, result: ModelExecutionResult) -> bytes:
        return encode_json(
            {
                "actual_model_identity": result.actual_model_identity,
                "finish_reason": result.finish_reason.value,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "provider_request_id": result.provider_request_id,
                "request_hash": request_hash,
                "schema": _RESULT_SCHEMA,
                "text": result.text,
                "warnings": list(result.warnings),
            }
        )

    @staticmethod
    def _decode(payload: bytes, request_hash: str) -> ModelExecutionResult:
        try:
            value = json.loads(payload)
            if type(value) is not dict or set(value) != {
                "actual_model_identity",
                "finish_reason",
                "input_tokens",
                "output_tokens",
                "provider_request_id",
                "request_hash",
                "schema",
                "text",
                "warnings",
            }:
                raise TypeError
            if value["schema"] != _RESULT_SCHEMA or value["request_hash"] != request_hash:
                raise TypeError
            if type(value["warnings"]) is not list:
                raise TypeError
            return ModelExecutionResult(
                text=value["text"],
                actual_model_identity=value["actual_model_identity"],
                input_tokens=value["input_tokens"],
                output_tokens=value["output_tokens"],
                finish_reason=ModelFinishReason(value["finish_reason"]),
                provider_request_id=value["provider_request_id"],
                warnings=tuple(value["warnings"]),
            )
        except MemoryError:
            raise
        except (DomainError, TypeError, UnicodeError, ValueError):
            raise ModelOperationResultStore._unavailable() from None

    @staticmethod
    def _unavailable() -> DomainError:
        return DomainError(
            "external_result_unavailable",
            ErrorCategory.CONFLICT,
            "The anchored model result is unavailable or invalid",
        )


__all__ = ["ModelOperationResultStore"]
