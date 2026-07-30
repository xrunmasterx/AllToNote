import logging
from pathlib import Path

import pytest

from app.downloaders import bilibili_downloader as module


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

        if self.opts.get("merge_output_format") == "mp4":
            output_path = self.opts["outtmpl"].replace(
                "%(id)s.%(ext)s", "BV1fixture.mp4"
            )
            Path(output_path).touch()

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
        }


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
