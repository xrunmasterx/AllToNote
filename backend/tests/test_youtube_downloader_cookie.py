import logging
import json
from pathlib import Path

import pytest

from app.downloaders import youtube_downloader as module


class _FakeYoutubeDL:
    captured_opts = []
    cookie_contents = []

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
            output_path = self.opts["outtmpl"].replace("%(id)s.%(ext)s", "video123.mp4")
            Path(output_path).touch()

        return {
            "id": "video123",
            "title": "Test video",
            "duration": 12,
            "thumbnail": "https://example.com/thumb.jpg",
            "ext": "m4a",
            "tags": ["test"],
        }


class _FailingYoutubeDL(_FakeYoutubeDL):
    def extract_info(self, video_url, download):
        super().extract_info(video_url, download)
        raise RuntimeError("yt-dlp failed")


class _FailingTempFile:
    def __init__(self, wrapped, failure, failure_error=None):
        self._wrapped = wrapped
        self._failure = failure
        self._failure_error = failure_error
        self._close_failed = False
        self.name = wrapped.name

    def writelines(self, lines):
        if self._failure == "write":
            if self._failure_error is not None:
                raise self._failure_error
            raise OSError(f"write failed for {self.name}: SID=abc")
        self._wrapped.writelines(lines)

    def close(self):
        if self._failure == "close" and not self._close_failed:
            self._close_failed = True
            if self._failure_error is not None:
                raise self._failure_error
            raise OSError(f"close failed for {self.name}: SID=abc")
        self._wrapped.close()

    def force_cleanup(self):
        self._wrapped.close()
        Path(self.name).unlink(missing_ok=True)


def _configure_downloader(monkeypatch, youtube_dl, cookie="SID=abc; HSID=def"):
    class FakeCookieConfigManager:
        def get(self, platform):
            assert platform == "youtube"
            return cookie

    _FakeYoutubeDL.captured_opts = []
    _FakeYoutubeDL.cookie_contents = []
    monkeypatch.setattr(module, "CookieConfigManager", FakeCookieConfigManager)
    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", youtube_dl)
    monkeypatch.setattr(module, "_apply_proxy", lambda opts: opts)
    monkeypatch.setattr(module.shutil, "which", lambda name: r"C:\Program Files\nodejs\node.exe")


def _install_failing_tempfile(monkeypatch, failure, failure_error=None):
    original_named_temporary_file = module.tempfile.NamedTemporaryFile
    created = []

    def create_tempfile(*args, **kwargs):
        wrapped = _FailingTempFile(
            original_named_temporary_file(*args, **kwargs),
            failure,
            failure_error,
        )
        created.append(wrapped)
        return wrapped

    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", create_tempfile)
    return created


def _force_cleanup_tempfiles(tempfiles):
    for tempfile_wrapper in tempfiles:
        tempfile_wrapper.force_cleanup()


def test_download_removes_cookiefile_after_success(monkeypatch, tmp_path, caplog):
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    caplog.set_level(logging.INFO, logger=module.__name__)

    downloader = module.YoutubeDownloader()
    downloader.download(
        "https://www.youtube.com/watch?v=video123",
        output_dir=str(tmp_path),
        skip_download=True,
    )

    opts = _FakeYoutubeDL.captured_opts[0]
    cookiefile = Path(opts["cookiefile"])
    assert not cookiefile.exists()
    assert ".youtube.com\tTRUE\t/\tFALSE\t0\tSID\tabc\n" in _FakeYoutubeDL.cookie_contents[0]
    assert ".youtube.com\tTRUE\t/\tFALSE\t0\tHSID\tdef\n" in _FakeYoutubeDL.cookie_contents[0]
    assert opts["js_runtimes"] == {"node": {"path": r"C:\Program Files\nodejs\node.exe"}}
    assert "remote_components" not in opts

    log_output = "\n".join(
        record.getMessage() for record in caplog.records if record.name == module.__name__
    )
    assert str(cookiefile) not in log_output
    assert "SID=abc; HSID=def" not in log_output
    assert "abc" not in log_output
    assert "def" not in log_output


