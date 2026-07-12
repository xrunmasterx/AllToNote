from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any


SUPPORTED_CLI_PROTOCOL = 1
SUPPORTED_SCHEMA_VERSION = 2


class IWikiClientErrorCode(str, Enum):
    NOT_INSTALLED = "not_installed"
    TIMEOUT = "timeout"
    PROCESS_FAILED = "process_failed"
    MALFORMED_RESPONSE = "malformed_response"
    INCOMPATIBLE_PROTOCOL = "incompatible_protocol"
    MISSING_CAPABILITY = "missing_capability"
    REMOTE_ERROR = "remote_error"


class IWikiClientError(Exception):
    def __init__(
        self,
        code: IWikiClientErrorCode,
        message: str,
        details: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class IWikiEnvelope:
    cli_protocol_version: int
    command: str
    data: dict[str, Any]


@dataclass(frozen=True)
class IWikiInspectResult:
    schema_version: int
    cli_protocol_version: int
    workspace_id: str
    name: str
    read_only: bool
    capabilities: frozenset[str]
    paths: dict[str, str]
    index: dict[str, object]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite_number(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _decode_json(
    decoder: json.JSONDecoder, stdout: str
) -> tuple[object, int] | None:
    try:
        return decoder.raw_decode(stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _load_single_json_object(stdout: str) -> dict[str, object]:
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite_number,
    )
    leading_trimmed = stdout.lstrip(" \t\r\n")
    decoded = _decode_json(decoder, leading_trimmed)
    if decoded is None:
        raise IWikiClientError(
            IWikiClientErrorCode.MALFORMED_RESPONSE,
            "iwiki stdout is not one valid JSON object",
        )
    payload, end = decoded
    if leading_trimmed[end:].strip(" \t\r\n") or type(payload) is not dict:
        raise IWikiClientError(
            IWikiClientErrorCode.MALFORMED_RESPONSE,
            "iwiki response must contain exactly one object",
        )
    return payload


_PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/)[^\x00\r\n]*")
_REMOTE_CODE = re.compile(r"[a-z][a-z0-9_]*\Z")


def _safe_remote_details(value: object) -> object:
    if isinstance(value, str):
        return "<redacted>" if _PRIVATE_PATH.search(value) else value
    if type(value) is list:
        return [_safe_remote_details(item) for item in value]
    if type(value) is dict:
        return {
            str(_safe_remote_details(key)): _safe_remote_details(item)
            for key, item in value.items()
        }
    return value


def _raise_malformed(message: str) -> None:
    raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, message)


def parse_envelope(stdout: str, expected_command: str) -> IWikiEnvelope:
    payload = _load_single_json_object(stdout)
    required_fields = {"cli_protocol_version", "ok", "command", "data", "error"}
    if not required_fields.issubset(payload):
        _raise_malformed("iwiki response is missing required fields")

    protocol = payload["cli_protocol_version"]
    if type(protocol) is not int:
        _raise_malformed("iwiki CLI protocol has an invalid type")
    if protocol != SUPPORTED_CLI_PROTOCOL:
        raise IWikiClientError(
            IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
            "unsupported iwiki CLI protocol",
            {"expected": SUPPORTED_CLI_PROTOCOL, "actual": protocol},
        )

    command = payload["command"]
    if type(command) is not str or command != expected_command:
        _raise_malformed("iwiki command does not match request")
    if type(payload["ok"]) is not bool:
        _raise_malformed("iwiki response status has an invalid type")

    if payload["ok"]:
        data = payload["data"]
        if type(data) is not dict or payload["error"] is not None:
            _raise_malformed("iwiki success response has an invalid shape")
        return IWikiEnvelope(protocol, command, data)

    if payload["data"] is not None:
        _raise_malformed("iwiki error response has an invalid shape")
    remote = payload["error"]
    if type(remote) is not dict or set(remote) != {"code", "message", "details"}:
        _raise_malformed("iwiki remote error has an invalid shape")
    if (
        type(remote["code"]) is not str
        or _REMOTE_CODE.fullmatch(remote["code"]) is None
        or type(remote["message"]) is not str
        or type(remote["details"]) is not dict
    ):
        _raise_malformed("iwiki remote error has invalid fields")
    raise IWikiClientError(
        IWikiClientErrorCode.REMOTE_ERROR,
        "iwiki command failed",
        {
            "remote_code": remote["code"],
            "remote_details": _safe_remote_details(remote["details"]),
        },
    )


def parse_inspect_result(envelope: IWikiEnvelope) -> IWikiInspectResult:
    if envelope.command != "inspect":
        _raise_malformed("inspect parser received a different command")
    data = envelope.data
    required_fields = {
        "schema_version",
        "cli_protocol_version",
        "workspace_id",
        "name",
        "read_only",
        "capabilities",
        "paths",
        "index",
    }
    if not required_fields.issubset(data):
        _raise_malformed("inspect result is missing required fields")

    schema_version = data["schema_version"]
    inner_protocol = data["cli_protocol_version"]
    workspace_id = data["workspace_id"]
    name = data["name"]
    read_only = data["read_only"]
    capabilities = data["capabilities"]
    paths = data["paths"]
    index = data["index"]
    if type(schema_version) is not int or type(inner_protocol) is not int:
        _raise_malformed("inspect protocol versions are invalid")
    if type(workspace_id) is not str or type(name) is not str or type(read_only) is not bool:
        _raise_malformed("inspect identity fields are invalid")
    if type(capabilities) is not list or not all(type(item) is str for item in capabilities):
        _raise_malformed("inspect capabilities are invalid")
    if type(paths) is not dict or not all(
        type(key) is str and type(value) is str for key, value in paths.items()
    ):
        _raise_malformed("inspect paths are invalid")
    if type(index) is not dict or set(index) != {"state", "backend"} or not all(
        type(value) is str for value in index.values()
    ):
        _raise_malformed("inspect index is invalid")

    if inner_protocol != envelope.cli_protocol_version or inner_protocol != SUPPORTED_CLI_PROTOCOL:
        raise IWikiClientError(
            IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
            "inspect result uses an incompatible CLI protocol",
        )
    if schema_version < SUPPORTED_SCHEMA_VERSION or (
        schema_version > SUPPORTED_SCHEMA_VERSION and not read_only
    ):
        raise IWikiClientError(
            IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
            "inspect result uses an incompatible workspace schema",
            {"supported": SUPPORTED_SCHEMA_VERSION, "actual": schema_version},
        )

    return IWikiInspectResult(
        schema_version=schema_version,
        cli_protocol_version=inner_protocol,
        workspace_id=workspace_id,
        name=name,
        read_only=read_only,
        capabilities=frozenset(capabilities),
        paths=dict(paths),
        index=dict(index),
    )
