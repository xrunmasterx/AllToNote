from dataclasses import asdict, dataclass
from pathlib import Path
import os
import shutil
import subprocess
import tomllib


CODEX_PROVIDER_ID = "codex_app_server"
CODEX_PROVIDER_TYPE = "codex_app_server"
CODEX_LOCAL_BASE_URL = "codex-app-server://local"


@dataclass
class CodexAppServerStatus:
    codex_cli_available: bool
    codex_version: str | None
    auth_available: bool
    default_model: str | None
    ready: bool

    def to_dict(self) -> dict:
        return asdict(self)


class CodexAppServerStatusService:
    @staticmethod
    def default_codex_home() -> Path:
        codex_home = os.environ.get("CODEX_HOME")
        if codex_home:
            return Path(codex_home).expanduser()
        return Path.home() / ".codex"

    @staticmethod
    def find_codex_bin() -> str | None:
        for name in ("codex", "codex.cmd"):
            codex_bin = shutil.which(name)
            if codex_bin:
                return codex_bin
        return None

    @staticmethod
    def read_codex_version(codex_bin: str) -> str | None:
        try:
            result = subprocess.run(
                [codex_bin, "--version"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        version = result.stdout.strip() or result.stderr.strip()
        return version or None

    @staticmethod
    def read_default_model(codex_home: Path | None = None) -> str | None:
        config_path = (codex_home or CodexAppServerStatusService.default_codex_home()) / "config.toml"
        if not config_path.exists():
            return None

        try:
            with config_path.open("rb") as f:
                config = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return None

        model = config.get("model")
        return model if isinstance(model, str) and model else None

    @staticmethod
    def auth_available(codex_home: Path | None = None) -> bool:
        home = codex_home or CodexAppServerStatusService.default_codex_home()
        return (home / "auth.json").exists()

    @staticmethod
    def get_status(codex_home: Path | None = None) -> CodexAppServerStatus:
        home = codex_home or CodexAppServerStatusService.default_codex_home()
        codex_bin = CodexAppServerStatusService.find_codex_bin()
        auth_available = CodexAppServerStatusService.auth_available(home)
        return CodexAppServerStatus(
            codex_cli_available=codex_bin is not None,
            codex_version=CodexAppServerStatusService.read_codex_version(codex_bin) if codex_bin else None,
            auth_available=auth_available,
            default_model=CodexAppServerStatusService.read_default_model(home),
            ready=codex_bin is not None and auth_available,
        )

    @staticmethod
    def get_model_suggestions() -> list[dict]:
        default_model = CodexAppServerStatusService.read_default_model()
        if not default_model:
            return []
        return [{"id": default_model, "object": "model"}]

    @staticmethod
    def assert_ready() -> None:
        status = CodexAppServerStatusService.get_status()
        if status.ready:
            return

        missing = []
        if not status.codex_cli_available:
            missing.append("Codex CLI")
        if not status.auth_available:
            missing.append("Codex auth")
        raise RuntimeError(f"Codex app-server is not ready: missing {', '.join(missing)}")
