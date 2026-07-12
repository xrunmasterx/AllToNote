from pathlib import Path
from collections.abc import Mapping
import json
import os
import stat
from subprocess import CompletedProcess, TimeoutExpired
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.iwiki_client import (
    IWikiTransport,
    IWikiClientError,
    IWikiClientErrorCode,
    IWikiEnvelope,
    discover_iwiki_bin,
    parse_envelope,
    parse_inspect_result,
)


FIXTURES = Path(__file__).parent / "fixtures" / "iwiki"


def _success_payload(**data_overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 2,
        "cli_protocol_version": 1,
        "workspace_id": "llm-iwiki-main",
        "name": "LLM Wiki",
        "description": "Personal and shared Markdown knowledge workspace.",
        "read_only": False,
        "capabilities": ["inspect", "validate", "query_native", "qmd_index"],
        "relative_paths": {
            "cache": ".cache",
            "raw_common": "raw/common",
            "raw_personal": "raw/personal",
            "wiki_common": "wiki/common",
            "wiki_personal": "wiki/personal",
        },
        "index_status": {
            "state": "missing",
            "backend": "qmd",
            "database_path": ".cache/qmd/llm-iwiki.sqlite",
            "last_success_at": None,
            "error": None,
        },
        "defaults": {
            "encoding": "utf-8",
            "link_style": "wikilink",
            "publish_scope": "personal",
            "visibility": "private",
        },
        "supported_schema_versions": [2],
    }
    data.update(data_overrides)
    return {
        "cli_protocol_version": 1,
        "ok": True,
        "command": "inspect",
        "data": data,
        "error": None,
    }


def _parse_inspect_payload(**data_overrides: object):
    envelope = parse_envelope(json.dumps(_success_payload(**data_overrides)), "inspect")
    return parse_inspect_result(envelope)


def test_parse_inspect_success_fixture():
    envelope = parse_envelope(
        (FIXTURES / "inspect-success.json").read_text(encoding="utf-8"), "inspect"
    )
    result = parse_inspect_result(envelope)
    assert result.schema_version == 2
    assert result.cli_protocol_version == 1
    assert result.capabilities >= {"inspect", "validate", "query_native", "qmd_index"}
    assert isinstance(result.capabilities, frozenset)
    assert result.paths["wiki_personal"] == "wiki/personal"
    assert result.index == {
        "backend": "qmd",
        "database_path": ".cache/qmd/llm-iwiki.sqlite",
        "error": None,
        "last_success_at": None,
        "state": "missing",
    }


def test_parse_inspect_replays_provider_v1_wire_contract_without_aliases():
    stdout = (FIXTURES / "inspect-success.json").read_text(encoding="utf-8")
    envelope = parse_envelope(stdout, "inspect")
    result = parse_inspect_result(envelope)
    assert "relative_paths" in envelope.data
    assert "index_status" in envelope.data
    assert "paths" not in envelope.data
    assert "index" not in envelope.data
    assert result.paths == envelope.data["relative_paths"]
    assert result.index == envelope.data["index_status"]


def test_parse_inspect_rejects_obsolete_paths_and_index_aliases():
    payload = _success_payload()
    data = payload["data"]
    data["paths"] = data.pop("relative_paths")  # type: ignore[union-attr]
    data["index"] = data.pop("index_status")  # type: ignore[union-attr]
    envelope = parse_envelope(json.dumps(payload), "inspect")
    with pytest.raises(IWikiClientError) as raised:
        parse_inspect_result(envelope)
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


def test_parse_inspect_rejects_obsolete_aliases_even_with_current_wire_fields():
    payload = _success_payload()
    payload["data"]["paths"] = {"wiki_common": "wrong"}  # type: ignore[index]
    envelope = parse_envelope(json.dumps(payload), "inspect")
    with pytest.raises(IWikiClientError) as raised:
        parse_inspect_result(envelope)
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize("stdout", ["", "not-json", "{}", "[]", '{"cli_protocol_version": 2}'])
def test_parse_envelope_rejects_malformed_or_incompatible_output(stdout: str):
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert raised.value.code in {
        IWikiClientErrorCode.MALFORMED_RESPONSE,
        IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
    }


