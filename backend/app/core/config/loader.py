from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import tomli_w
from filelock import FileLock
from platformdirs import user_config_path

from app.core.config.model import (
    ProviderProfileConfig,
    RecipeDefaults,
    RuntimeConfig,
    TranscriberProfileConfig,
)
from app.core.errors import DomainError, ErrorCategory


CONFIG_VERSION = 1
_CONFIG_FILENAME = "config.toml"
_TOP_LEVEL_KEYS = frozenset(
    {
        "config_version",
        "default_workspace",
        "default_provider_profile",
        "default_transcriber_profile",
        "providers",
        "transcribers",
        "ffmpeg_path",
        "recipe_defaults",
        "log_level",
        "work_directory",
    }
)
_PROVIDER_KEYS = frozenset({"type", "base_url", "default_model", "credential_ref"})
_TRANSCRIBER_KEYS = frozenset(
    {"type", "model", "device", "compute_type", "credential_ref"}
)
_RECIPE_KEYS = frozenset(
    {"output_language", "quality_preset", "style", "screenshot_policy"}
)
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "oauth_token",
        "password",
        "secret",
        "token",
    }
)
_ENVIRONMENT_FIELDS = {
    "ALLTONOTE_DEFAULT_WORKSPACE": "default_workspace",
    "ALLTONOTE_DEFAULT_PROVIDER_PROFILE": "default_provider_profile",
    "ALLTONOTE_DEFAULT_TRANSCRIBER_PROFILE": "default_transcriber_profile",
    "ALLTONOTE_FFMPEG_PATH": "ffmpeg_path",
    "ALLTONOTE_LOG_LEVEL": "log_level",
    "ALLTONOTE_WORK_DIRECTORY": "work_directory",
}
_RECIPE_ENVIRONMENT_FIELDS = {
    "ALLTONOTE_RECIPE_OUTPUT_LANGUAGE": "output_language",
    "ALLTONOTE_RECIPE_QUALITY_PRESET": "quality_preset",
    "ALLTONOTE_RECIPE_STYLE": "style",
    "ALLTONOTE_RECIPE_SCREENSHOT_POLICY": "screenshot_policy",
}


def runtime_config_path() -> Path:
    return Path(user_config_path("AllToNote")) / _CONFIG_FILENAME


def _config_error(code: str, message: str, **details: object) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message, details)