@pytest.mark.parametrize("method_name", ["download", "download_video"])
def test_download_removes_cookiefile_when_extract_info_raises(
    monkeypatch, tmp_path, method_name
):
    _configure_downloader(monkeypatch, _FailingYoutubeDL)
    downloader = module.YoutubeDownloader()

    method = getattr(downloader, method_name)
    kwargs = {"output_dir": str(tmp_path)}
    if method_name == "download":
        kwargs["skip_download"] = True
    with pytest.raises(RuntimeError, match="yt-dlp failed"):
        method("https://www.youtube.com/watch?v=video123", **kwargs)

    cookiefile = Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"])
    assert not cookiefile.exists()


def test_reused_downloader_creates_new_cookiefile_for_each_download_method(
    monkeypatch, tmp_path
):
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    downloader = module.YoutubeDownloader()

    downloader.download(
        "https://www.youtube.com/watch?v=video123",
        output_dir=str(tmp_path),
        skip_download=True,
    )
    downloader.download_video(
        "https://www.youtube.com/watch?v=video123",
        output_dir=str(tmp_path),
    )

    cookiefiles = [Path(opts["cookiefile"]) for opts in _FakeYoutubeDL.captured_opts]
    assert len(cookiefiles) == 2
    assert cookiefiles[0] != cookiefiles[1]
    assert all(not cookiefile.exists() for cookiefile in cookiefiles)


def test_youtube_downloader_does_not_create_cookiefile_without_cookie(monkeypatch, tmp_path):
    _configure_downloader(monkeypatch, _FakeYoutubeDL, cookie=None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("NamedTemporaryFile must not be called without a cookie")

    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", fail_if_called)
    downloader = module.YoutubeDownloader()
    downloader.download(
        "https://www.youtube.com/watch?v=video123",
        output_dir=str(tmp_path),
        skip_download=True,
    )

    assert "cookiefile" not in _FakeYoutubeDL.captured_opts[0]


def test_cookiefile_is_removed_when_youtube_dl_constructor_raises(monkeypatch, tmp_path):
    primary_error = RuntimeError("YoutubeDL constructor failed")
    captured_opts = []

    class ConstructorFailingYoutubeDL:
        def __init__(self, opts):
            captured_opts.append(opts)
            raise primary_error

    _configure_downloader(monkeypatch, ConstructorFailingYoutubeDL)
    downloader = module.YoutubeDownloader()

    with pytest.raises(RuntimeError) as caught:
        downloader.download(
            "https://www.youtube.com/watch?v=video123",
            output_dir=str(tmp_path),
            skip_download=True,
        )

    assert caught.value is primary_error
    assert not Path(captured_opts[0]["cookiefile"]).exists()


def test_cookiefile_is_removed_when_youtube_dl_enter_raises(monkeypatch, tmp_path):
    primary_error = RuntimeError("YoutubeDL enter failed")

    class EnterFailingYoutubeDL(_FakeYoutubeDL):
        def __enter__(self):
            raise primary_error

    _configure_downloader(monkeypatch, EnterFailingYoutubeDL)
    downloader = module.YoutubeDownloader()

    with pytest.raises(RuntimeError) as caught:
        downloader.download(
            "https://www.youtube.com/watch?v=video123",
            output_dir=str(tmp_path),
            skip_download=True,
        )

    assert caught.value is primary_error
    cookiefile = Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"])
    assert not cookiefile.exists()


@pytest.mark.parametrize("failure", ["write", "close"])
def test_cookiefile_creation_failure_removes_file_and_raises_safe_error(
    monkeypatch, tmp_path, failure
):
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    tempfiles = _install_failing_tempfile(monkeypatch, failure)
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(RuntimeError) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert str(caught.value) == "Failed to create YouTube cookie file"
        assert "SID=abc" not in str(caught.value)
        assert tempfiles
        assert not Path(tempfiles[0].name).exists()
    finally:
        _force_cleanup_tempfiles(tempfiles)


def test_cookiefile_logger_failure_removes_file_and_raises_safe_error(
    monkeypatch, tmp_path
):
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    tempfiles = _install_failing_tempfile(monkeypatch, failure=None)

    def fail_log(*args, **kwargs):
        raise OSError(f"log failed for {tempfiles[0].name}: SID=abc")

    monkeypatch.setattr(module.logger, "info", fail_log)
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(RuntimeError) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert str(caught.value) == "Failed to create YouTube cookie file"
        assert "SID=abc" not in str(caught.value)
        assert not Path(tempfiles[0].name).exists()
    finally:
        _force_cleanup_tempfiles(tempfiles)


def test_cookiefile_creation_and_cleanup_failure_raises_distinct_safe_error(
    monkeypatch, tmp_path
):
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    tempfiles = _install_failing_tempfile(monkeypatch, failure="write")

    def fail_remove(path):
        raise PermissionError(f"cannot remove {path}: SID=abc")

    monkeypatch.setattr(module.os, "remove", fail_remove)
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(RuntimeError) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert str(caught.value) == "Failed to create and clean up YouTube cookie file"
        assert tempfiles[0].name not in str(caught.value)
        assert "SID=abc" not in str(caught.value)
    finally:
        _force_cleanup_tempfiles(tempfiles)


def test_missing_cookiefile_during_cleanup_is_success(monkeypatch, tmp_path):
    class RemovingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            info = super().extract_info(video_url, download)
            Path(self.opts["cookiefile"]).unlink()
            return info

    _configure_downloader(monkeypatch, RemovingYoutubeDL)
    downloader = module.YoutubeDownloader()

    result = downloader.download(
        "https://www.youtube.com/watch?v=video123",
        output_dir=str(tmp_path),
        skip_download=True,
    )

    assert result.video_id == "video123"


def test_cleanup_failure_after_success_raises_safe_runtime_error(monkeypatch, tmp_path):
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    downloader = module.YoutubeDownloader()

    def fail_remove(path):
        raise PermissionError(f"cannot remove {path}: SID=abc")

    monkeypatch.setattr(module.os, "remove", fail_remove)

    try:
        with pytest.raises(RuntimeError) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        cookiefile = _FakeYoutubeDL.captured_opts[0]["cookiefile"]
        assert str(caught.value) == "Failed to clean up YouTube cookie file"
        assert cookiefile not in str(caught.value)
        assert "SID=abc" not in str(caught.value)
    finally:
        if _FakeYoutubeDL.captured_opts:
            Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"]).unlink(missing_ok=True)


def test_cleanup_failure_preserves_primary_error_with_safe_log_and_note(
    monkeypatch, tmp_path, caplog
):
    primary_error = RuntimeError("primary yt-dlp failure")

    class PrimaryFailingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            super().extract_info(video_url, download)
            raise primary_error

    _configure_downloader(monkeypatch, PrimaryFailingYoutubeDL)
    downloader = module.YoutubeDownloader()

    def fail_remove(path):
        raise PermissionError(f"cannot remove {path}: SID=abc")

    monkeypatch.setattr(module.os, "remove", fail_remove)
    caplog.set_level(logging.ERROR, logger=module.__name__)

    try:
        with pytest.raises(RuntimeError) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        cookiefile = _FakeYoutubeDL.captured_opts[0]["cookiefile"]
        log_output = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name == module.__name__
        )
        assert caught.value is primary_error
        assert caught.value.__notes__ == [
            "YouTube cookie file cleanup also failed"
        ]
        assert log_output == "Failed to clean up YouTube cookie file after download error"
        assert cookiefile not in log_output
        assert "SID=abc" not in log_output
    finally:
        if _FakeYoutubeDL.captured_opts:
            Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"]).unlink(missing_ok=True)