@pytest.mark.parametrize(
    "stdout",
    [
        json.dumps(_success_payload()) + json.dumps(_success_payload()),
        '{"cli_protocol_version":1,"cli_protocol_version":1,"ok":true,'
        '"command":"inspect","data":{},"error":null}',
        '{"cli_protocol_version":1,"ok":true,"command":"inspect",'
        '"data":{"value":NaN},"error":null}',
        "\N{NO-BREAK SPACE}" + json.dumps(_success_payload()),
        json.dumps(_success_payload()) + "\N{NO-BREAK SPACE}",
    ],
)
def test_parse_envelope_requires_one_strict_json_object(stdout: str):
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "number",
    ["1e999", "-1e999", "[1, {\"nested\": 1e999}]"],
)
def test_parse_envelope_rejects_nonfinite_json_floats(number: str):
    stdout = (
        '{"cli_protocol_version":1,"ok":true,"command":"inspect",'
        f'"data":{{"value":{number}}},"error":null}}'
    )
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_parse_envelope_converts_decoder_recursion_to_safe_malformed_response():
    private = r"C:\Users\private-user\secret-vault"
    stdout = "[" * 2000 + json.dumps(private) + "]" * 2000
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cli_protocol_version", True),
        ("ok", 1),
        ("command", 1),
    ],
)
def test_parse_envelope_rejects_non_exact_primitive_types(field: str, value: object):
    payload = _success_payload()
    payload[field] = value
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(payload), "inspect")
    assert raised.value.code in {
        IWikiClientErrorCode.MALFORMED_RESPONSE,
        IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
    }


def test_parse_envelope_rejects_command_mismatch():
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(_success_payload()), "query")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "updates",
    [
        {"ok": True, "error": {"code": "unexpected"}},
        {"ok": True, "data": None},
        {"ok": False, "data": {}},
        {"ok": False, "error": None},
    ],
)
def test_parse_envelope_rejects_inconsistent_success_error_shapes(updates: dict[str, object]):
    payload = _success_payload()
    payload.update(updates)
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(payload), "inspect")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


def test_parse_envelope_converts_well_formed_remote_error():
    stdout = json.dumps(
        {
            "cli_protocol_version": 1,
            "ok": False,
            "command": "inspect",
            "data": None,
            "error": {
                "code": "invalid_workspace",
                "message": "cannot read manifest",
                "details": {"reason": "missing"},
            },
        }
    )
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert raised.value.code == IWikiClientErrorCode.REMOTE_ERROR
    assert raised.value.details == {
        "remote_code": "invalid_workspace",
        "remote_details": {"reason": "missing"},
    }


@pytest.mark.parametrize(
    "remote_error",
    [
        {},
        {"code": 1, "message": "failed", "details": {}},
        {"code": "failed", "message": 1, "details": {}},
        {"code": "failed", "message": "failed", "details": []},
    ],
)
def test_parse_envelope_rejects_malformed_remote_error(remote_error: object):
    payload = _success_payload()
    payload.update(ok=False, data=None, error=remote_error)
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(payload), "inspect")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


def test_parser_errors_do_not_echo_raw_stdout_or_private_paths():
    private = r"C:\\Users\\private-user\\secret-vault"
    stdout = private + " is not JSON"
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_remote_errors_redact_private_paths():
    private = r"C:\\Users\\private-user\\secret-vault"
    payload = _success_payload()
    payload.update(
        ok=False,
        data=None,
        error={
            "code": "invalid_workspace",
            "message": f"cannot read {private}",
            "details": {
                "manifest": f"{private}\\iwiki.yaml",
                f"{private}\\key": "value",
            },
        },
    )
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(payload), "inspect")
    assert raised.value.code == IWikiClientErrorCode.REMOTE_ERROR
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)
    assert raised.value.details["remote_details"] == {
        "manifest": "<redacted>",
        "<redacted>": "value",
    }


