from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.video_packs.media_basic_worker import acquire_request


_VERSIONS = {"requests": "2.32.3", "yt-dlp": "2026.7.4"}
_URL = "https://www.bilibili.com/video/BV1vc411b7Wa?p=1"


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Http:
    def __init__(self, responses: list[object]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return _Response(self._responses.pop(0))


def _view() -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "title": "真实课程",
            "duration": 12.5,
            "pic": "https://i.example/cover.jpg",
            "pages": [{"cid": 987}],
        },
    }


def _request(output: Path, **overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "canonical_uri": _URL,
        "output_dir": str(output),
        "need_media": False,
        "need_subtitles": True,
        "cookie": None,
        **overrides,
    }


def test_subtitle_first_path_does_not_start_ytdlp(tmp_path: Path) -> None:
    http = _Http(
        [
            _view(),
            {
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "zh-CN",
                                "ai_type": 0,
                                "subtitle_url": "//aisubtitle.hdslb.com/track.json",
                            }
                        ]
                    }
                },
            },
            {
                "body": [
                    {"from": 0.0, "to": 1.2, "content": "第一段"},
                    {"from": 1.2, "to": 2.5, "content": "第二段"},
                ]
            },
        ]
    )

    result = acquire_request(
        _request(tmp_path, cookie="SESSDATA=test-cookie"),
        versions=_VERSIONS,
        http_get=http,
        youtube_dl_factory=lambda _opts: pytest.fail("yt-dlp was started"),
    )

    assert result["title"] == "真实课程"
    assert result["duration_ms"] == 12_500
    assert result["media_path"] is None
    assert result["subtitle_status"] == "available"
    assert result["subtitle"] == {
        "language": "zh-CN",
        "segments": [
            {"start": 0.0, "end": 1.2, "text": "第一段"},
            {"start": 1.2, "end": 2.5, "text": "第二段"},
        ],
    }
    assert "Cookie" in http.calls[0][1]["headers"]
    assert "Cookie" not in http.calls[2][1]["headers"]


def test_media_path_uses_private_cookie_file_and_removes_it(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _Ydl:
        def __init__(self, options: dict[str, object]) -> None:
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            assert url == _URL
            assert download is True
            options = captured["options"]
            cookie_file = Path(options["cookiefile"])
            captured["cookie_file"] = cookie_file
            captured["cookie_text"] = cookie_file.read_text(encoding="utf-8")
            audio = tmp_path / "BV1vc411b7Wa.mp3"
            audio.write_bytes(b"audio")
            return {
                "id": "BV1vc411b7Wa",
                "title": "Download title",
                "duration": 10,
                "thumbnail": "https://i.example/download.jpg",
            }

    secret = "SESSDATA=top-secret; bili_jct=csrf"
    result = acquire_request(
        _request(
            tmp_path,
            need_media=True,
            need_subtitles=False,
            cookie=secret,
        ),
        versions=_VERSIONS,
        youtube_dl_factory=_Ydl,
    )

    assert result["media_path"] == "BV1vc411b7Wa.mp3"
    assert result["subtitle_status"] == "not_supported"
    assert result["subtitle"] is None
    assert not captured["cookie_file"].exists()
    assert "SESSDATA\ttop-secret" in captured["cookie_text"]
    assert secret not in repr(result)
    assert "top-secret" not in repr(result)


def test_successful_empty_subtitle_list_is_unavailable(tmp_path: Path) -> None:
    result = acquire_request(
        _request(tmp_path),
        versions=_VERSIONS,
        http_get=_Http(
            [
                _view(),
                {"code": 0, "data": {"subtitle": {"subtitles": []}}},
            ]
        ),
        youtube_dl_factory=lambda _opts: pytest.fail("yt-dlp was started"),
    )

    assert result["subtitle_status"] == "unavailable"
    assert result["subtitle"] is None


def test_transient_subtitle_failure_remains_unknown(tmp_path: Path) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise TimeoutError

    result = acquire_request(
        _request(tmp_path),
        versions=_VERSIONS,
        http_get=fail,
        youtube_dl_factory=lambda _opts: pytest.fail("yt-dlp was started"),
    )

    assert result["subtitle_status"] == "unknown"
    assert result["subtitle"] is None


def test_untrusted_subtitle_host_is_not_requested(tmp_path: Path) -> None:
    http = _Http(
        [
            _view(),
            {
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "zh",
                                "subtitle_url": "https://127.0.0.1/private",
                            }
                        ]
                    }
                },
            },
        ]
    )

    result = acquire_request(
        _request(tmp_path),
        versions=_VERSIONS,
        http_get=http,
        youtube_dl_factory=lambda _opts: pytest.fail("yt-dlp was started"),
    )

    assert result["subtitle_status"] == "unknown"
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    "request_update",
    (
        {"canonical_uri": "https://evil.example/video/BV1vc411b7Wa?p=1"},
        {"canonical_uri": "https://www.bilibili.com/video/not-a-bv?p=1"},
        {"cookie": "SESSDATA=secret\nInjected=true"},
        {"need_media": 1},
    ),
)
def test_request_contract_rejects_unsafe_values(
    tmp_path: Path,
    request_update: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="worker request"):
        acquire_request(
            _request(tmp_path, **request_update),
            versions=_VERSIONS,
            youtube_dl_factory=lambda _opts: pytest.fail("yt-dlp was started"),
        )


def test_worker_rejects_media_path_outside_output(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.mp3"
    outside.write_bytes(b"outside")

    class _Ydl:
        def __init__(self, _options: dict[str, object]) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
            assert download
            return {
                "id": outside.stem,
                "requested_downloads": [{"filepath": str(outside)}],
            }

    with pytest.raises(ValueError, match="media output"):
        acquire_request(
            _request(tmp_path, need_media=True, need_subtitles=False),
            versions=_VERSIONS,
            youtube_dl_factory=_Ydl,
        )
