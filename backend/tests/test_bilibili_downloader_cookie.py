import logging
from pathlib import Path

import pytest

from app.downloaders import bilibili_downloader as module


_REAL_YOUTUBE_DL = module.yt_dlp.YoutubeDL


class _FakeYoutubeDL:
    captured_opts: list[dict] = []
    cookie_contents: list[str] = []

    def __init__(self, opts):
        self.opts = opts
        self.captured_opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, video_url, download):
        cookiefile = self.opts.get("cookiefile")
        if cookiefile:
            self.cookie_contents.append(Path(cookiefile).read_text(encoding="utf-8"))

        if self.opts.get("merge_output_format"):
            Path(self.prepare_filename({"id": "BV1fixture", "format_id": "1080+a", "ext": "mkv"})).touch()

        if self.opts.get("writesubtitles"):
            return {
                "requested_subtitles": {
                    "zh": {
                        "ext": "srt",
                        "data": "1\n00:00:00,000 --> 00:00:01,000\n字幕\n",
                    }
                }
            }

        return {
            "id": "BV1fixture",
            "title": "Fixture video",
            "duration": 12,
            "thumbnail": "https://example.com/thumb.jpg",
            "format_id": "1080+a",
            "ext": "mkv",
        }

    def prepare_filename(self, info):
        return (self.opts["outtmpl"].replace("%(id)s", info["id"])
                .replace("%(format_id)s", info["format_id"])
                .replace("%(ext)s", info["ext"]))


class _FailingYoutubeDL(_FakeYoutubeDL):
    def extract_info(self, video_url, download):
        super().extract_info(video_url, download)
        raise RuntimeError("yt-dlp failed")


class _NoSubtitleFetcher:
    def fetch_subtitles(self, video_url):
        return None


def _configure(monkeypatch, youtube_dl=_FakeYoutubeDL, cookie="SESSDATA=secret; bili_jct=csrf"):
    class FakeCookieConfigManager:
        def get(self, platform):
            assert platform == "bilibili"
            return cookie

    _FakeYoutubeDL.captured_opts = []
    _FakeYoutubeDL.cookie_contents = []
    monkeypatch.setattr(module, "CookieConfigManager", FakeCookieConfigManager)
    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", youtube_dl)
    monkeypatch.setattr(module, "BilibiliSubtitleFetcher", _NoSubtitleFetcher)
    monkeypatch.setattr(module, "extract_video_id", lambda *_args: "BV1fixture")


def test_constructor_does_not_create_long_lived_cookiefile(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        module.tempfile,
        "NamedTemporaryFile",
        lambda *args, **kwargs: pytest.fail("constructor created a cookie file"),
    )

    module.BilibiliDownloader()


@pytest.mark.parametrize("method_name", ["download", "download_video", "download_subtitles"])
def test_each_ytdlp_operation_uses_and_removes_a_fresh_cookiefile(
    monkeypatch, tmp_path, method_name
):
    _configure(monkeypatch)
    downloader = module.BilibiliDownloader()

    getattr(downloader, method_name)(
        "https://www.bilibili.com/video/BV1fixture",
        output_dir=str(tmp_path),
    )

    opts = _FakeYoutubeDL.captured_opts[0]
    cookiefile = Path(opts["cookiefile"])
    assert not cookiefile.exists()
    assert ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tsecret\n" in (
        _FakeYoutubeDL.cookie_contents[0]
    )
    assert ".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\tcsrf\n" in (
        _FakeYoutubeDL.cookie_contents[0]
    )


def test_cookiefile_is_removed_when_ytdlp_raises(monkeypatch, tmp_path):
    _configure(monkeypatch, _FailingYoutubeDL)
    downloader = module.BilibiliDownloader()

    with pytest.raises(RuntimeError, match="yt-dlp failed"):
        downloader.download(
            "https://www.bilibili.com/video/BV1fixture",
            output_dir=str(tmp_path),
        )

    cookiefile = Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"])
    assert not cookiefile.exists()


