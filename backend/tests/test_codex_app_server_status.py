from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.codex_app_server import (
    CODEX_PROVIDER_ID,
    CodexAppServerStatus,
    CodexAppServerStatusService,
)


def test_read_default_model_from_config(tmp_path: Path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    assert CodexAppServerStatusService.read_default_model(codex_home) == "gpt-5.5"


def test_missing_config_returns_none(tmp_path: Path):
    assert CodexAppServerStatusService.read_default_model(tmp_path / ".codex") is None


def test_status_ready_requires_cli_and_auth(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    monkeypatch.setattr(CodexAppServerStatusService, "find_codex_bin", staticmethod(lambda: "codex"))
    monkeypatch.setattr(
        CodexAppServerStatusService,
        "read_codex_version",
        staticmethod(lambda _bin: "codex-cli 0.137.0"),
    )
    status = CodexAppServerStatusService.get_status(codex_home=codex_home)
    assert status == CodexAppServerStatus(
        codex_cli_available=True,
        codex_version="codex-cli 0.137.0",
        auth_available=True,
        default_model="gpt-5.5",
        ready=True,
    )


def test_status_not_ready_without_auth(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    monkeypatch.setattr(CodexAppServerStatusService, "find_codex_bin", staticmethod(lambda: "codex"))
    monkeypatch.setattr(
        CodexAppServerStatusService,
        "read_codex_version",
        staticmethod(lambda _bin: "codex-cli 0.137.0"),
    )
    status = CodexAppServerStatusService.get_status(codex_home=codex_home)
    assert status.codex_cli_available is True
    assert status.auth_available is False
    assert status.ready is False


def test_codex_provider_id_is_stable():
    assert CODEX_PROVIDER_ID == "codex_app_server"
