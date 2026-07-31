from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping
from uuid import UUID


DESCRIPTOR_VERSION = 1
ENGINE_PROTOCOL_VERSION = 1
RUNTIME_COMPATIBILITY_MAJOR = 0
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_FRAME_BYTES = 1024 * 1024

_DESCRIPTOR_KEYS = frozenset(
    {
        "descriptor_version",
        "engine_protocol_version",
        "runtime_major",
        "scope_id",
        "engine_id",
        "pid",
        "process_start_identity",
        "endpoint_kind",
        "endpoint_name",
        "nonce",
        "started_at",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "engine_protocol_version",
        "request_id",
        "method",
        "nonce",
        "params",
    }
)
_RESPONSE_KEYS = frozenset(
    {"engine_protocol_version", "request_id", "ok", "data", "error"}
)
_METHODS = frozenset({"hello", "health", "shutdown"})
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[A-Za-z0-9_-]{43}")


class EngineProtocolError(ValueError):
    def __init__(self, code: str = "engine_protocol_invalid") -> None:
        super().__init__(code)
        self.code = code


def _reject_constant(_value: str) -> None:
    raise EngineProtocolError()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EngineProtocolError()
        result[key] = value
    return result


def _decode_json(payload: bytes, *, maximum: int) -> dict[str, object]:
    if not payload or len(payload) > maximum:
        raise EngineProtocolError()
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (EngineProtocolError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, EngineProtocolError):
            raise
        raise EngineProtocolError() from error
    if type(value) is not dict:
        raise EngineProtocolError()
    return value


def _canonical_bytes(payload: Mapping[str, object], *, newline: bool) -> bytes:
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EngineProtocolError() from error
    return encoded + (b"\n" if newline else b"")


def _string(value: object, *, maximum: int = 512) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise EngineProtocolError()
    return value


def _nonce(value: object) -> str:
    nonce = _string(value, maximum=43)
    if _NONCE.fullmatch(nonce) is None:
        raise EngineProtocolError()
    try:
        decoded = base64.urlsafe_b64decode(nonce + "=")
    except ValueError as error:
        raise EngineProtocolError() from error
    if len(decoded) != 32:
        raise EngineProtocolError()
    return nonce


@dataclass(frozen=True)
class EngineDescriptor:
    descriptor_version: int
    engine_protocol_version: int
    runtime_major: int
    scope_id: str
    engine_id: str
    pid: int
    process_start_identity: str
    endpoint_kind: str
    endpoint_name: str
    nonce: str = field(repr=False)
    started_at: str

    def __post_init__(self) -> None:
        if type(self.descriptor_version) is not int or self.descriptor_version != 1:
            raise EngineProtocolError()
        if (
            type(self.engine_protocol_version) is not int
            or self.engine_protocol_version != ENGINE_PROTOCOL_VERSION
        ):
            raise EngineProtocolError("engine_protocol_incompatible")
        if type(self.runtime_major) is not int or self.runtime_major != 0:
            raise EngineProtocolError("engine_protocol_incompatible")
        if type(self.scope_id) is not str or _HEX_64.fullmatch(self.scope_id) is None:
            raise EngineProtocolError()
        try:
            UUID(_string(self.engine_id, maximum=64))
        except ValueError as error:
            raise EngineProtocolError() from error
        if type(self.pid) is not int or self.pid <= 0:
            raise EngineProtocolError()
        _string(self.process_start_identity, maximum=128)
        if self.endpoint_kind not in {"windows-named-pipe", "unix-domain-socket"}:
            raise EngineProtocolError()
        _string(self.endpoint_name, maximum=512)
        _nonce(self.nonce)
        started_at = _string(self.started_at, maximum=64)
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise EngineProtocolError() from error
        if parsed.tzinfo is None:
            raise EngineProtocolError()

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.payload(), newline=True)

    def payload(self) -> dict[str, object]:
        return {
            "descriptor_version": self.descriptor_version,
            "engine_protocol_version": self.engine_protocol_version,
            "runtime_major": self.runtime_major,
            "scope_id": self.scope_id,
            "engine_id": self.engine_id,
            "pid": self.pid,
            "process_start_identity": self.process_start_identity,
            "endpoint_kind": self.endpoint_kind,
            "endpoint_name": self.endpoint_name,
            "nonce": self.nonce,
            "started_at": self.started_at,
        }

    @classmethod
    def from_bytes(cls, payload: bytes) -> EngineDescriptor:
        value = _decode_json(payload, maximum=MAX_DESCRIPTOR_BYTES)
        if frozenset(value) != _DESCRIPTOR_KEYS:
            raise EngineProtocolError()
        try:
            return cls(**value)
        except TypeError as error:
            raise EngineProtocolError() from error