@pytest.mark.parametrize("failure_stage", ["write", "log"])
def test_cookiefile_creation_control_error_is_preserved_after_cleanup(
    monkeypatch, tmp_path, failure_stage
):
    control_error = KeyboardInterrupt(f"{failure_stage} interrupted: SID=abc")
    tempfile_failure = "write" if failure_stage == "write" else None
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    tempfiles = _install_failing_tempfile(
        monkeypatch,
        failure=tempfile_failure,
        failure_error=control_error,
    )
    if failure_stage == "log":
        monkeypatch.setattr(
            module.logger,
            "info",
            lambda *args, **kwargs: (_ for _ in ()).throw(control_error),
        )
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert caught.value is control_error
        assert not Path(tempfiles[0].name).exists()
    finally:
        _force_cleanup_tempfiles(tempfiles)


def test_creation_control_error_remains_primary_when_cleanup_fails(
    monkeypatch, tmp_path
):
    control_error = KeyboardInterrupt("write interrupted: SID=abc")
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    tempfiles = _install_failing_tempfile(
        monkeypatch,
        failure="write",
        failure_error=control_error,
    )

    def fail_remove(path):
        raise PermissionError(f"cannot remove {path}: HSID=def")

    monkeypatch.setattr(module.os, "remove", fail_remove)
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert caught.value is control_error
        assert caught.value.__notes__ == [
            "YouTube cookie file cleanup also failed"
        ]
    finally:
        _force_cleanup_tempfiles(tempfiles)