def _normalized_field_name(field: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")


def _reject_secret_fields(value: object, path: tuple[str, ...] = ()) -> None:
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        if type(key) is not str:
            continue
        key_path = (*path, key)
        if _normalized_field_name(key) in _SECRET_FIELD_NAMES:
            raise _config_error(
                "config_secret_forbidden",
                "Secrets must be stored through the credential broker",
                field=".".join(key_path),
            )
        _reject_secret_fields(item, key_path)


def _reject_unknown_keys(
    value: Mapping[object, object], allowed: frozenset[str], path: str
) -> None:
    for key in value:
        if type(key) is not str or key not in allowed:
            field = f"{path}.{key}" if path else str(key)
            raise _config_error(
                "config_unknown_key", "Runtime configuration contains an unknown key", field=field
            )


def _require_mapping(value: object, field: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise _config_error(
            "config_type_invalid", "Runtime configuration field has an invalid type", field=field
        )
    return value


def _require_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise _config_error(
            "config_type_invalid", "Runtime configuration field has an invalid type", field=field
        )
    return value


def _validate_partial_config(
    data: Mapping[object, object], *, require_profile_type: bool = False
) -> None:
    _reject_secret_fields(data)
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "")

    if "config_version" in data:
        version = data["config_version"]
        if type(version) is not int:
            raise _config_error(
                "config_type_invalid",
                "Runtime configuration field has an invalid type",
                field="config_version",
            )
        if version > CONFIG_VERSION:
            raise _config_error(
                "config_version_unsupported",
                "Runtime configuration major version is not supported",
                config_version=version,
                supported_version=CONFIG_VERSION,
            )
        if version != CONFIG_VERSION:
            raise _config_error(
                "config_version_invalid",
                "Runtime configuration major version is invalid",
                config_version=version,
            )

    string_fields = (
        "default_workspace",
        "default_provider_profile",
        "default_transcriber_profile",
        "ffmpeg_path",
        "log_level",
        "work_directory",
    )
    for field in string_fields:
        if field in data:
            _require_string(data[field], field)

    if "providers" in data:
        providers = _require_mapping(data["providers"], "providers")
        for profile_id, raw_profile in providers.items():
            profile_path = f"providers.{profile_id}"
            _require_string(profile_id, "providers profile id")
            profile = _require_mapping(raw_profile, profile_path)
            _reject_unknown_keys(profile, _PROVIDER_KEYS, profile_path)
            if require_profile_type and "type" not in profile:
                raise _config_error(
                    "config_required_key_missing",
                    "Runtime configuration is missing a required key",
                    field=f"{profile_path}.type",
                )
            for key, item in profile.items():
                _require_string(item, f"{profile_path}.{key}")

    if "transcribers" in data:
        transcribers = _require_mapping(data["transcribers"], "transcribers")
        for profile_id, raw_profile in transcribers.items():
            profile_path = f"transcribers.{profile_id}"
            _require_string(profile_id, "transcribers profile id")
            profile = _require_mapping(raw_profile, profile_path)
            _reject_unknown_keys(profile, _TRANSCRIBER_KEYS, profile_path)
            if require_profile_type and "type" not in profile:
                raise _config_error(
                    "config_required_key_missing",
                    "Runtime configuration is missing a required key",
                    field=f"{profile_path}.type",
                )
            for key, item in profile.items():
                _require_string(item, f"{profile_path}.{key}")

    if "recipe_defaults" in data:
        recipe = _require_mapping(data["recipe_defaults"], "recipe_defaults")
        _reject_unknown_keys(recipe, _RECIPE_KEYS, "recipe_defaults")
        for key, item in recipe.items():
            _require_string(item, f"recipe_defaults.{key}")


def _recipe_v1_defaults() -> dict[str, object]:
    return {
        "config_version": CONFIG_VERSION,
        "default_provider_profile": "default",
        "default_transcriber_profile": "default",
        "providers": {},
        "transcribers": {},
        "recipe_defaults": {
            "output_language": "zh-CN",
            "quality_preset": "balanced",
            "style": "structured",
            "screenshot_policy": "off",
        },
        "log_level": "INFO",
    }


def _environment_overrides(environ: Mapping[str, str]) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for environment_name, field in _ENVIRONMENT_FIELDS.items():
        if environment_name in environ:
            overrides[field] = environ[environment_name]

    recipe_overrides = {
        field: environ[environment_name]
        for environment_name, field in _RECIPE_ENVIRONMENT_FIELDS.items()
        if environment_name in environ
    }
    if recipe_overrides:
        overrides["recipe_defaults"] = recipe_overrides
    return overrides


def _deep_merge(base: dict[str, object], override: Mapping[object, object]) -> None:
    for key, value in override.items():
        assert type(key) is str
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _deep_merge(current, value)
        else:
            base[key] = value


def _read_toml(path: Path) -> Mapping[object, object]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise _config_error(
            "config_toml_invalid", "Runtime configuration TOML is invalid"
        ) from exc
    except OSError as exc:
        raise _config_error(
            "config_read_failed", "Runtime configuration could not be read"
        ) from exc


def _runtime_config_from_mapping(data: Mapping[str, object]) -> RuntimeConfig:
    provider_values = _require_mapping(data["providers"], "providers")
    providers = {
        profile_id: ProviderProfileConfig(
            provider_type=profile["type"],
            base_url=profile.get("base_url"),
            default_model=profile.get("default_model"),
            credential_ref=profile.get("credential_ref"),
        )
        for profile_id, profile in provider_values.items()
    }
    transcriber_values = _require_mapping(data["transcribers"], "transcribers")
    transcribers = {
        profile_id: TranscriberProfileConfig(
            transcriber_type=profile["type"],
            model=profile.get("model"),
            device=profile.get("device"),
            compute_type=profile.get("compute_type"),
            credential_ref=profile.get("credential_ref"),
        )
        for profile_id, profile in transcriber_values.items()
    }
    recipe_values = _require_mapping(data["recipe_defaults"], "recipe_defaults")
    return RuntimeConfig(
        config_version=data["config_version"],
        default_workspace=Path(data["default_workspace"])
        if "default_workspace" in data
        else None,
        default_provider_profile=data["default_provider_profile"],
        default_transcriber_profile=data["default_transcriber_profile"],
        providers=providers,
        transcribers=transcribers,
        ffmpeg_path=Path(data["ffmpeg_path"]) if "ffmpeg_path" in data else None,
        recipe_defaults=RecipeDefaults(
            output_language=recipe_values["output_language"],
            quality_preset=recipe_values["quality_preset"],
            style=recipe_values["style"],
            screenshot_policy=recipe_values["screenshot_policy"],
        ),
        log_level=data["log_level"],
        work_directory=Path(data["work_directory"])
        if "work_directory" in data
        else None,
    )


def load_runtime_config(
    path: Path | None,
    environ: Mapping[str, str],
    *,
    cli_overrides: Mapping[str, object] | None = None,
) -> RuntimeConfig:
    resolved_path = Path(path) if path is not None else runtime_config_path()
    config_exists = resolved_path.exists()
    runtime_values = _read_toml(resolved_path)
    if config_exists and "config_version" not in runtime_values:
        raise _config_error(
            "config_version_missing",
            "Runtime configuration must declare its major version",
        )
    _validate_partial_config(runtime_values, require_profile_type=True)

    environment_values = _environment_overrides(environ)
    command_line_values = cli_overrides or {}
    _validate_partial_config(command_line_values)

    effective = _recipe_v1_defaults()
    _deep_merge(effective, runtime_values)
    _deep_merge(effective, environment_values)
    _deep_merge(effective, command_line_values)
    _validate_partial_config(effective, require_profile_type=True)
    return _runtime_config_from_mapping(effective)


def _without_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _runtime_config_to_mapping(config: RuntimeConfig) -> dict[str, object]:
    values: dict[str, object] = {
        "config_version": config.config_version,
        "default_provider_profile": config.default_provider_profile,
        "default_transcriber_profile": config.default_transcriber_profile,
        "providers": {
            profile_id: _without_none(
                {
                    "type": profile.provider_type,
                    "base_url": profile.base_url,
                    "default_model": profile.default_model,
                    "credential_ref": profile.credential_ref,
                }
            )
            for profile_id, profile in config.providers.items()
        },
        "transcribers": {
            profile_id: _without_none(
                {
                    "type": profile.transcriber_type,
                    "model": profile.model,
                    "device": profile.device,
                    "compute_type": profile.compute_type,
                    "credential_ref": profile.credential_ref,
                }
            )
            for profile_id, profile in config.transcribers.items()
        },
        "recipe_defaults": {
            "output_language": config.recipe_defaults.output_language,
            "quality_preset": config.recipe_defaults.quality_preset,
            "style": config.recipe_defaults.style,
            "screenshot_policy": config.recipe_defaults.screenshot_policy,
        },
        "log_level": config.log_level,
    }
    if config.default_workspace is not None:
        values["default_workspace"] = str(config.default_workspace)
    if config.ffmpeg_path is not None:
        values["ffmpeg_path"] = str(config.ffmpeg_path)
    if config.work_directory is not None:
        values["work_directory"] = str(config.work_directory)
    return values


def write_runtime_config(config: RuntimeConfig, path: Path | None = None) -> Path:
    resolved_path = Path(path) if path is not None else runtime_config_path()
    values = _runtime_config_to_mapping(config)
    _validate_partial_config(values, require_profile_type=True)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    lock = FileLock(f"{resolved_path}.lock")
    with lock:
        temporary_path = resolved_path.with_name(
            f".{resolved_path.name}.{uuid4().hex}.tmp"
        )
        try:
            with temporary_path.open("wb") as stream:
                tomli_w.dump(values, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, resolved_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return resolved_path
