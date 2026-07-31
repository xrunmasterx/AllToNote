from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    modules: tuple[str, ...]
    version: str = "1"


@dataclass(frozen=True)
class RuntimeCapability:
    key: str
    installed: bool
    version: str
    probe: str = "static"

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "installed": self.installed,
            "version": self.version,
            "probe": self.probe,
        }


_CAPABILITY_SPECS = (
    CapabilitySpec(
        "model.codex-app-server",
        ("app.adapters.models.codex_app_server_bridge",),
    ),
    CapabilitySpec(
        "model.openai-compatible",
        ("app.adapters.models.legacy_gpt",),
    ),
    CapabilitySpec(
        "portable.iwiki.v1",
        ("iwiki.portable",),
    ),
    CapabilitySpec(
        "recipe.video.acquire.bilibili",
        ("app.downloaders.bilibili_downloader", "yt_dlp"),
    ),
    CapabilitySpec(
        "recipe.video.acquire.douyin",
        ("app.downloaders.douyin_downloader", "requests"),
    ),
    CapabilitySpec(
        "recipe.video.acquire.kuaishou",
        ("app.downloaders.kuaishou_downloader", "requests"),
    ),
    CapabilitySpec(
        "recipe.video.acquire.local",
        ("app.downloaders.local_downloader",),
    ),
    CapabilitySpec(
        "recipe.video.acquire.youtube",
        ("app.downloaders.youtube_downloader", "yt_dlp"),
    ),
    CapabilitySpec(
        "recipe.video.compile.faithful-edition",
        ("app.core.application.faithful_edition_compiler",),
    ),
    CapabilitySpec(
        "recipe.video.compile.knowledge-note",
        ("app.core.application.video_compiler",),
    ),
    CapabilitySpec(
        "recipe.video.transcribe.bcut",
        ("app.transcriber.bcut",),
    ),
    CapabilitySpec(
        "recipe.video.transcribe.groq",
        ("app.transcriber.groq", "groq"),
    ),
    CapabilitySpec(
        "recipe.video.transcribe.kuaishou",
        ("app.transcriber.kuaishou",),
    ),
    CapabilitySpec(
        "recipe.video.transcribe.local.cpu",
        ("app.transcriber.whisper", "faster_whisper"),
    ),
    CapabilitySpec("transport.cli.v1", ("app.cli.main",)),
    CapabilitySpec("engine.lifecycle.v1", ("app.engine.client",)),
)


class CapabilityRegistry:
    def __init__(
        self,
        specs: tuple[CapabilitySpec, ...] = _CAPABILITY_SPECS,
    ) -> None:
        self._specs = tuple(specs)

    def snapshot(self) -> tuple[RuntimeCapability, ...]:
        capabilities = []
        for spec in self._specs:
            installed = True
            for module in spec.modules:
                try:
                    if find_spec(module) is None:
                        installed = False
                        break
                except (ImportError, ModuleNotFoundError, ValueError):
                    installed = False
                    break
            capabilities.append(
                RuntimeCapability(
                    key=spec.key,
                    installed=installed,
                    version=spec.version,
                )
            )
        return tuple(sorted(capabilities, key=lambda item: item.key))


__all__ = [
    "CapabilityRegistry",
    "CapabilitySpec",
    "RuntimeCapability",
]
