from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import app.adapters.video_packs.official_video_pack_probe as probe_module
from app.adapters.video_packs.official_video_pack_probe import (
    probe_official_video_pack_entrypoints,
)
from app.core.errors import DomainError


class _Completed:
    def __init__(self, payload: object) -> None:
        self.stdout = json.dumps(payload).encode("utf-8")


class _ToolCompleted:
    def __init__(self, name: str) -> None:
        self.stdout = f"{name} version 8.1.2\n".encode("utf-8")


def test_media_basic_probe_checks_dependencies_and_bundled_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    expected = {"requests": "2.32.3", "yt-dlp": "2026.7.4"}

    def run(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> _Completed | _ToolCompleted:
        calls.append(command)
        if command[0].endswith("python.exe"):
            return _Completed(expected)
        return _ToolCompleted(Path(command[0]).stem)

    monkeypatch.setattr(probe_module.subprocess, "run", run)

    probe_official_video_pack_entrypoints(
        "media-basic",
        {
            "python": tmp_path / "python.exe",
            "ffmpeg": tmp_path / "ffmpeg.exe",
            "ffprobe": tmp_path / "ffprobe.exe",
        },
    )

    assert [Path(command[0]).stem for command in calls] == [
        "python",
        "ffmpeg",
        "ffprobe",
    ]
    assert calls[0][1:3] == ("-I", "-B")


def test_media_basic_probe_rejects_empty_tool_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = {"requests": "2.32.3", "yt-dlp": "2026.7.4"}

    def run(command: tuple[str, ...], **_kwargs: object) -> _Completed:
        if command[0].endswith("python.exe"):
            return _Completed(expected)
        completed = _Completed(None)
        completed.stdout = b""
        return completed

    monkeypatch.setattr(probe_module.subprocess, "run", run)

    with pytest.raises(DomainError, match="pack_probe_failed"):
        probe_official_video_pack_entrypoints(
            "media-basic",
            {
                "python": tmp_path / "python.exe",
                "ffmpeg": tmp_path / "ffmpeg.exe",
                "ffprobe": tmp_path / "ffprobe.exe",
            },
        )


def test_video_pack_probe_rejects_dependency_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        probe_module.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed({"requests": "wrong"}),
    )

    with pytest.raises(DomainError, match="pack_probe_failed"):
        probe_official_video_pack_entrypoints(
            "media-basic",
            {
                "python": tmp_path / "python.exe",
                "ffmpeg": tmp_path / "ffmpeg.exe",
                "ffprobe": tmp_path / "ffprobe.exe",
            },
        )


def test_video_pack_probe_maps_timeout_to_stable_domain_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired("python", 60)

    monkeypatch.setattr(probe_module.subprocess, "run", timeout)

    with pytest.raises(DomainError, match="pack_probe_failed"):
        probe_official_video_pack_entrypoints(
            "transcribe-cpu",
            {"python": tmp_path / "python.exe"},
        )
