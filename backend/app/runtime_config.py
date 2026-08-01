from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.core.config.loader import (
    load_runtime_config,
    runtime_config_mapping,
    update_runtime_config,
)
from app.core.config.model import (
    SEMANTIC_CONFIG_KEYS,
    JobConfigSnapshot,
    ProviderProfileConfig,
    RuntimeConfig,
)
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.identity import is_executor_identity
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


_PROFILE_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
_SUPPORTED_PROVIDER_TYPES = frozenset({"codex-app-server"})
_CODEX_CREDENTIAL_REF = "codex/local-login"
_SETTABLE_KEYS = frozenset(
    {
        "default_workspace",
        "default_provider_profile",
        "default_verifier_provider_profile",
        "default_transcriber_profile",
        "ffmpeg_path",
        "recipe_defaults.output_language",
        "recipe_defaults.quality_preset",
        "recipe_defaults.style",
        "recipe_defaults.screenshot_policy",
        "log_level",
        "work_directory",
    }
)


class ConfigDrift(StrEnum):
    NONE = "none"
    NON_SEMANTIC = "non-semantic"
    SEMANTIC = "semantic"


@dataclass(frozen=True)
class EffectiveRuntimeConfig:
    config: RuntimeConfig
    values: Mapping[str, object]
    digest: str
    semantic_digest: str
    profile: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def job_snapshot(self) -> JobConfigSnapshot:
        return JobConfigSnapshot(
            snapshot_version=1,
            values=self.values,
            digest=self.digest,
            semantic_digest=self.semantic_digest,
        )


def _canonical_json(values: Mapping[str, object]) -> str:
    return json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def effective_runtime_config(
    config: RuntimeConfig,
    *,
    profile: str | None = None,
) -> EffectiveRuntimeConfig:
    values = runtime_config_mapping(config)
    semantic_values = {
        key: value for key, value in values.items() if key in SEMANTIC_CONFIG_KEYS
    }
    return EffectiveRuntimeConfig(
        config=config,
        values=values,
        digest=sha256_digest(_canonical_json(values)),
        semantic_digest=sha256_digest(_canonical_json(semantic_values)),
        profile=profile,
    )


def classify_config_drift(
    previous: EffectiveRuntimeConfig,
    current: EffectiveRuntimeConfig,
) -> ConfigDrift:
    if previous.digest == current.digest:
        return ConfigDrift.NONE
    if previous.semantic_digest == current.semantic_digest:
        return ConfigDrift.NON_SEMANTIC
    return ConfigDrift.SEMANTIC


