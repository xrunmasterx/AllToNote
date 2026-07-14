from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


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
