from pathlib import Path
import json
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.iwiki_client import (
    IWikiClientError,
    IWikiClientErrorCode,
    IWikiEnvelope,
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
        "read_only": False,
        "capabilities": ["inspect", "validate", "query_native", "qmd_index"],
        "paths": {"wiki_common": "wiki/common", "wiki_personal": "wiki/personal"},
        "index": {"state": "missing", "backend": "qmd"},
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
        ("read_only", 0),
        ("capabilities", ["inspect", 1]),
        ("paths", {"wiki_common": 1}),
        ("index", []),
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
        "read_only",
        "capabilities",
        "paths",
        "index",
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
    "index",
    [
        {"state": 1, "backend": "qmd"},
        {"state": "missing", "backend": 1},
        {1: "missing", "backend": "qmd"},
    ],
)
def test_parse_inspect_rejects_non_string_index_entries(index: object):
    with pytest.raises(IWikiClientError) as raised:
        _parse_inspect_payload(index=index)
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
    index = {"state": "missing", "backend": "qmd"}
    data = _success_payload(capabilities=capabilities, paths=paths, index=index)["data"]
    envelope = IWikiEnvelope(1, "inspect", data)  # type: ignore[arg-type]

    result = parse_inspect_result(envelope)
    capabilities.append("query_native")
    paths["wiki_common"] = "changed"
    index["state"] = "ready"

    assert result.capabilities == frozenset({"inspect"})
    assert result.paths == {"wiki_common": "wiki/common"}
    assert result.index == {"state": "missing", "backend": "qmd"}