@dataclass(frozen=True)
class EngineRequest:
    engine_protocol_version: int
    request_id: str
    method: str
    nonce: str = field(repr=False)
    params: Mapping[str, object]

    def __post_init__(self) -> None:
        if (
            type(self.engine_protocol_version) is not int
            or self.engine_protocol_version != ENGINE_PROTOCOL_VERSION
        ):
            raise EngineProtocolError("engine_protocol_incompatible")
        _string(self.request_id, maximum=128)
        if self.method not in _METHODS:
            raise EngineProtocolError("engine_method_unsupported")
        _nonce(self.nonce)
        if type(self.params) is not dict:
            raise EngineProtocolError()
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


def encode_request(
    *,
    request_id: str,
    method: str,
    nonce: str,
    params: Mapping[str, object],
) -> bytes:
    request = EngineRequest(
        ENGINE_PROTOCOL_VERSION,
        request_id,
        method,
        nonce,
        dict(params),
    )
    payload = _canonical_bytes(
        {
            "engine_protocol_version": request.engine_protocol_version,
            "request_id": request.request_id,
            "method": request.method,
            "nonce": request.nonce,
            "params": dict(request.params),
        },
        newline=False,
    )
    if len(payload) > MAX_FRAME_BYTES:
        raise EngineProtocolError()
    return payload


def decode_request(payload: bytes) -> EngineRequest:
    value = _decode_json(payload, maximum=MAX_FRAME_BYTES)
    if frozenset(value) != _REQUEST_KEYS:
        raise EngineProtocolError()
    try:
        return EngineRequest(**value)
    except TypeError as error:
        raise EngineProtocolError() from error


def encode_response(
    *,
    request_id: str,
    data: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
) -> bytes:
    _string(request_id, maximum=128)
    if (data is None) == (error is None):
        raise EngineProtocolError()
    payload = _canonical_bytes(
        {
            "engine_protocol_version": ENGINE_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": error is None,
            "data": dict(data) if data is not None else None,
            "error": dict(error) if error is not None else None,
        },
        newline=False,
    )
    if len(payload) > MAX_FRAME_BYTES:
        raise EngineProtocolError()
    return payload


def decode_response(payload: bytes, *, request_id: str) -> dict[str, object]:
    value = _decode_json(payload, maximum=MAX_FRAME_BYTES)
    if frozenset(value) != _RESPONSE_KEYS:
        raise EngineProtocolError()
    if (
        type(value["engine_protocol_version"]) is not int
        or value["engine_protocol_version"] != ENGINE_PROTOCOL_VERSION
    ):
        raise EngineProtocolError("engine_protocol_incompatible")
    if value["request_id"] != request_id or type(value["ok"]) is not bool:
        raise EngineProtocolError()
    if value["ok"]:
        if type(value["data"]) is not dict or value["error"] is not None:
            raise EngineProtocolError()
    elif type(value["error"]) is not dict or value["data"] is not None:
        raise EngineProtocolError()
    return value


__all__ = [
    "DESCRIPTOR_VERSION",
    "ENGINE_PROTOCOL_VERSION",
    "EngineDescriptor",
    "EngineProtocolError",
    "EngineRequest",
    "MAX_DESCRIPTOR_BYTES",
    "MAX_FRAME_BYTES",
    "RUNTIME_COMPATIBILITY_MAJOR",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]
