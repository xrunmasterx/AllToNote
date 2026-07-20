from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SEMANTIC_CONFIG_KEYS = frozenset(
    {
        "config_version",
        "default_provider_profile",
        "default_transcriber_profile",
        "providers",
        "transcribers",
        "ffmpeg_path",
        "recipe_defaults",
    }
)


def _config_digest(values: Mapping[str, object]) -> str:
    payload = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JobConfigSnapshot:
    snapshot_version: int
    values: Mapping[str, object]
    digest: str
    semantic_digest: str

    def __post_init__(self) -> None:
        if self.snapshot_version != 1:
            raise ValueError("job_config_snapshot_version_invalid")
        if (
            _SHA256_DIGEST.fullmatch(self.digest) is None
            or _SHA256_DIGEST.fullmatch(self.semantic_digest) is None
        ):
            raise ValueError("job_config_snapshot_digest_invalid")
        try:
            normalized = json.loads(
                json.dumps(
                    dict(self.values),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, OverflowError):
            raise ValueError("job_config_snapshot_values_invalid") from None
        semantic_values = {
            key: value
            for key, value in normalized.items()
            if key in SEMANTIC_CONFIG_KEYS
        }
        if (
            _config_digest(normalized) != self.digest
            or _config_digest(semantic_values) != self.semantic_digest
        ):
            raise ValueError("job_config_snapshot_digest_invalid")
        object.__setattr__(self, "values", MappingProxyType(normalized))


@dataclass(frozen=True)
class ProviderProfileConfig:
    provider_type: str
    base_url: str | None = None
    default_model: str | None = None
    credential_ref: str | None = None


@dataclass(frozen=True)
class TranscriberProfileConfig:
    transcriber_type: str
    model: str | None = None
    device: str | None = None
    compute_type: str | None = None
    credential_ref: str | None = None


@dataclass(frozen=True)
class RecipeDefaults:
    output_language: str = "zh-CN"
    quality_preset: str = "balanced"
    style: str = "structured"
    screenshot_policy: str = "off"


@dataclass(frozen=True)
class RuntimeConfig:
    config_version: int = 1
    default_workspace: Path | None = None
    default_provider_profile: str = "default"
    default_transcriber_profile: str = "default"
    providers: Mapping[str, ProviderProfileConfig] = field(default_factory=dict)
    transcribers: Mapping[str, TranscriberProfileConfig] = field(default_factory=dict)
    ffmpeg_path: Path | None = None
    recipe_defaults: RecipeDefaults = field(default_factory=RecipeDefaults)
    log_level: str = "INFO"
    work_directory: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
        object.__setattr__(
            self, "transcribers", MappingProxyType(dict(self.transcribers))
        )
