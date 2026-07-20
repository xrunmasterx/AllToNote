from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app.adapters.credentials.keyring_broker import CredentialBroker
from app.adapters.credentials.profile_catalog import CredentialProfileCatalog
from app.cli.main import main


class _Keyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.set_error: Exception | None = None

    def get_password(self, service: str, profile: str) -> str | None:
        return self.values.get((service, profile))

    def set_password(self, service: str, profile: str, secret: str) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.values[(service, profile)] = secret

    def delete_password(self, service: str, profile: str) -> None:
        del self.values[(service, profile)]


def _broker(tmp_path: Path, keyring_backend: _Keyring) -> CredentialBroker:
    timestamps = iter(
        (
            "2026-07-18T12:00:00.000Z",
            "2026-07-18T12:01:00.000Z",
        )
    )
    return CredentialBroker(
        keyring_backend=keyring_backend,
        catalog=CredentialProfileCatalog(
            tmp_path / "credential-profiles.toml",
            clock=timestamps.__next__,
        ),
        environ={},
        clock=lambda: "2026-07-18T12:02:00.000Z",
    )


def test_credential_set_status_delete_never_echoes_or_persists_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keyring_backend = _Keyring()
    broker = _broker(tmp_path, keyring_backend)
    secret = "credential-cli-secret-canary-1042"

    assert (
        main(
            [
                "credential",
                "set",
                "providers/openai-main",
                "--stdin",
                "--json",
            ],
            credential_broker=broker,
            input_stream=io.StringIO(secret + "\n"),
        )
        == 0
    )
    set_output = capsys.readouterr()
    assert json.loads(set_output.out)["data"] == {"stored": True}
    assert set_output.err == ""
    assert secret not in set_output.out
    assert secret not in (tmp_path / "credential-profiles.toml").read_text(
        encoding="utf-8"
    )

    assert (
        main(
            ["credential", "status", "providers/openai-main", "--json"],
            credential_broker=broker,
        )
        == 0
    )
    status_output = capsys.readouterr()
    status = json.loads(status_output.out)
    assert status["data"] == {
        "present": True,
        "validated": None,
        "last_checked_at": "2026-07-18T12:02:00.000Z",
    }
    assert secret not in status_output.out

    assert (
        main(
            ["credential", "delete", "providers/openai-main", "--json"],
            credential_broker=broker,
        )
        == 0
    )
    delete_output = capsys.readouterr()
    assert json.loads(delete_output.out)["data"] == {"deleted": True}
    assert secret not in delete_output.out
    assert keyring_backend.values == {}


def test_credential_set_rejects_plaintext_positional_argument_without_echo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "positional-secret-canary-2043"

    exit_code = main(
        [
            "credential",
            "set",
            "providers/openai-main",
            secret,
            "--json",
        ],
        credential_broker=_broker(tmp_path, _Keyring()),
    )
    output = capsys.readouterr()
    envelope = json.loads(output.out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "cli_usage_invalid"
    assert secret not in output.out
    assert secret not in output.err


def test_noninteractive_credential_set_requires_explicit_stdin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keyring_backend = _Keyring()

    exit_code = main(
        ["credential", "set", "providers/openai-main", "--json"],
        credential_broker=_broker(tmp_path, keyring_backend),
        input_stream=io.StringIO("must-not-be-read"),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 40
    assert envelope["error"]["code"] == "credential_input_required"
    assert keyring_backend.values == {}


def test_unavailable_secure_backend_never_falls_back_to_plaintext(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keyring_backend = _Keyring()
    keyring_backend.set_error = RuntimeError("private backend diagnostic")
    secret = "backend-secret-canary-3044"

    exit_code = main(
        [
            "credential",
            "set",
            "providers/openai-main",
            "--stdin",
            "--json",
        ],
        credential_broker=_broker(tmp_path, keyring_backend),
        input_stream=io.StringIO(secret),
    )
    output = capsys.readouterr()
    envelope = json.loads(output.out)

    assert exit_code == 30
    assert envelope["error"]["code"] == "credential_backend_unavailable"
    assert secret not in output.out
    assert "private backend diagnostic" not in output.out
    assert not (tmp_path / "credential-profiles.toml").exists()
