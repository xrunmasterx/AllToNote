from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.sources.legacy_video import LegacyVideoSourceAdapter
from app.adapters.video_packs.official_video_pack import MEDIA_BASIC
from app.adapters.video_packs.official_video_pack_resolver import (
    ResolvedOfficialVideoPack,
)
from app.adapters.video_packs.packed_bilibili_source import (
    PackedBilibiliVideoSourceAdapter,
)
from app.core.errors import DomainError
from app.core.ports.source import SubtitleAvailability


class _Token:
    def __init__(self) -> None:
        self.calls = 0

    def raise_if_cancelled(self) -> None:
        self.calls += 1


def _resolved(tmp_path: Path) -> ResolvedOfficialVideoPack:
    generation = tmp_path / "generation"
    python = generation / "python" / "python.exe"
    ffmpeg = generation / "bin" / "ffmpeg.exe"
    ffprobe = generation / "bin" / "ffprobe.exe"
    for path in (python, ffmpeg, ffprobe):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    return ResolvedOfficialVideoPack(
        pack_id=MEDIA_BASIC.pack_id,
        pack_version=MEDIA_BASIC.pack_version,
        platform="windows-x86_64",
        manifest_sha256="sha256:" + "b" * 64,
        generation=generation.resolve(),
        entrypoints={
            "python": python.resolve(),
            "ffmpeg": ffmpeg.resolve(),
            "ffprobe": ffprobe.resolve(),
        },
    )


def _adapter(
    tmp_path: Path,
    result: dict[str, object],
    captured: dict,
    *,
    probe_result: dict[str, object] | None = None,
) -> object:
    def runner(command, request, **kwargs):
        captured.update(command=command, request=request, **kwargs)
        return result

    def probe_runner(command, request, **kwargs):
        captured["probe"] = {
            "command": command,
            "request": request,
            **kwargs,
        }
        return (
            {"format": {"duration": "12.000000"}}
            if probe_result is None
            else probe_result
        )

    return PackedBilibiliVideoSourceAdapter(
        LegacyVideoSourceAdapter(local_machine_id="machine-a"),
        _resolved(tmp_path),
        cookie_resolver=lambda: "SESSDATA=secret",
        runner=runner,
        probe_runner=probe_runner,
        backend_root=Path(__file__).resolve().parents[2],
    )


def _result(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "identity": {
            "worker_protocol_version": 1,
            "downloader": "yt-dlp",
            "downloader_version": "2026.7.4",
            "http_client": "requests",
            "http_client_version": "2.32.3",
        },
        "title": "课程",
        "duration_ms": 2_500,
        "cover_uri": "https://i.example/cover.jpg",
        "media_path": None,
        "subtitle_status": "available",
        "subtitle": {
            "language": "zh",
            "segments": [{"start": 0.0, "end": 1.0, "text": "字幕"}],
        },
        **overrides,
    }


def test_adapter_uses_frozen_worker_and_keeps_cookie_in_stdin(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    adapter = _adapter(tmp_path, _result(), captured)
    source = adapter.resolve(
        "https://www.bilibili.com/video/BV1vc411b7Wa?token=discarded"
    )
    token = _Token()

    acquired = adapter.acquire(
        source,
        need_media=False,
        need_subtitles=True,
        output_dir=tmp_path / "attempt",
        token=token,
    )

    assert captured["command"][:3] == (
        str(_resolved(tmp_path).entrypoints["python"]),
        "-B",
        "-m",
    )
    assert captured["request"]["canonical_uri"].endswith("?p=1")
    assert captured["request"]["cookie"] == "SESSDATA=secret"
    assert "secret" not in repr(acquired)
    assert captured["check_cancelled"] == token.raise_if_cancelled
    assert acquired.subtitle_availability is SubtitleAvailability.AVAILABLE
    assert acquired.opaque_subtitle.language == "zh"
    assert acquired.opaque_subtitle.segments[0]["text"] == "字幕"


def test_adapter_resolves_contained_media_result(tmp_path: Path) -> None:
    output = tmp_path / "attempt"
    output.mkdir()
    media = output / "audio.mp3"
    media.write_bytes(b"audio")
    captured: dict[str, object] = {}
    adapter = _adapter(
        tmp_path,
        _result(
            media_path="audio.mp3",
            subtitle_status="not_supported",
            subtitle=None,
        ),
        captured,
    )
    source = adapter.resolve(
        "https://www.bilibili.com/video/BV1vc411b7Wa?p=2"
    )

    acquired = adapter.acquire(
        source,
        need_media=True,
        need_subtitles=False,
        output_dir=output,
        token=_Token(),
    )

    assert acquired.media_path == media.resolve()
    assert acquired.subtitle_availability is SubtitleAvailability.NOT_SUPPORTED


def test_adapter_rejects_worker_identity_drift(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    result = _result()
    result["identity"] = {
        **result["identity"],
        "downloader_version": "future",
    }
    adapter = _adapter(tmp_path, result, captured)
    source = adapter.resolve(
        "https://www.bilibili.com/video/BV1vc411b7Wa"
    )

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            source,
            need_media=False,
            output_dir=tmp_path / "attempt",
            token=_Token(),
        )

    assert caught.value.code == "source_result_invalid"


def test_adapter_rejects_non_bilibili_remote_source(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, _result(), {})

    with pytest.raises(DomainError) as caught:
        adapter.resolve("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert caught.value.code == "source_unsupported"


def test_local_source_still_uses_existing_hardened_path(tmp_path: Path) -> None:
    local = tmp_path / "local.mp4"
    local.write_bytes(b"video")
    captured: dict[str, object] = {}
    adapter = _adapter(
        tmp_path,
        _result(),
        captured,
    )
    source = adapter.resolve(str(local))

    acquired = adapter.acquire(
        source,
        need_media=False,
        need_subtitles=False,
        output_dir=tmp_path / "attempt",
        token=_Token(),
    )

    assert acquired.source.connector_id == "local"
    assert acquired.title == "local"
    assert acquired.duration_ms == 12_000
    assert captured["probe"]["command"] == (
        str(_resolved(tmp_path).entrypoints["ffprobe"]),
        "-hide_banner",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(local),
    )
    assert captured["probe"]["request"] == {}


@pytest.mark.parametrize(
    "probe_result",
    [
        {},
        {"format": {}},
        {"format": {"duration": 12}},
        {"format": {"duration": "0"}},
        {"format": {"duration": "NaN"}},
    ],
)
def test_local_source_rejects_invalid_probe_result(
    tmp_path: Path,
    probe_result: dict[str, object],
) -> None:
    local = tmp_path / "local.mp4"
    local.write_bytes(b"video")
    adapter = _adapter(
        tmp_path,
        _result(),
        {},
        probe_result=probe_result,
    )

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            adapter.resolve(str(local)),
            need_media=False,
            need_subtitles=False,
            output_dir=tmp_path / "attempt",
            token=_Token(),
        )

    assert caught.value.code == "source_result_invalid"
    assert str(local) not in str(caught.value)