@pytest.mark.parametrize(
    "private_path",
    [
        r"C:relative\secret",
        r"C:\absolute\secret",
        r"C:/absolute/secret",
        r"\\server\share\secret",
        r"\\?\C:\extended\secret",
        r"\\.\PIPE\secret",
        r"\rooted\secret",
        "/home/private/secret",
    ],
)
def test_remote_errors_redact_all_supported_private_path_forms(private_path: str):
    payload = _success_payload()
    payload.update(
        ok=False,
        data=None,
        error={
            "code": "invalid_workspace",
            "message": f"cannot read {private_path}",
            "details": {
                "paths": [private_path],
                private_path: "private key",
            },
        },
    )
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(payload), "inspect")
    assert raised.value.code == IWikiClientErrorCode.REMOTE_ERROR
    assert private_path not in str(raised.value)
    assert private_path not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.details["remote_details"] == {
        "paths": ["<redacted>"],
        "<redacted>": "private key",
    }


def test_remote_detail_sanitizer_rejects_excessive_nesting_without_leaking():
    private = r"C:\Users\private-user\secret-vault"
    nested: object = private
    for _ in range(80):
        nested = {"next": nested}
    payload = _success_payload()
    payload.update(
        ok=False,
        data=None,
        error={
            "code": "invalid_workspace",
            "message": f"cannot read {private}",
            "details": {"nested": nested},
        },
    )
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(payload), "inspect")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_remote_error_code_must_be_a_safe_identifier():
    private = r"C:\\Users\\private-user\\secret-vault"
    payload = _success_payload()
    payload.update(
        ok=False,
        data=None,
        error={"code": private, "message": "failed", "details": {}},
    )
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(json.dumps(payload), "inspect")
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("cli_protocol_version", True),
        ("workspace_id", 1),
        ("name", 1),
        ("description", 1),
        ("read_only", 0),
        ("capabilities", ["inspect", 1]),
        ("relative_paths", {"wiki_common": 1}),
        ("index_status", []),
        ("defaults", []),
        ("supported_schema_versions", [True]),
    ],
)
def test_parse_inspect_rejects_invalid_required_field_types(field: str, value: object):
    with pytest.raises(IWikiClientError) as raised:
        _parse_inspect_payload(**{field: value})
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "missing",
    [
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
    ],
)
def test_parse_inspect_requires_all_fields(missing: str):
    payload = _success_payload()
    del payload["data"][missing]  # type: ignore[index]
    envelope = parse_envelope(json.dumps(payload), "inspect")
    with pytest.raises(IWikiClientError) as raised:
        parse_inspect_result(envelope)
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "index_status",
    [
        {
            "state": 1,
            "backend": "qmd",
            "database_path": ".cache/qmd.sqlite",
            "last_success_at": None,
            "error": None,
        },
        {
            "state": "missing",
            "backend": "qmd",
            "database_path": ".cache/qmd.sqlite",
            "last_success_at": 1,
            "error": None,
        },
        {
            "state": "missing",
            "backend": "qmd",
            "database_path": ".cache/qmd.sqlite",
            "last_success_at": None,
        },
    ],
)
def test_parse_inspect_rejects_invalid_index_status(index_status: object):
    with pytest.raises(IWikiClientError) as raised:
        _parse_inspect_payload(index_status=index_status)
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


def test_parse_inspect_requires_matching_inner_protocol():
    with pytest.raises(IWikiClientError) as raised:
        _parse_inspect_payload(cli_protocol_version=2)
    assert raised.value.code == IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL


def test_parse_inspect_requires_inspect_envelope():
    payload = _success_payload()["data"]
    envelope = IWikiEnvelope(1, "query", payload)  # type: ignore[arg-type]
    with pytest.raises(IWikiClientError) as raised:
        parse_inspect_result(envelope)
    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE


def test_parse_inspect_accepts_supported_writable_schema():
    result = _parse_inspect_payload(schema_version=2, read_only=False)
    assert result.schema_version == 2
    assert result.read_only is False


def test_parse_inspect_accepts_newer_schema_only_when_read_only():
    result = _parse_inspect_payload(schema_version=3, read_only=True)
    assert result.schema_version == 3
    assert result.read_only is True

    with pytest.raises(IWikiClientError) as raised:
        _parse_inspect_payload(schema_version=3, read_only=False)
    assert raised.value.code == IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL


