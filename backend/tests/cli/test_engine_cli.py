from __future__ import annotations

import json
from dataclasses import dataclass

from app.cli.main import main


@dataclass(frozen=True)
class _Status:
    state: str
    running: bool
    engine_id: str | None = None
    started_at: str | None = None
    started: bool = False
    stopped: bool = False


class _FakeEngineClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self) -> _Status:
        self.calls.append("status")
        return _Status("stopped", False)

    def ensure(self) -> _Status:
        self.calls.append("ensure")
        return _Status(
            "running",
            True,
            engine_id="018f0000-0000-7000-8000-000000000001",
            started_at="2026-08-01T00:00:00Z",
            started=True,
        )

    def stop(self) -> _Status:
        self.calls.append("stop")
        return _Status("stopped", False, stopped=True)


def test_engine_status_is_read_only_and_uses_cli_protocol_v1(capsys) -> None:
    client = _FakeEngineClient()

    assert main(["engine", "status", "--json"], engine_client=client) == 0

    output = capsys.readouterr()
    envelope = json.loads(output.out)
    assert output.err == ""
    assert client.calls == ["status"]
    assert envelope["alltonote_cli_protocol_version"] == 1
    assert envelope["command"] == "engine status"
    assert envelope["data"] == {
        "engine_id": None,
        "engine_protocol_version": 1,
        "running": False,
        "started_at": None,
        "state": "stopped",
        "supported": True,
    }
    assert "endpoint" not in output.out
    assert "nonce" not in output.out


def test_engine_ensure_and_stop_are_explicit_idempotent_commands(capsys) -> None:
    client = _FakeEngineClient()

    assert main(["engine", "ensure", "--json"], engine_client=client) == 0
    ensured = json.loads(capsys.readouterr().out)
    assert ensured["command"] == "engine ensure"
    assert ensured["data"]["started"] is True
    assert ensured["data"]["running"] is True

    assert main(["engine", "stop", "--json"], engine_client=client) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["command"] == "engine stop"
    assert stopped["data"]["stopped"] is True
    assert stopped["data"]["running"] is False
    assert client.calls == ["ensure", "stop"]


def test_engine_usage_error_preserves_leaf_command_identity(capsys) -> None:
    exit_code = main(["engine", "ensure", "--unknown", "--json"])
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["command"] == "engine ensure"
    assert envelope["error"]["code"] == "cli_usage_invalid"