def test_cleanup_close_control_error_still_removes_file_and_is_propagated(
    monkeypatch, tmp_path
):
    cleanup_control_error = KeyboardInterrupt("cleanup close interrupted")
    original_named_temporary_file = module.tempfile.NamedTemporaryFile
    tempfiles = []

    class CleanupCloseFailingTempFile:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.name = wrapped.name

        def writelines(self, lines):
            raise OSError("write failed")

        def close(self):
            self._wrapped.close()
            raise cleanup_control_error

        def force_cleanup(self):
            self._wrapped.close()
            Path(self.name).unlink(missing_ok=True)

    def create_tempfile(*args, **kwargs):
        wrapped = CleanupCloseFailingTempFile(
            original_named_temporary_file(*args, **kwargs)
        )
        tempfiles.append(wrapped)
        return wrapped

    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", create_tempfile)
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert caught.value is cleanup_control_error
        assert not Path(tempfiles[0].name).exists()
    finally:
        _force_cleanup_tempfiles(tempfiles)


def test_cleanup_remove_control_error_is_propagated_after_close_attempt(
    monkeypatch, tmp_path
):
    cleanup_control_error = KeyboardInterrupt("cleanup remove interrupted")
    _configure_downloader(monkeypatch, _FakeYoutubeDL)
    tempfiles = _install_failing_tempfile(monkeypatch, failure="write")

    def fail_remove(path):
        raise cleanup_control_error

    monkeypatch.setattr(module.os, "remove", fail_remove)
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert caught.value is cleanup_control_error
        assert tempfiles[0]._wrapped.closed
    finally:
        _force_cleanup_tempfiles(tempfiles)


def test_cleanup_diagnostic_error_never_masks_primary_error(monkeypatch, tmp_path):
    primary_error = RuntimeError("primary yt-dlp failure")

    class PrimaryFailingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            super().extract_info(video_url, download)
            raise primary_error

    _configure_downloader(monkeypatch, PrimaryFailingYoutubeDL)

    def fail_remove(path):
        raise PermissionError(f"cannot remove {path}: SID=abc")

    def fail_error_log(*args, **kwargs):
        cookiefile = _FakeYoutubeDL.captured_opts[0]["cookiefile"]
        raise KeyboardInterrupt(f"logger failed for {cookiefile}: HSID=def")

    monkeypatch.setattr(module.os, "remove", fail_remove)
    monkeypatch.setattr(module.logger, "error", fail_error_log)
    downloader = module.YoutubeDownloader()

    try:
        caught = None
        try:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )
        except BaseException as error:
            caught = error

        assert caught is primary_error
        assert caught.__notes__ == ["YouTube cookie file cleanup also failed"]
    finally:
        if _FakeYoutubeDL.captured_opts:
            Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"]).unlink(missing_ok=True)


def test_add_note_failure_never_masks_primary_error(monkeypatch, tmp_path, caplog):
    class AddNoteFailingError(RuntimeError):
        def add_note(self, note):
            raise KeyboardInterrupt(f"note failed: {note}: SID=abc")

    primary_error = AddNoteFailingError("primary yt-dlp failure")

    class PrimaryFailingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            super().extract_info(video_url, download)
            raise primary_error

    _configure_downloader(monkeypatch, PrimaryFailingYoutubeDL)

    def fail_remove(path):
        raise PermissionError(f"cannot remove {path}: HSID=def")

    monkeypatch.setattr(module.os, "remove", fail_remove)
    caplog.set_level(logging.ERROR, logger=module.__name__)
    downloader = module.YoutubeDownloader()

    try:
        caught = None
        try:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )
        except BaseException as error:
            caught = error

        assert caught is primary_error
        assert [record.getMessage() for record in caplog.records] == [
            "Failed to clean up YouTube cookie file after download error"
        ]
    finally:
        if _FakeYoutubeDL.captured_opts:
            Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"]).unlink(missing_ok=True)