class RuntimeConfigService:
    def __init__(
        self,
        *,
        paths: RuntimePaths | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._paths = paths or resolve_runtime_paths()
        self._environ = dict(environ or {})

    @property
    def paths(self) -> RuntimePaths:
        return self._paths

    def effective(
        self,
        *,
        profile: str | None = None,
        cli_overrides: Mapping[str, object] | None = None,
    ) -> EffectiveRuntimeConfig:
        profile_path = self._profile_path(profile) if profile is not None else None
        config = load_runtime_config(
            self._paths.config_file,
            self._environ,
            cli_overrides=cli_overrides,
            profile_path=profile_path,
        )
        return effective_runtime_config(config, profile=profile)

    def validate(
        self,
        *,
        profile: str | None = None,
    ) -> EffectiveRuntimeConfig:
        effective = self.effective(profile=profile)
        _validate_provider_defaults(effective.config)
        return effective

    def list_profiles(self) -> tuple[str, ...]:
        profile_dir = self._paths.config_dir / "profiles"
        if not profile_dir.exists():
            return ()
        if not profile_dir.is_dir():
            raise DomainError(
                "config_profiles_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Runtime configuration profiles location is invalid",
            )
        return tuple(
            sorted(
                path.stem
                for path in profile_dir.glob("*.toml")
                if _PROFILE_ID.fullmatch(path.stem) is not None and path.is_file()
            )
        )

    def set_value(self, key: str, value: str) -> EffectiveRuntimeConfig:
        if key not in _SETTABLE_KEYS or type(value) is not str:
            raise DomainError(
                "config_key_not_settable",
                ErrorCategory.INVALID_REQUEST,
                "Runtime configuration key cannot be set by this command",
            )
        def apply(current: RuntimeConfig) -> RuntimeConfig:
            if key.startswith("recipe_defaults."):
                recipe_key = key.removeprefix("recipe_defaults.")
                return replace(
                    current,
                    recipe_defaults=replace(
                        current.recipe_defaults,
                        **{recipe_key: value},
                    ),
                )
            if key in {"default_workspace", "ffmpeg_path", "work_directory"}:
                return replace(current, **{key: Path(value)})
            updated = replace(current, **{key: value})
            if key in {
                "default_provider_profile",
                "default_verifier_provider_profile",
            }:
                _validate_provider_defaults(updated)
            return updated

        updated = update_runtime_config(apply, self._paths.config_file)
        return effective_runtime_config(updated)

    def set_provider_profile(
        self,
        profile: str,
        *,
        provider_type: str,
        default_model: str,
        credential_ref: str | None = None,
    ) -> tuple[EffectiveRuntimeConfig, bool]:
        if type(profile) is not str or _PROFILE_ID.fullmatch(profile) is None:
            raise DomainError(
                "config_provider_profile_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Provider profile is invalid",
            )
        if (
            type(provider_type) is not str
            or provider_type not in _SUPPORTED_PROVIDER_TYPES
        ):
            raise DomainError(
                "config_provider_type_unsupported",
                ErrorCategory.INVALID_REQUEST,
                "Provider type is not supported",
            )
        if (
            type(default_model) is not str
            or not is_executor_identity(default_model)
        ):
            raise DomainError(
                "config_provider_model_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Provider model is invalid",
            )
        normalized_credential_ref = (
            _CODEX_CREDENTIAL_REF if credential_ref is None else credential_ref
        )
        if normalized_credential_ref != _CODEX_CREDENTIAL_REF:
            raise DomainError(
                "config_provider_credential_ref_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Provider credential reference is invalid",
            )

        provider = ProviderProfileConfig(
            provider_type=provider_type,
            default_model=default_model,
            credential_ref=normalized_credential_ref,
        )
        changed = False

        def apply(current: RuntimeConfig) -> RuntimeConfig:
            nonlocal changed
            existing = current.providers.get(profile)
            if existing == provider:
                return current
            if existing is not None and (
                existing.provider_type != provider_type
                or existing.credential_ref != normalized_credential_ref
                or existing.base_url is not None
            ):
                raise DomainError(
                    "config_provider_profile_conflict",
                    ErrorCategory.CONFLICT,
                    "Provider profile conflicts with the admitted Runtime provider",
                )
            changed = True
            updated = replace(
                current,
                providers={**current.providers, profile: provider},
            )
            if profile in {
                current.default_provider_profile,
                current.default_verifier_provider_profile,
            }:
                _validate_provider_defaults(updated)
            return updated

        updated = update_runtime_config(apply, self._paths.config_file)
        return effective_runtime_config(updated), changed

    def _profile_path(self, profile: str) -> Path:
        if type(profile) is not str or _PROFILE_ID.fullmatch(profile) is None:
            raise DomainError(
                "config_profile_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Runtime configuration profile is invalid",
            )
        profile_dir = (self._paths.config_dir / "profiles").resolve(strict=False)
        candidate = profile_dir / f"{profile}.toml"
        if not candidate.is_file():
            raise DomainError(
                "config_profile_not_found",
                ErrorCategory.INVALID_REQUEST,
                "Runtime configuration profile was not found",
            )
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise DomainError(
                "config_profile_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Runtime configuration profile is invalid",
            ) from error
        if not resolved.is_relative_to(profile_dir):
            raise DomainError(
                "config_profile_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Runtime configuration profile is invalid",
            )
        return resolved


def _validate_provider_defaults(config: RuntimeConfig) -> None:
    composer = config.providers.get(config.default_provider_profile)
    if composer is None:
        if (
            config.default_provider_profile == "default"
            and config.default_verifier_provider_profile is None
        ):
            return
        raise DomainError(
            "default_provider_profile_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The configured default provider profile is not usable",
        )
    if (
        composer.provider_type != "codex-app-server"
        or not is_executor_identity(composer.default_model)
    ):
        raise DomainError(
            "default_provider_profile_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The configured default provider profile is not usable",
        )

    verifier_profile = config.default_verifier_provider_profile
    if verifier_profile is None:
        return
    verifier = config.providers.get(verifier_profile)
    if (
        verifier is None
        or verifier.provider_type != "codex-app-server"
        or not is_executor_identity(verifier.default_model)
    ):
        raise DomainError(
            "document_verifier_model_required",
            ErrorCategory.INVALID_REQUEST,
            "The configured Document verifier profile has no frozen model",
        )
    if composer.default_model == verifier.default_model:
        raise DomainError(
            "document_verifier_not_independent",
            ErrorCategory.INVALID_REQUEST,
            "The configured Document verifier must use a different model",
        )


__all__ = [
    "ConfigDrift",
    "EffectiveRuntimeConfig",
    "RuntimeConfigService",
    "classify_config_drift",
    "effective_runtime_config",
]