def test_parse_inspect_rejects_older_schema():
    with pytest.raises(IWikiClientError) as raised:
        _parse_inspect_payload(schema_version=1, read_only=True)
    assert raised.value.code == IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL


def test_parse_inspect_does_not_alias_mutable_envelope_data():
    capabilities = ["inspect"]
    paths = {"wiki_common": "wiki/common"}
    index = {
        "state": "missing",
        "backend": "qmd",
        "database_path": ".cache/qmd.sqlite",
        "last_success_at": None,
        "error": None,
    }
    data = _success_payload(
        capabilities=capabilities,
        relative_paths=paths,
        index_status=index,
    )["data"]
    envelope = IWikiEnvelope(1, "inspect", data)  # type: ignore[arg-type]

    result = parse_inspect_result(envelope)
    capabilities.append("query_native")
    paths["wiki_common"] = "changed"
    index["state"] = "ready"

    assert result.capabilities == frozenset({"inspect"})
    assert result.paths == {"wiki_common": "wiki/common"}
    assert result.index == {
        "state": "missing",
        "backend": "qmd",
        "database_path": ".cache/qmd.sqlite",
        "last_success_at": None,
        "error": None,
    }


def test_protocol_models_are_deeply_immutable():
    query = (FIXTURES / "query-success.json").read_text(encoding="utf-8")
    envelope = parse_envelope(query, "query")
    assert isinstance(envelope.data, Mapping)
    assert isinstance(envelope.data["items"], tuple)
    with pytest.raises(TypeError):
        envelope.data["scope"] = "personal"  # type: ignore[index]
    with pytest.raises(TypeError):
        envelope.data["items"][0]["path"] = "private.md"  # type: ignore[index]

    result = _parse_inspect_payload()
    assert isinstance(result.paths, Mapping)
    assert isinstance(result.index, Mapping)
    with pytest.raises(TypeError):
        result.paths["wiki_common"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.index["state"] = "ready"  # type: ignore[index]


def test_discovery_prefers_resolved_explicit_environment(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    binary = home / "iwiki.exe"
    binary.write_bytes(b"")
    calls: list[str] = []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("IWIKI_BIN", "~/iwiki.exe")
    monkeypatch.setattr(
        "app.services.iwiki_client.shutil.which",
        lambda name: calls.append(name),
    )

    assert discover_iwiki_bin() == binary.resolve()
    assert calls == []


def test_discovery_rejects_invalid_environment_without_leaking_path(
    monkeypatch, tmp_path: Path
):
    private = tmp_path / "private-user" / "missing-iwiki.exe"
    monkeypatch.setenv("IWIKI_BIN", str(private))

    with pytest.raises(IWikiClientError) as raised:
        discover_iwiki_bin()

    assert raised.value.code == IWikiClientErrorCode.NOT_INSTALLED
    assert str(private) not in str(raised.value)
    assert str(private) not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_discovery_falls_back_to_path_and_returns_absolute_file(monkeypatch, tmp_path: Path):
    binary = tmp_path / "bin" / "iwiki"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    calls: list[str] = []

    def fake_which(name: str):
        calls.append(name)
        return str(binary) if name == "iwiki" else None

    monkeypatch.delenv("IWIKI_BIN", raising=False)
    monkeypatch.setattr("app.services.iwiki_client.shutil.which", fake_which)

    discovered = discover_iwiki_bin()
    assert discovered == binary.resolve()
    assert discovered.is_absolute()
    assert calls == ["iwiki"]


def test_discovery_tries_windows_path_name_and_reports_generic_missing(monkeypatch):
    calls: list[str] = []
    monkeypatch.delenv("IWIKI_BIN", raising=False)
    monkeypatch.setattr(
        "app.services.iwiki_client.shutil.which",
        lambda name: calls.append(name),
    )

    with pytest.raises(IWikiClientError) as raised:
        discover_iwiki_bin()

    assert raised.value.code == IWikiClientErrorCode.NOT_INSTALLED
    assert calls == ["iwiki", "iwiki.exe"]
    assert raised.value.details == {}


def test_discovery_converts_path_lookup_oserror_without_leaking(monkeypatch):
    private = r"C:\Users\private-user\secret-path"
    monkeypatch.delenv("IWIKI_BIN", raising=False)
    monkeypatch.setattr(
        "app.services.iwiki_client.shutil.which",
        lambda _name: (_ for _ in ()).throw(OSError(private)),
    )

    with pytest.raises(IWikiClientError) as raised:
        discover_iwiki_bin()

    assert raised.value.code == IWikiClientErrorCode.NOT_INSTALLED
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_transport_uses_new_immutable_argv_without_shell(monkeypatch, tmp_path: Path):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    fixture = (FIXTURES / "inspect-success.json").read_text(encoding="utf-8")
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return CompletedProcess(args, 0, fixture, "ignored diagnostic")

    monkeypatch.setattr("app.services.iwiki_client.subprocess.run", fake_run)
    supplied_args = ["--workspace", "C:/Wiki", "--json"]
    envelope = IWikiTransport(binary).run("inspect", supplied_args, 10)

    assert envelope.command == "inspect"
    assert calls[0][0] == (
        str(binary.resolve()),
        "inspect",
        "--workspace",
        "C:/Wiki",
        "--json",
    )
    assert isinstance(calls[0][0], tuple)
    assert calls[0][0] is not supplied_args
    assert supplied_args == ["--workspace", "C:/Wiki", "--json"]
    assert calls[0][1] == {
        "capture_output": True,
        "check": False,
        "text": True,
        "encoding": "utf-8",
        "errors": "strict",
        "timeout": 10,
        "shell": False,
    }


@pytest.mark.parametrize("command", ["inspect", "validate", "query", "index"])
def test_transport_accepts_only_planned_read_only_commands(
    monkeypatch, tmp_path: Path, command: str
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    payload = _success_payload()
    payload["command"] = command
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda args, **kwargs: CompletedProcess(args, 0, json.dumps(payload), ""),
    )

    args = ["status"] if command == "index" else []
    assert IWikiTransport(binary).run(command, args, 1).command == command


@pytest.mark.parametrize("args", [[], ["rebuild"], ["--workspace", "C:/Wiki"]])
def test_transport_index_surface_allows_status_only(
    monkeypatch, tmp_path: Path, args: list[str]
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *run_args, **kwargs: pytest.fail("write-capable index args reached subprocess"),
    )

    with pytest.raises(ValueError):
        IWikiTransport(binary).run("index", args, 1)


@pytest.mark.parametrize(
    "command",
    ["apply-publish", "plan-publish", "publish", "", 1, True],
)
def test_transport_rejects_non_read_only_or_non_string_commands(
    monkeypatch, tmp_path: Path, command: object
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *args, **kwargs: pytest.fail("invalid command reached subprocess"),
    )

    with pytest.raises((TypeError, ValueError)):
        IWikiTransport(binary).run(command, [], 1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "args",
    [(), ("--json",), [Path("workspace")], [1], [True]],
)
def test_transport_requires_an_exact_list_of_exact_strings(
    monkeypatch, tmp_path: Path, args: object
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *run_args, **kwargs: pytest.fail("invalid args reached subprocess"),
    )

    with pytest.raises(TypeError):
        IWikiTransport(binary).run("inspect", args, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [True, False, 0, -1, float("inf"), float("nan"), "10"])
def test_transport_requires_a_positive_finite_numeric_timeout(
    monkeypatch, tmp_path: Path, timeout: object
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *args, **kwargs: pytest.fail("invalid timeout reached subprocess"),
    )

    with pytest.raises((TypeError, ValueError)):
        IWikiTransport(binary).run("inspect", [], timeout)  # type: ignore[arg-type]


def test_transport_timeout_is_typed_and_does_not_chain_private_argv(
    monkeypatch, tmp_path: Path
):
    binary = tmp_path / "private-user" / "iwiki.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutExpired(args[0], 10)),
    )

    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", ["--workspace", str(tmp_path)], 10)

    assert raised.value.code == IWikiClientErrorCode.TIMEOUT
    assert str(binary) not in str(raised.value)
    assert str(binary) not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "failure",
    [OSError(r"cannot execute C:\\Users\\private\\iwiki.exe"), UnicodeError("decode failed")],
)
def test_transport_process_failures_are_typed_and_generic(
    monkeypatch, tmp_path: Path, failure: Exception
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", [], 10)

    assert raised.value.code == IWikiClientErrorCode.PROCESS_FAILED
    assert "private" not in str(raised.value)
    assert "private" not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_transport_converts_nul_argument_valueerror_without_leaking_argv(
    monkeypatch, tmp_path: Path
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    private = r"C:\Users\private-user\secret-vault"
    argument = private + "\x00suffix"
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError(f"embedded null byte in {args[0]!r}")
        ),
    )

    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", ["--workspace", argument], 10)

    assert raised.value.code == IWikiClientErrorCode.PROCESS_FAILED
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)
    assert raised.value.details == {}
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def _remote_error_payload(command: str = "inspect") -> str:
    return json.dumps(
        {
            "cli_protocol_version": 1,
            "ok": False,
            "command": command,
            "data": None,
            "error": {
                "code": "invalid_workspace",
                "message": "cannot read manifest",
                "details": {"reason": "missing"},
            },
        }
    )


