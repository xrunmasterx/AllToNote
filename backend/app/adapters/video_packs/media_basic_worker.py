from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "canonical_uri",
        "output_dir",
        "need_media",
        "need_subtitles",
        "cookie",
    }
)
_EXPECTED_VERSIONS = {"requests": "2.32.3", "yt-dlp": "2026.7.4"}
_BILIBILI_ID = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_MAXIMUM_REQUEST_BYTES = 64 * 1024
_MAXIMUM_RESULT_BYTES = 32 * 1024 * 1024
_MAXIMUM_COOKIE_BYTES = 16 * 1024
_MAXIMUM_SEGMENTS = 100_000
_MAXIMUM_TEXT_BYTES = 16 * 1024 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_SUBTITLE_HOSTS = ("bilibili.com", "bilivideo.com", "hdslb.com")


def _is_reparse_or_link(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_output_directory(value: object) -> Path:
    if type(value) is not str or not value:
        raise ValueError("worker request path is invalid")
    requested = Path(value).absolute()
    try:
        if any(
            _is_reparse_or_link(component)
            for component in (requested, *requested.parents)
            if component.exists() or component.is_symlink()
        ):
            raise ValueError("worker request path is invalid")
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("worker request path is invalid") from error
    if not resolved.is_dir():
        raise ValueError("worker request path is invalid")
    return resolved


def _canonical_bilibili(value: object) -> tuple[str, str, int]:
    if type(value) is not str:
        raise ValueError("worker request URI is invalid")
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        parts = [part for part in parsed.path.split("/") if part]
    except (UnicodeError, ValueError) as error:
        raise ValueError("worker request URI is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.bilibili.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or len(parts) != 2
        or parts[0] != "video"
        or _BILIBILI_ID.fullmatch(parts[1]) is None
        or frozenset(query) != frozenset({"p"})
        or len(query["p"]) != 1
        or not query["p"][0].isdigit()
        or int(query["p"][0]) < 1
    ):
        raise ValueError("worker request URI is invalid")
    page = int(query["p"][0])
    canonical = f"https://www.bilibili.com/video/{parts[1]}?p={page}"
    if value != canonical:
        raise ValueError("worker request URI is invalid")
    return canonical, parts[1], page


def _validated_cookie(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > _MAXIMUM_COOKIE_BYTES
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError("worker request cookie is invalid")
    return value


def _installed_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in _EXPECTED_VERSIONS
    }


def _default_http_get(url: str, **kwargs: object) -> object:
    import requests

    return requests.get(url, **kwargs)


def _default_youtube_dl_factory(options: dict[str, object]) -> object:
    from app.downloaders.bilibili_dm_patch import apply_bilibili_dm_img_patch
    import yt_dlp

    apply_bilibili_dm_img_patch()
    return yt_dlp.YoutubeDL(options)


def _response_json(response: object) -> object:
    json_method = getattr(response, "json", None)
    if not callable(json_method):
        raise ValueError("Bilibili response is invalid")
    return json_method()


def _metadata_from_view(
    payload: object,
    page: int,
) -> tuple[dict[str, object], int]:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError("Bilibili metadata is unavailable")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Bilibili metadata is unavailable")
    pages = data.get("pages")
    cid: object = data.get("cid")
    if isinstance(pages, Sequence) and not isinstance(
        pages, (str, bytes, bytearray)
    ):
        if page > len(pages) or not isinstance(pages[page - 1], dict):
            raise ValueError("Bilibili page is unavailable")
        cid = pages[page - 1].get("cid")
    if type(cid) is not int or cid < 1:
        raise ValueError("Bilibili metadata is unavailable")
    title = data.get("title")
    duration = data.get("duration")
    cover = data.get("pic")
    duration_ms = None
    if (
        not isinstance(duration, bool)
        and isinstance(duration, (int, float))
        and math.isfinite(float(duration))
        and duration >= 0
    ):
        duration_ms = int(float(duration) * 1000)
    return (
        {
            "title": title if type(title) is str and title.strip() else None,
            "duration_ms": duration_ms,
            "cover_uri": cover if type(cover) is str and cover else None,
        },
        cid,
    )


def _pick_subtitle(tracks: object) -> dict[str, object] | None:
    if not isinstance(tracks, Sequence) or isinstance(
        tracks, (str, bytes, bytearray)
    ):
        raise ValueError("Bilibili subtitle list is invalid")
    candidates = [track for track in tracks if type(track) is dict]
    if not candidates:
        return None

    def is_chinese(track: Mapping[str, object]) -> bool:
        language = track.get("lan")
        return (
            type(language) is str
            and (
                language.casefold().startswith("zh")
                or language.casefold() == "ai-zh"
            )
        )

    for track in candidates:
        if is_chinese(track) and not track.get("ai_type"):
            return track
    for track in candidates:
        if is_chinese(track):
            return track
    return candidates[0]


def _subtitle_segments(body: object) -> list[dict[str, object]]:
    if not isinstance(body, Sequence) or isinstance(
        body, (str, bytes, bytearray)
    ):
        raise ValueError("Bilibili subtitle body is invalid")
    result: list[dict[str, object]] = []
    total_text_bytes = 0
    previous_start = -1.0
    for item in body:
        if type(item) is not dict:
            continue
        start = item.get("from")
        end = item.get("to")
        text = item.get("content")
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(start) < previous_start
            or float(end) <= float(start)
            or type(text) is not str
            or not text.strip()
        ):
            continue
        if len(result) >= _MAXIMUM_SEGMENTS:
            raise ValueError("Bilibili subtitle segment limit exceeded")
        normalized = " ".join(text.split())
        total_text_bytes += len(normalized.encode("utf-8"))
        if total_text_bytes > _MAXIMUM_TEXT_BYTES:
            raise ValueError("Bilibili subtitle text limit exceeded")
        previous_start = float(start)
        result.append(
            {
                "start": float(start),
                "end": float(end),
                "text": normalized,
            }
        )
    return result


