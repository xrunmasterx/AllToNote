from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import MappingProxyType
from typing import cast


SUPPORTED_CLI_PROTOCOL = 1
SUPPORTED_SCHEMA_VERSION = 2
MAX_RESPONSE_DEPTH = 64
MAX_PROCESS_DIAGNOSTIC_LENGTH = 4000
READ_ONLY_COMMANDS = frozenset({"inspect", "validate", "query", "index"})


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
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _freeze_mapping(self.data))


@dataclass(frozen=True)
class IWikiInspectResult:
    schema_version: int
    cli_protocol_version: int
    workspace_id: str
    name: str
    read_only: bool
    capabilities: frozenset[str]
    paths: Mapping[str, str]
    index: Mapping[str, str | None]

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "paths", _freeze_mapping(self.paths))
        object.__setattr__(self, "index", _freeze_mapping(self.index))


_TOO_DEEP = object()


def _freeze(value: object, depth: int = 0) -> object:
    if depth > MAX_RESPONSE_DEPTH:
        return _TOO_DEEP
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            frozen_item = _freeze(item, depth + 1)
            if frozen_item is _TOO_DEEP:
                return _TOO_DEEP
            frozen[key] = frozen_item
        return MappingProxyType(frozen)
    if type(value) in (list, tuple):
        frozen_items = tuple(_freeze(item, depth + 1) for item in value)
        if any(item is _TOO_DEEP for item in frozen_items):
            return _TOO_DEEP
        return frozen_items
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen = _freeze(value)
    if frozen is _TOO_DEEP:
        _raise_malformed("iwiki response nesting is too deep")
    return cast(Mapping[str, object], frozen)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite_number(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _decode_json(
    decoder: json.JSONDecoder, stdout: str
) -> tuple[object, int] | None:
    try:
        return decoder.raw_decode(stdout)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None


def _load_single_json_object(stdout: str) -> dict[str, object]:
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite_number,
        parse_float=_parse_finite_float,
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


_PRIVATE_PATH = re.compile(r"(?:\b[A-Za-z]:|[\\/])[^\x00\r\n\t ]*")
_REMOTE_CODE = re.compile(r"[a-z][a-z0-9_]*\Z")


def _safe_remote_details(value: object, depth: int = 0) -> object:
    if depth > MAX_RESPONSE_DEPTH:
        return _TOO_DEEP
    if isinstance(value, str):
        return "<redacted>" if _PRIVATE_PATH.search(value) else value
    if type(value) is list:
        sanitized = [_safe_remote_details(item, depth + 1) for item in value]
        return _TOO_DEEP if any(item is _TOO_DEEP for item in sanitized) else sanitized
    if type(value) is dict:
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            safe_key = _safe_remote_details(key, depth + 1)
            safe_item = _safe_remote_details(item, depth + 1)
            if safe_key is _TOO_DEEP or safe_item is _TOO_DEEP:
                return _TOO_DEEP
            sanitized[str(safe_key)] = safe_item
        return sanitized
    return value


def _raise_malformed(message: str) -> None:
    raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, message)


def _is_protocol_relative_path(value: object) -> bool:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or "\\" in value
        or ":" in value
    ):
        return False
    if value.startswith("/") or value.endswith("/"):
        return False
    return all(component not in {"", ".", ".."} for component in value.split("/"))


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
    remote_details = _safe_remote_details(remote["details"])
    if remote_details is _TOO_DEEP:
        _raise_malformed("iwiki remote error details are too deeply nested")
    raise IWikiClientError(
        IWikiClientErrorCode.REMOTE_ERROR,
        "iwiki command failed",
        {
            "remote_code": remote["code"],
            "remote_details": remote_details,
        },
    )


