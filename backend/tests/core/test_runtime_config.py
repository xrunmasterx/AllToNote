from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config.loader import load_runtime_config, write_runtime_config
from app.core.config.model import RuntimeConfig
from app.core.errors import DomainError, ErrorCategory


def test_unknown_config_key_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("config_version=1\nunknown=true\n", encoding="utf-8")

    with pytest.raises(DomainError, match="config_unknown_key") as exc_info:
        load_runtime_config(path, {})

    assert exc_info.value.category is ErrorCategory.INVALID_REQUEST


@pytest.mark.parametrize("field", ["api_key", "cookie", "oauth_token", "password"])
def test_secret_fields_are_rejected_before_schema_validation(
    tmp_path: Path, field: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'config_version=1\n[providers.main]\n{field}="do-not-store"\n',
        encoding="utf-8",
    )

    with pytest.raises(DomainError, match="config_secret_forbidden") as exc_info:
        load_runtime_config(path, {})

    assert "do-not-store" not in str(exc_info.value)
    assert "do-not-store" not in repr(exc_info.value)


def test_higher_major_config_version_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("config_version=2\n", encoding="utf-8")

    with pytest.raises(DomainError, match="config_version_unsupported"):
        load_runtime_config(path, {})


def test_existing_config_requires_explicit_version(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('default_provider_profile="toml-profile"\n', encoding="utf-8")

    with pytest.raises(DomainError, match="config_version_missing"):
        load_runtime_config(path, {})


def test_non_secret_precedence_is_cli_env_toml_then_recipe_default(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version=1\ndefault_provider_profile="toml-profile"\n',
        encoding="utf-8",
    )
    environ = {"ALLTONOTE_DEFAULT_PROVIDER_PROFILE": "env-profile"}

    from_cli = load_runtime_config(
        path,
        environ,
        cli_overrides={"default_provider_profile": "cli-profile"},
    )
    from_env = load_runtime_config(path, environ)
    from_toml = load_runtime_config(path, {})
    from_recipe_default = load_runtime_config(tmp_path / "missing.toml", {})

    assert from_cli.default_provider_profile == "cli-profile"
    assert from_env.default_provider_profile == "env-profile"
    assert from_toml.default_provider_profile == "toml-profile"
    assert from_recipe_default.default_provider_profile == "default"


def test_cli_can_override_one_nested_non_secret_profile_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            (
                "config_version=1",
                "[providers.main]",
                'type="openai"',
                'base_url="https://toml.example"',
            )
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(
        path,
        {},
        cli_overrides={
            "providers": {"main": {"base_url": "https://cli.example"}}
        },
    )

    assert config.providers["main"].provider_type == "openai"
    assert config.providers["main"].base_url == "https://cli.example"


def test_cli_overrides_are_strict_and_cannot_contain_secrets(tmp_path: Path) -> None:
    with pytest.raises(DomainError, match="config_unknown_key"):
        load_runtime_config(
            tmp_path / "missing.toml", {}, cli_overrides={"unknown": True}
        )

    with pytest.raises(DomainError, match="config_secret_forbidden"):
        load_runtime_config(
            tmp_path / "missing.toml", {}, cli_overrides={"api_key": "secret"}
        )


def test_default_path_uses_platformdirs_user_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version=1\ndefault_transcriber_profile="local-whisper"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.core.config.loader.user_config_path", lambda app_name: tmp_path
    )

    config = load_runtime_config(None, {})

    assert config.default_transcriber_profile == "local-whisper"


def test_runtime_config_round_trips_through_locked_atomic_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    config = RuntimeConfig(
        default_workspace=tmp_path / "知识库",
        default_provider_profile="openai-main",
        ffmpeg_path=tmp_path / "tools" / "ffmpeg.exe",
    )

    write_runtime_config(config, path)

    assert load_runtime_config(path, {}) == config
    assert not tuple(tmp_path.glob("*.tmp"))
