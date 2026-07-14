from __future__ import annotations

import json
import math
from collections.abc import Iterable

from app.core.errors import DomainError, ErrorCategory


def _json_invalid() -> DomainError:
    return DomainError(
        "portable_json_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Portable JSON value is invalid",
    )


def _validate_json_value(value: object) -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise _json_invalid()
    if type(value) is list:
        for item in value:
            _validate_json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _json_invalid()
            _validate_json_value(item)
        return
    raise _json_invalid()


def encode_json(value: object) -> bytes:
    try:
        _validate_json_value(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except DomainError:
        raise
    except MemoryError:
        raise
    except (RecursionError, TypeError, UnicodeError, ValueError):
        raise _json_invalid() from None
    return encoded + b"\n"


def encode_ndjson(records: Iterable[object]) -> bytes:
    if isinstance(records, (bytes, bytearray, str)):
        raise _json_invalid()
    try:
        iterator = iter(records)
    except MemoryError:
        raise
    except Exception:
        raise _json_invalid() from None
    encoded: list[bytes] = []
    while True:
        try:
            record = next(iterator)
        except StopIteration:
            break
        except MemoryError:
            raise
        except Exception:
            raise _json_invalid() from None
        encoded.append(encode_json(record))
    return b"".join(encoded)


def encode_utf8_lf(text: str) -> bytes:
    if not isinstance(text, str):
        raise DomainError(
            "portable_text_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Portable text must be a string",
        )
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    try:
        return normalized.encode("utf-8")
    except UnicodeError:
        raise DomainError(
            "portable_text_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Portable text is not valid UTF-8 text",
        ) from None