def parse_inspect_result(envelope: IWikiEnvelope) -> IWikiInspectResult:
    if envelope.command != "inspect":
        _raise_malformed("inspect parser received a different command")
    data = envelope.data
    if "paths" in data or "index" in data:
        _raise_malformed("inspect result contains obsolete field aliases")
    required_fields = {
        "schema_version",
        "cli_protocol_version",
        "workspace_id",
        "name",
        "description",
        "read_only",
        "capabilities",
        "relative_paths",
        "index_status",
        "defaults",
        "supported_schema_versions",
    }
    if not required_fields.issubset(data):
        _raise_malformed("inspect result is missing required fields")

    schema_version = data["schema_version"]
    inner_protocol = data["cli_protocol_version"]
    workspace_id = data["workspace_id"]
    name = data["name"]
    description = data["description"]
    read_only = data["read_only"]
    capabilities = data["capabilities"]
    paths = data["relative_paths"]
    index = data["index_status"]
    defaults = data["defaults"]
    supported_schema_versions = data["supported_schema_versions"]
    if type(schema_version) is not int or type(inner_protocol) is not int:
        _raise_malformed("inspect protocol versions are invalid")
    if (
        type(workspace_id) is not str
        or type(name) is not str
        or type(description) is not str
        or type(read_only) is not bool
    ):
        _raise_malformed("inspect identity fields are invalid")
    if type(capabilities) is not tuple or not all(
        type(item) is str for item in capabilities
    ):
        _raise_malformed("inspect capabilities are invalid")
    if not isinstance(paths, Mapping) or not all(
        type(key) is str and _is_protocol_relative_path(value)
        for key, value in paths.items()
    ):
        _raise_malformed("inspect paths are invalid")
    if not isinstance(index, Mapping) or set(index) != {
        "state",
        "backend",
        "database_path",
        "last_success_at",
        "error",
    }:
        _raise_malformed("inspect index is invalid")
    if not all(type(index[field]) is str for field in ("state", "backend", "database_path")):
        _raise_malformed("inspect index is invalid")
    if not _is_protocol_relative_path(index["database_path"]):
        _raise_malformed("inspect paths are invalid")
    if not all(
        index[field] is None or type(index[field]) is str
        for field in ("last_success_at", "error")
    ):
        _raise_malformed("inspect index is invalid")
    if not isinstance(defaults, Mapping) or set(defaults) != {
        "encoding",
        "link_style",
        "publish_scope",
        "visibility",
    } or not all(type(value) is str for value in defaults.values()):
        _raise_malformed("inspect defaults are invalid")
    if type(supported_schema_versions) is not tuple or not all(
        type(item) is int for item in supported_schema_versions
    ):
        _raise_malformed("inspect supported schema versions are invalid")

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
        paths=cast(Mapping[str, str], paths),
        index=cast(Mapping[str, str | None], index),
    )