def _fetch_subtitle(
    *,
    bvid: str,
    page: int,
    cookie: str | None,
    http_get: Callable[..., object],
) -> tuple[dict[str, object], str, dict[str, object] | None]:
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.bilibili.com",
    }
    if cookie is not None:
        headers["Cookie"] = cookie
    try:
        view_response = http_get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid, "p": page},
            headers=headers,
            timeout=15,
        )
        metadata, cid = _metadata_from_view(_response_json(view_response), page)
        player_response = http_get(
            "https://api.bilibili.com/x/player/wbi/v2",
            params={"bvid": bvid, "cid": cid},
            headers=headers,
            timeout=15,
        )
        player = _response_json(player_response)
        if not isinstance(player, dict) or player.get("code") != 0:
            raise ValueError("Bilibili subtitle list is unavailable")
        data = player.get("data")
        subtitle = data.get("subtitle") if isinstance(data, dict) else None
        tracks = subtitle.get("subtitles") if isinstance(subtitle, dict) else None
        selected = _pick_subtitle(tracks)
        if selected is None:
            return metadata, "unavailable", None
        subtitle_url = selected.get("subtitle_url")
        language = selected.get("lan")
        if type(subtitle_url) is not str or not subtitle_url:
            return metadata, "unavailable", None
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        parsed = urlsplit(subtitle_url)
        host = (
            parsed.hostname.casefold().rstrip(".")
            if parsed.hostname is not None
            else ""
        )
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or not any(
                host == suffix or host.endswith(f".{suffix}")
                for suffix in _SUBTITLE_HOSTS
            )
        ):
            raise ValueError("Bilibili subtitle URL is invalid")
        body_response = http_get(
            subtitle_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Referer": "https://www.bilibili.com",
            },
            timeout=15,
            allow_redirects=False,
        )
        body_payload = _response_json(body_response)
        body = body_payload.get("body") if isinstance(body_payload, dict) else None
        segments = _subtitle_segments(body)
        if not segments:
            return metadata, "unavailable", None
        return (
            metadata,
            "available",
            {
                "language": (
                    language.strip()
                    if type(language) is str and language.strip()
                    else "und"
                ),
                "segments": segments,
            },
        )
    except Exception:
        return (
            {"title": None, "duration_ms": None, "cover_uri": None},
            "unknown",
            None,
        )


def _cookie_lines(cookie: str) -> list[str]:
    lines = ["# Netscape HTTP Cookie File\n"]
    for pair in cookie.replace("\n", ";").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key and not any(character.isspace() for character in key):
            lines.append(
                f".bilibili.com\tTRUE\t/\tFALSE\t0\t{key}\t{value}\n"
            )
    return lines


@contextmanager
def _temporary_cookie_file(
    output_dir: Path,
    cookie: str | None,
):
    if cookie is None:
        yield None
        return
    lines = _cookie_lines(cookie)
    if len(lines) == 1:
        yield None
        return
    with tempfile.TemporaryDirectory(
        prefix=".alltonote-cookie-",
        dir=output_dir,
    ) as temporary:
        path = Path(temporary) / "cookies.txt"
        path.write_text("".join(lines), encoding="utf-8", newline="\n")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        yield path