def test_nonzero_valid_remote_error_preserves_remote_error_with_safe_diagnostics(
    monkeypatch, tmp_path: Path
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    private = r"C:\Users\private-user\secret-vault"
    stderr = "x" * 5000 + " " + private
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda args, **kwargs: CompletedProcess(args, 7, _remote_error_payload(), stderr),
    )

    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", [], 10)

    assert raised.value.code == IWikiClientErrorCode.REMOTE_ERROR
    assert raised.value.details["remote_code"] == "invalid_workspace"
    assert raised.value.details["exit_code"] == 7
    assert private not in repr(raised.value.details)
    assert raised.value.details["stderr"] == "<redacted>"


def test_nonzero_diagnostic_is_truncated_before_attachment(monkeypatch, tmp_path: Path):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    stderr = "a" * 5000
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda args, **kwargs: CompletedProcess(args, 7, _remote_error_payload(), stderr),
    )

    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", [], 10)

    assert raised.value.code == IWikiClientErrorCode.REMOTE_ERROR
    assert raised.value.details["stderr"] == "a" * 4000


@pytest.mark.parametrize("stdout", ["not-json", json.dumps(_success_payload())])
def test_nonzero_malformed_or_success_stdout_cannot_be_successful(
    monkeypatch, tmp_path: Path, stdout: str
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda args, **kwargs: CompletedProcess(args, 3, stdout, "diagnostic"),
    )

    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", [], 10)

    assert raised.value.code == IWikiClientErrorCode.PROCESS_FAILED
    assert raised.value.details == {"exit_code": 3, "stderr": "diagnostic"}


def test_zero_malformed_stdout_remains_malformed_without_raw_output(
    monkeypatch, tmp_path: Path
):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    private = r"C:\Users\private-user\secret-vault"
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda args, **kwargs: CompletedProcess(args, 0, private + " not-json", ""),
    )

    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", [], 10)

    assert raised.value.code == IWikiClientErrorCode.MALFORMED_RESPONSE
    assert private not in str(raised.value)
    assert private not in repr(raised.value.details)


def test_stderr_is_diagnostic_only_and_is_not_parsed(monkeypatch, tmp_path: Path):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    success = json.dumps(_success_payload())
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda args, **kwargs: CompletedProcess(
            args, 0, success, _remote_error_payload()
        ),
    )

    envelope = IWikiTransport(binary).run("inspect", [], 10)
    assert envelope.command == "inspect"


@pytest.mark.skipif(os.name == "nt", reason="portable temp executables require POSIX")
def test_transport_real_temp_executable_smoke(tmp_path: Path):
    binary = tmp_path / "iwiki"
    fixture = (FIXTURES / "inspect-success.json").read_text(encoding="utf-8")
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({fixture!r})\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    envelope = IWikiTransport(binary).run("inspect", ["--json"], 10)
    assert envelope.command == "inspect"