def _resolve_executable(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve()
        if resolved.is_file():
            return resolved
    except (OSError, RuntimeError):
        pass
    raise IWikiClientError(
        IWikiClientErrorCode.NOT_INSTALLED,
        "iwiki executable is not available",
    ) from None


def discover_iwiki_bin() -> Path:
    configured = os.environ.get("IWIKI_BIN")
    if configured:
        return _resolve_executable(Path(configured))

    lookup_failed = False
    try:
        discovered = shutil.which("iwiki") or shutil.which("iwiki.exe")
    except OSError:
        lookup_failed = True
        discovered = None
    if lookup_failed or discovered is None:
        raise IWikiClientError(
            IWikiClientErrorCode.NOT_INSTALLED,
            "iwiki executable was not found",
        )
    return _resolve_executable(Path(discovered))


def _validate_transport_request(
    command: str, args: list[str], timeout_seconds: float
) -> None:
    if type(command) is not str:
        raise TypeError("command must be a string")
    if command not in READ_ONLY_COMMANDS:
        raise ValueError("command is not available through the read-only transport")
    if type(args) is not list or not all(type(argument) is str for argument in args):
        raise TypeError("args must be a list of strings")
    if command == "index" and (not args or args[0] != "status"):
        raise ValueError("only index status is available through the read-only transport")
    if type(timeout_seconds) not in (int, float):
        raise TypeError("timeout_seconds must be a number")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")


def _safe_process_diagnostic(stderr: str) -> str:
    truncated = stderr[-MAX_PROCESS_DIAGNOSTIC_LENGTH:]
    sanitized = _safe_remote_details(truncated)
    return cast(str, sanitized)


class IWikiTransport:
    def __init__(self, executable: Path):
        if not isinstance(executable, Path):
            raise TypeError("executable must be a Path")
        self.executable = _resolve_executable(executable)

    def run(
        self, command: str, args: list[str], timeout_seconds: float
    ) -> IWikiEnvelope:
        _validate_transport_request(command, args, timeout_seconds)
        process_args = (str(self.executable), command, *tuple(args))
        process_failure: IWikiClientErrorCode | None = None
        try:
            result = subprocess.run(
                process_args,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            process_failure = IWikiClientErrorCode.TIMEOUT
        except (OSError, UnicodeError, ValueError):
            process_failure = IWikiClientErrorCode.PROCESS_FAILED

        if process_failure == IWikiClientErrorCode.TIMEOUT:
            raise IWikiClientError(
                IWikiClientErrorCode.TIMEOUT,
                f"iwiki {command} timed out",
            )
        if process_failure == IWikiClientErrorCode.PROCESS_FAILED:
            raise IWikiClientError(
                IWikiClientErrorCode.PROCESS_FAILED,
                f"cannot run iwiki {command}",
            )

        if (
            type(result.returncode) is not int
            or type(result.stdout) is not str
            or type(result.stderr) is not str
        ):
            raise IWikiClientError(
                IWikiClientErrorCode.PROCESS_FAILED,
                f"iwiki {command} returned an invalid process result",
            )

        if result.returncode == 0:
            return parse_envelope(result.stdout, command)

        diagnostics = {
            "exit_code": result.returncode,
            "stderr": _safe_process_diagnostic(result.stderr),
        }
        try:
            parse_envelope(result.stdout, command)
        except IWikiClientError as error:
            if error.code in {
                IWikiClientErrorCode.REMOTE_ERROR,
                IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
            }:
                error.details.update(diagnostics)
                raise error from None
        raise IWikiClientError(
            IWikiClientErrorCode.PROCESS_FAILED,
            f"iwiki {command} process failed",
            diagnostics,
        ) from None


class IWikiClient:
    def __init__(self, transport: IWikiTransport):
        self.transport = transport

    @classmethod
    def discover(cls) -> "IWikiClient":
        return cls(IWikiTransport(discover_iwiki_bin()))

    @staticmethod
    def _workspace_path(workspace: Path) -> Path:
        if not isinstance(workspace, Path):
            raise TypeError("workspace must be a Path")
        resolved: Path | None = None
        try:
            resolved = workspace.expanduser().resolve()
        except (OSError, RuntimeError):
            pass
        if resolved is None:
            raise IWikiClientError(
                IWikiClientErrorCode.PROCESS_FAILED,
                "cannot resolve iwiki workspace",
            )
        return resolved

    def _inspect_resolved(self, workspace: Path) -> IWikiInspectResult:
        envelope = self.transport.run(
            "inspect",
            ["--workspace", str(workspace), "--json"],
            10,
        )
        return parse_inspect_result(envelope)

    def inspect(self, workspace: Path) -> IWikiInspectResult:
        return self._inspect_resolved(self._workspace_path(workspace))

    def _require(self, workspace: Path, capability: str) -> None:
        inspected = self._inspect_resolved(workspace)
        if capability not in inspected.capabilities:
            raise IWikiClientError(
                IWikiClientErrorCode.MISSING_CAPABILITY,
                f"iwiki capability is missing: {capability}",
                {"capability": capability},
            )

    @staticmethod
    def _data(envelope: IWikiEnvelope, expected_command: str) -> dict[str, object]:
        if envelope.command != expected_command:
            _raise_malformed("iwiki command does not match request")
        if not isinstance(envelope.data, Mapping):
            _raise_malformed("iwiki success response data must be an object")
        return {
            key: IWikiClient._thaw(value)
            for key, value in envelope.data.items()
        }

    @staticmethod
    def _thaw(value: object) -> object:
        if isinstance(value, Mapping):
            return {key: IWikiClient._thaw(item) for key, item in value.items()}
        if type(value) is tuple:
            return [IWikiClient._thaw(item) for item in value]
        return value

    def validate(self, workspace: Path) -> dict[str, object]:
        resolved = self._workspace_path(workspace)
        self._require(resolved, "validate")
        envelope = self.transport.run(
            "validate",
            ["--workspace", str(resolved), "--json"],
            30,
        )
        return self._data(envelope, "validate")

    def query(
        self,
        workspace: Path,
        *,
        scope: str,
        text: str,
        limit: int = 20,
    ) -> dict[str, object]:
        if type(scope) is not str:
            raise TypeError("scope must be a string")
        if scope not in {"common", "personal", "combined"}:
            raise ValueError("scope must be common, personal, or combined")
        if type(text) is not str:
            raise TypeError("text must be a string")
        if not text.strip():
            raise ValueError("text must not be empty")
        if type(limit) is not int:
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        resolved = self._workspace_path(workspace)
        self._require(resolved, "query_native")
        envelope = self.transport.run(
            "query",
            [
                "--workspace",
                str(resolved),
                "--scope",
                scope,
                "--text",
                text,
                "--limit",
                str(limit),
                "--json",
            ],
            30,
        )
        return self._data(envelope, "query")

    def index_status(self, workspace: Path) -> dict[str, object]:
        resolved = self._workspace_path(workspace)
        self._require(resolved, "qmd_index")
        envelope = self.transport.run(
            "index",
            ["status", "--workspace", str(resolved), "--json"],
            10,
        )
        return self._data(envelope, "index")
