from pathlib import Path

from app.downloaders import youtube_downloader as module


class _FakeYoutubeDL:
    captured_opts = []

    def __init__(self, opts):
        self.opts = opts
        self.captured_opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, video_url, download):
        return {
            "id": "video123",
            "title": "Test video",
            "duration": 12,
            "thumbnail": "https://example.com/thumb.jpg",
            "ext": "m4a",
            "tags": ["test"],
        }


def test_youtube_downloader_passes_configured_cookiefile_to_yt_dlp(monkeypatch, tmp_path):
    class FakeCookieConfigManager:
        def get(self, platform):
            assert platform == "youtube"
            return "SID=abc; HSID=def"

    _FakeYoutubeDL.captured_opts = []
    monkeypatch.setattr(module, "CookieConfigManager", FakeCookieConfigManager)
    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    monkeypatch.setattr(module, "_apply_proxy", lambda opts: opts)
    monkeypatch.setattr(module.shutil, "which", lambda name: r"C:\Program Files\nodejs\node.exe")

    downloader = module.YoutubeDownloader()
    downloader.download(
        "https://www.youtube.com/watch?v=video123",
        output_dir=str(tmp_path),
        skip_download=True,
    )

    opts = _FakeYoutubeDL.captured_opts[0]
    assert "cookiefile" in opts
    cookiefile = Path(opts["cookiefile"])
    content = cookiefile.read_text(encoding="utf-8")
    assert ".youtube.com\tTRUE\t/\tFALSE\t0\tSID\tabc\n" in content
    assert ".youtube.com\tTRUE\t/\tFALSE\t0\tHSID\tdef\n" in content
    assert opts["js_runtimes"] == {"node": {"path": r"C:\Program Files\nodejs\node.exe"}}
    assert opts["remote_components"] == ["ejs:github"]


def test_youtube_downloader_omits_cookiefile_when_cookie_is_not_configured(monkeypatch, tmp_path):
    class FakeCookieConfigManager:
        def get(self, platform):
            assert platform == "youtube"
            return None

    _FakeYoutubeDL.captured_opts = []
    monkeypatch.setattr(module, "CookieConfigManager", FakeCookieConfigManager)
    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    monkeypatch.setattr(module, "_apply_proxy", lambda opts: opts)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)

    downloader = module.YoutubeDownloader()
    downloader.download(
        "https://www.youtube.com/watch?v=video123",
        output_dir=str(tmp_path),
        skip_download=True,
    )

    opts = _FakeYoutubeDL.captured_opts[0]
    assert "cookiefile" not in opts
