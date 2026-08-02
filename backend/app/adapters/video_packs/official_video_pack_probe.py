from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from app.adapters.video_packs.official_pack_process import (
    minimal_worker_environment,
)
from app.core.errors import DomainError, ErrorCategory


def probe_official_video_pack_entrypoints(
    pack_id: str,
    entrypoints: Mapping[str, Path],
) -> None:
    expected = (
        {"requests": "2.32.3", "yt-dlp": "2026.7.4"}
        if pack_id == "media-basic"
        else {
            "av": "14.2.0",
            "ctranslate2": "4.6.0",
            "faster-whisper": "1.1.1",
            "tokenizers": "0.21.1",
        }
    )
    script = (
        "import importlib.metadata as m,json;"
        f"names={tuple(expected)!r};"
        "versions={n:m.version(n) for n in names};"
        + (
            "import yt_dlp,requests;"
            if pack_id == "media-basic"
            else "import faster_whisper,ctranslate2,av,tokenizers;"
        )
        + "print(json.dumps(versions,sort_keys=True))"
    )
    environment = minimal_worker_environment()
    try:
        completed = subprocess.run(
            (str(entrypoints["python"]), "-I", "-B", "-c", script),
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        actual = json.loads(completed.stdout.decode("utf-8"))
        if actual != expected:
            raise ValueError("dependency identity mismatch")
        if pack_id == "media-basic":
            for name in ("ffmpeg", "ffprobe"):
                tool = subprocess.run(
                    (str(entrypoints[name]), "-version"),
                    check=True,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                    env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                first_line = tool.stdout.decode(
                    "utf-8", errors="replace"
                ).splitlines()[0]
                if not first_line.startswith(f"{name} version 8.1.2"):
                    raise ValueError("FFmpeg identity mismatch")
    except (
        IndexError,
        KeyError,
        OSError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        raise DomainError(
            "pack_probe_failed",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            f"The {pack_id} Pack failed its isolated dynamic probe",
        ) from error


__all__ = ["probe_official_video_pack_entrypoints"]