def test_cleanup_control_error_replaces_ordinary_primary_error(monkeypatch, tmp_path):
    primary_error = RuntimeError("primary yt-dlp failure")
    cleanup_control_error = KeyboardInterrupt("cleanup interrupted")
    error_logs = []

    class PrimaryFailingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            super().extract_info(video_url, download)
            raise primary_error

    _configure_downloader(monkeypatch, PrimaryFailingYoutubeDL)

    def fail_remove(path):
        raise cleanup_control_error

    monkeypatch.setattr(module.os, "remove", fail_remove)
    monkeypatch.setattr(module.logger, "error", lambda *args: error_logs.append(args))
    downloader = module.YoutubeDownloader()

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            downloader.download(
                "https://www.youtube.com/watch?v=video123",
                output_dir=str(tmp_path),
                skip_download=True,
            )

        assert caught.value is cleanup_control_error
        assert not hasattr(primary_error, "__notes__")
        assert error_logs == []
    finally:
        if _FakeYoutubeDL.captured_opts:
            Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"]).unlink(missing_ok=True)


def test_subtitle_fallback_uses_authenticated_ytdlp_json3(monkeypatch):
    class EmptyTranscriptFetcher:
        def fetch_subtitles(self, video_id, langs):
            assert video_id == "video123ABC"
            assert langs == ["zh", "en"]
            return None

    class Response:
        closed = False

        def read(self):
            return json.dumps(
                {
                    "events": [
                        {
                            "tStartMs": 1000,
                            "dDurationMs": 2500,
                            "segs": [{"utf8": "第一段\n字幕"}],
                        },
                        {
                            "tStartMs": 3500,
                            "dDurationMs": 1500,
                            "segs": [{"utf8": "第二段"}],
                        },
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")

        def close(self):
            self.closed = True

    response = Response()

    class SubtitleYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            assert download is False
            cookiefile = self.opts.get("cookiefile")
            self.cookie_contents.append(
                Path(cookiefile).read_text(encoding="utf-8")
            )
            return {
                "subtitles": {
                    "de": [
                        {"ext": "json3", "url": "https://caption/manual-de"}
                    ]
                },
                "automatic_captions": {
                    "zh": [
                        {"ext": "json3", "url": "https://caption/auto-zh"}
                    ]
                },
            }

        def urlopen(self, url):
            assert url == "https://caption/auto-zh"
            return response

    _configure_downloader(monkeypatch, SubtitleYoutubeDL)
    monkeypatch.setattr(
        module,
        "YouTubeSubtitleFetcher",
        EmptyTranscriptFetcher,
    )

    transcript = module.YoutubeDownloader().download_subtitles(
        "https://www.youtube.com/watch?v=video123ABC",
        langs=["zh", "en"],
    )

    assert transcript is not None
    assert transcript.language == "zh"
    assert transcript.full_text == "第一段 字幕 第二段"
    assert [(item.start, item.end, item.text) for item in transcript.segments] == [
        (1.0, 3.5, "第一段 字幕"),
        (3.5, 5.0, "第二段"),
    ]
    assert transcript.raw == {
        "source": "yt_dlp_json3",
        "language_code": "zh",
        "is_generated": True,
    }
    assert response.closed is True
    cookiefile = Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"])
    assert not cookiefile.exists()


def test_subtitle_fallback_propagates_cookie_cleanup_failure(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "events": [
                        {
                            "tStartMs": 0,
                            "dDurationMs": 1000,
                            "segs": [{"utf8": "caption"}],
                        }
                    ]
                }
            ).encode("utf-8")

        def close(self):
            return None

    class SubtitleYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, video_url, download):
            assert download is False
            return {
                "subtitles": {
                    "en": [
                        {"ext": "json3", "url": "https://caption/manual-en"}
                    ]
                }
            }

        def urlopen(self, url):
            assert url == "https://caption/manual-en"
            return Response()

    _configure_downloader(monkeypatch, SubtitleYoutubeDL)
    downloader = module.YoutubeDownloader()

    def fail_remove(path):
        raise PermissionError(f"cannot remove {path}: SID=abc")

    monkeypatch.setattr(module.os, "remove", fail_remove)

    try:
        with pytest.raises(RuntimeError) as caught:
            downloader._download_subtitles_with_ytdlp(
                "https://www.youtube.com/watch?v=video123ABC",
                ["en"],
            )

        cookiefile = _FakeYoutubeDL.captured_opts[0]["cookiefile"]
        assert str(caught.value) == "Failed to clean up YouTube cookie file"
        assert cookiefile not in str(caught.value)
        assert "SID=abc" not in str(caught.value)
    finally:
        if _FakeYoutubeDL.captured_opts:
            Path(_FakeYoutubeDL.captured_opts[0]["cookiefile"]).unlink(missing_ok=True)