def test_cookiefile_path_and_secret_are_not_logged(monkeypatch, tmp_path, caplog):
    _configure(monkeypatch)
    caplog.set_level(logging.INFO, logger=module.__name__)

    module.BilibiliDownloader().download(
        "https://www.bilibili.com/video/BV1fixture",
        output_dir=str(tmp_path),
    )

    cookiefile = _FakeYoutubeDL.captured_opts[0]["cookiefile"]
    output = "\n".join(record.getMessage() for record in caplog.records)
    assert cookiefile not in output
    assert "SESSDATA" not in output
    assert "secret" not in output
    assert "csrf" not in output


def test_no_cookie_does_not_create_cookiefile(monkeypatch, tmp_path):
    _configure(monkeypatch, cookie=None)
    monkeypatch.setattr(
        module.tempfile,
        "NamedTemporaryFile",
        lambda *args, **kwargs: pytest.fail("cookie file created without a cookie"),
    )

    module.BilibiliDownloader().download(
        "https://www.bilibili.com/video/BV1fixture",
        output_dir=str(tmp_path),
    )

    assert "cookiefile" not in _FakeYoutubeDL.captured_opts[0]


def _video(format_id, height, *, ext="mp4", fps=30, preference=0, tbr=2000,
           vcodec="avc1", acodec="none"):
    return {
        "format_id": format_id,
        "url": f"https://example.com/{format_id}.{ext}",
        "ext": ext, "width": height * 16 // 9, "height": height,
        "fps": fps, "preference": preference, "tbr": tbr,
        "vcodec": vcodec, "acodec": acodec,
    }


_AUDIO = {
    "format_id": "audio", "url": "https://example.com/audio.m4a",
    "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128,
}


@pytest.mark.parametrize("formats,expected_format,expected_ext", [
    ([_video("1080", 1080, preference=10),
      _video("2160", 2160, ext="webm", vcodec="vp9", preference=-10), _AUDIO],
     "2160+audio", "mkv"),
    ([_video("2160", 2160, fps=30), _video("1080", 1080, fps=60), _AUDIO],
     "2160+audio", "mkv"),
    ([_video("30fps", 1080, fps=30), _video("60fps", 1080, fps=60), _AUDIO],
     "60fps+audio", "mkv"),
    ([_video("low", 1080, tbr=2000), _video("high", 1080, tbr=8000), _AUDIO],
     "high+audio", "mkv"),
    ([_video("360", 360), _AUDIO], "360+audio", "mkv"),
    ([_video("combined", 1080, acodec="mp4a")], "combined", "mp4"),
    ([_video("silent", 2160, ext="webm", vcodec="vp9")], "silent", "webm"),
])
def test_video_download_selects_best_available_format_and_actual_path(
    monkeypatch, tmp_path, formats, expected_format, expected_ext,
):
    class SelectingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            assert download is True
            # Use yt-dlp's real selector, without accessing the network.
            with _REAL_YOUTUBE_DL({**self.opts, "quiet": True, "no_warnings": True}) as ydl:
                info = ydl.process_ie_result({
                    "id": "BV1fixture", "title": "Fixture", "formats": formats,
                }, download=False)
                assert info["format_id"] == expected_format
                assert info["ext"] == expected_ext
                Path(ydl.prepare_filename(info)).write_bytes(b"selected-video")
                return info

    _configure(monkeypatch, SelectingYoutubeDL, cookie=None)
    old_video = tmp_path / "BV1fixture.mp4"
    old_video.write_bytes(b"old-low-resolution")

    result = module.BilibiliDownloader().download_video(
        "https://www.bilibili.com/video/BV1fixture", output_dir=str(tmp_path),
    )

    assert Path(result) == tmp_path / f"BV1fixture.f{expected_format}.{expected_ext}"
    assert Path(result).read_bytes() == b"selected-video"
    assert old_video.read_bytes() == b"old-low-resolution"
    assert len(_FakeYoutubeDL.captured_opts) == 1
    assert "postprocessors" not in _FakeYoutubeDL.captured_opts[0]


def test_video_download_rejects_missing_selected_file(monkeypatch, tmp_path):
    class MissingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            return {"id": "BV1fixture", "format_id": "1080", "ext": "webm"}

    _configure(monkeypatch, MissingYoutubeDL, cookie=None)
    with pytest.raises(FileNotFoundError):
        module.BilibiliDownloader().download_video(
            "https://www.bilibili.com/video/BV1fixture", output_dir=str(tmp_path),
        )