def _safe_media_output(
    info: Mapping[str, object],
    output_dir: Path,
    bvid: str,
) -> Path:
    candidates: list[Path] = []
    requested = info.get("requested_downloads")
    if isinstance(requested, Sequence) and not isinstance(
        requested, (str, bytes, bytearray)
    ):
        for item in requested:
            if isinstance(item, Mapping):
                value = item.get("filepath")
                if type(value) is str and value:
                    candidates.append(Path(value))
    candidates.append(output_dir / f"{bvid}.mp3")
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = output_dir / candidate
        try:
            if _is_reparse_or_link(candidate):
                raise ValueError("worker media output is invalid")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(output_dir)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    raise ValueError("worker media output is invalid")


def _download_media(
    *,
    canonical_uri: str,
    bvid: str,
    output_dir: Path,
    cookie: str | None,
    youtube_dl_factory: Callable[[dict[str, object]], object],
) -> tuple[Path, dict[str, object]]:
    options: dict[str, object] = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "http_headers": {"Referer": "https://www.bilibili.com"},
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    ffmpeg_directory = os.environ.get("ALLTONOTE_FFMPEG_DIR")
    if ffmpeg_directory:
        options["ffmpeg_location"] = ffmpeg_directory
    with _temporary_cookie_file(output_dir, cookie) as cookie_file:
        if cookie_file is not None:
            options["cookiefile"] = str(cookie_file)
        with youtube_dl_factory(options) as downloader:  # type: ignore[attr-defined]
            info = downloader.extract_info(canonical_uri, download=True)
    if type(info) is not dict:
        raise ValueError("worker media metadata is invalid")
    return _safe_media_output(info, output_dir, bvid), info


def _media_metadata(info: Mapping[str, object]) -> dict[str, object]:
    title = info.get("title")
    duration = info.get("duration")
    cover = info.get("thumbnail")
    duration_ms = None
    if (
        not isinstance(duration, bool)
        and isinstance(duration, (int, float))
        and math.isfinite(float(duration))
        and duration >= 0
    ):
        duration_ms = int(float(duration) * 1000)
    return {
        "title": title if type(title) is str and title.strip() else None,
        "duration_ms": duration_ms,
        "cover_uri": cover if type(cover) is str and cover else None,
    }


def acquire_request(
    request: Mapping[str, object],
    *,
    versions: Mapping[str, str] | None = None,
    http_get: Callable[..., object] = _default_http_get,
    youtube_dl_factory: Callable[
        [dict[str, object]], object
    ] = _default_youtube_dl_factory,
) -> dict[str, object]:
    if (
        type(request) is not dict
        or frozenset(request) != _REQUEST_KEYS
        or request.get("schema_version") != 1
        or type(request.get("need_media")) is not bool
        or type(request.get("need_subtitles")) is not bool
    ):
        raise ValueError("worker request is invalid")
    canonical_uri, bvid, page = _canonical_bilibili(
        request.get("canonical_uri")
    )
    output_dir = _safe_output_directory(request.get("output_dir"))
    cookie = _validated_cookie(request.get("cookie"))
    actual_versions = dict(versions or _installed_versions())
    if actual_versions != _EXPECTED_VERSIONS:
        raise ValueError("worker dependency identity is invalid")

    metadata: dict[str, object] = {
        "title": None,
        "duration_ms": None,
        "cover_uri": None,
    }
    subtitle_status = "not_supported"
    subtitle = None
    if request["need_subtitles"]:
        metadata, subtitle_status, subtitle = _fetch_subtitle(
            bvid=bvid,
            page=page,
            cookie=cookie,
            http_get=http_get,
        )

    media_path = None
    if request["need_media"]:
        media, info = _download_media(
            canonical_uri=canonical_uri,
            bvid=bvid,
            output_dir=output_dir,
            cookie=cookie,
            youtube_dl_factory=youtube_dl_factory,
        )
        media_path = media.relative_to(output_dir).as_posix()
        metadata = _media_metadata(info)

    return {
        "schema_version": 1,
        "identity": {
            "worker_protocol_version": 1,
            "downloader": "yt-dlp",
            "downloader_version": actual_versions["yt-dlp"],
            "http_client": "requests",
            "http_client_version": actual_versions["requests"],
        },
        **metadata,
        "media_path": media_path,
        "subtitle_status": subtitle_status,
        "subtitle": subtitle,
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(_MAXIMUM_REQUEST_BYTES + 1)
        if len(payload) > _MAXIMUM_REQUEST_BYTES:
            raise ValueError("worker request is too large")
        request = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non_finite_json")
            ),
        )
        result = acquire_request(request)
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAXIMUM_RESULT_BYTES:
            raise ValueError("worker result is too large")
    except Exception:
        return 1
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["acquire_request", "main"]
