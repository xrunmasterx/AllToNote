import json
import logging
import math
import os
import shutil
import tempfile
from abc import ABC
from contextlib import contextmanager
from typing import Iterator, Union, Optional, List

import yt_dlp

from app.downloaders.base import Downloader, DownloadQuality
from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id

logger = logging.getLogger(__name__)

_COOKIE_CREATION_ERROR = "Failed to create YouTube cookie file"
_COOKIE_CREATION_CLEANUP_ERROR = "Failed to create and clean up YouTube cookie file"
_COOKIE_CLEANUP_ERROR = "Failed to clean up YouTube cookie file"
_COOKIE_CLEANUP_AFTER_ERROR_LOG = "Failed to clean up YouTube cookie file after download error"
_COOKIE_CLEANUP_NOTE = "YouTube cookie file cleanup also failed"


def _remove_cookie_file(cookiefile: str) -> None:
    try:
        os.remove(cookiefile)
    except FileNotFoundError:
        return
    except OSError:
        raise RuntimeError(_COOKIE_CLEANUP_ERROR) from None


def _add_safe_note(error: BaseException, note: str) -> None:
    try:
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            add_note(note)
    except BaseException:
        pass


def _apply_proxy(ydl_opts: dict) -> dict:
    """YouTube 在国内需要代理。配置了全局代理就塞进 yt-dlp opts。"""
    proxy = ProxyConfigManager().get_proxy_url()
    if proxy:
        ydl_opts['proxy'] = proxy
        logger.info(f"yt-dlp 走代理: {proxy}")
    return ydl_opts


def _apply_youtube_challenge_support(ydl_opts: dict) -> dict:
    node_path = shutil.which("node")
    if node_path:
        ydl_opts['js_runtimes'] = {'node': {'path': node_path}}
        ydl_opts['remote_components'] = ['ejs:github']
    return ydl_opts


class YoutubeDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()
        self._cookie_mgr = CookieConfigManager()
        self._cookie = self._cookie_mgr.get('youtube')

    def _write_netscape_cookie_file(self) -> Optional[str]:
        if not self._cookie:
            logger.warning("YouTube cookie is not configured; downloads may fail when YouTube requires sign-in")
            return None

        lines = ["# Netscape HTTP Cookie File\n"]
        for pair in self._cookie.replace("\n", "; ").split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            if key:
                lines.append(f".youtube.com\tTRUE\t/\tFALSE\t0\t{key}\t{value}\n")

        if len(lines) == 1:
            return None

        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', delete=False, encoding='utf-8'
            )
            tmp.writelines(lines)
            tmp.close()
            logger.info(
                "Created YouTube Netscape cookie file for yt-dlp (entries: %d)",
                len(lines) - 1,
            )
            return tmp.name
        except BaseException as creation_error:
            cleanup_errors = []
            if tmp is not None:
                try:
                    tmp.close()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                try:
                    _remove_cookie_file(tmp.name)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            if not isinstance(creation_error, Exception):
                if cleanup_errors:
                    _add_safe_note(creation_error, _COOKIE_CLEANUP_NOTE)
                raise

            cleanup_control_error = next(
                (
                    cleanup_error
                    for cleanup_error in cleanup_errors
                    if not isinstance(cleanup_error, Exception)
                ),
                None,
            )
            if cleanup_control_error is not None:
                if len(cleanup_errors) > 1:
                    _add_safe_note(cleanup_control_error, _COOKIE_CLEANUP_NOTE)
                raise cleanup_control_error from None

            message = (
                _COOKIE_CREATION_CLEANUP_ERROR
                if cleanup_errors
                else _COOKIE_CREATION_ERROR
            )
            raise RuntimeError(message) from None

    @contextmanager
    def _cookiefile_for_download(self) -> Iterator[Optional[str]]:
        cookiefile = self._write_netscape_cookie_file()
        try:
            yield cookiefile
        except BaseException as primary_error:
            if cookiefile:
                try:
                    _remove_cookie_file(cookiefile)
                except BaseException as cleanup_error:
                    if not isinstance(cleanup_error, Exception):
                        raise
                    _add_safe_note(primary_error, _COOKIE_CLEANUP_NOTE)
                    try:
                        logger.error(_COOKIE_CLEANUP_AFTER_ERROR_LOG)
                    except BaseException:
                        pass
            raise
        else:
            if cookiefile:
                _remove_cookie_file(cookiefile)

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
        }

        if skip_download:
            ydl_opts['skip_download'] = True

        with self._cookiefile_for_download() as cookiefile:
            if cookiefile:
                ydl_opts['cookiefile'] = cookiefile

            _apply_youtube_challenge_support(ydl_opts)
            _apply_proxy(ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=not skip_download)
                video_id = info.get("id")
                title = info.get("title")
                duration = info.get("duration", 0)
                cover_url = info.get("thumbnail")
                ext = info.get("ext", "m4a")
                audio_path = os.path.join(output_dir, f"{video_id}.{ext}")
        return AudioDownloadResult(
            file_path=audio_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="youtube",
            video_id=video_id,
            raw_info={'tags': info.get('tags')},
            video_path=None,
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
    ) -> str:
        """
        下载视频，返回视频文件路径
        """
        if output_dir is None:
            output_dir = get_data_dir()
        video_id = extract_video_id(video_url, "youtube")
        video_path = os.path.join(output_dir, f"{video_id}.mp4")
        if os.path.exists(video_path):
            return video_path
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
            'merge_output_format': 'mp4',  # 确保合并成 mp4
        }

        with self._cookiefile_for_download() as cookiefile:
            if cookiefile:
                ydl_opts['cookiefile'] = cookiefile

            _apply_youtube_challenge_support(ydl_opts)
            _apply_proxy(ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                video_id = info.get("id")
                video_path = os.path.join(output_dir, f"{video_id}.mp4")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频文件未找到: {video_path}")

        return video_path

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: List[str] = None) -> Optional[TranscriptResult]:
        """
        通过 YouTube InnerTube API 直接获取字幕（优先人工字幕，其次自动生成）。
        比 yt_dlp 方式更轻量，无需写临时文件到磁盘。

        :param video_url: 视频链接
        :param output_dir: 未使用（保留接口兼容）
        :param langs: 优先语言列表
        :return: TranscriptResult 或 None
        """
        if langs is None:
            langs = ['zh-Hans', 'zh', 'zh-CN', 'zh-TW', 'en', 'en-US', 'ja']

        video_id = extract_video_id(video_url, "youtube")
        fetcher = YouTubeSubtitleFetcher()
        print(
            f"尝试获取字幕，video_id={video_id}, langs={langs}"
        )
        transcript = fetcher.fetch_subtitles(video_id, langs)
        if transcript is not None:
            return transcript
        return self._download_subtitles_with_ytdlp(video_url, langs)

    def _download_subtitles_with_ytdlp(
        self,
        video_url: str,
        langs: List[str],
    ) -> Optional[TranscriptResult]:
        """Fallback to yt-dlp's authenticated caption metadata and JSON3 URL."""

        ydl_opts = {
            'skip_download': True,
            'quiet': True,
            'noplaylist': True,
        }
        with self._cookiefile_for_download() as cookiefile:
            try:
                if cookiefile:
                    ydl_opts['cookiefile'] = cookiefile
                _apply_youtube_challenge_support(ydl_opts)
                _apply_proxy(ydl_opts)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=False)
                    selected = self._select_json3_caption(info, langs)
                    if selected is None:
                        return None
                    language_code, is_generated, caption_url = selected
                    response = ydl.urlopen(caption_url)
                    try:
                        payload = response.read()
                    finally:
                        close = getattr(response, "close", None)
                        if callable(close):
                            close()
                return self._parse_json3_caption(
                    payload,
                    language_code=language_code,
                    is_generated=is_generated,
                )
            except Exception as error:
                logger.warning(
                    "YouTube yt-dlp subtitle fallback failed: %s",
                    type(error).__name__,
                )
                return None

    @staticmethod
    def _select_json3_caption(
        info: object,
        langs: List[str],
    ) -> Optional[tuple[str, bool, str]]:
        if not isinstance(info, dict):
            return None
        sources = (
            (info.get("subtitles"), False),
            (info.get("automatic_captions"), True),
        )
        for preferred_only in (True, False):
            for tracks, is_generated in sources:
                if not isinstance(tracks, dict):
                    continue
                languages = (
                    [language for language in langs if language in tracks]
                    if preferred_only
                    else [language for language in tracks if language not in langs]
                )
                for language in languages:
                    formats = tracks.get(language)
                    if not isinstance(formats, list):
                        continue
                    for item in formats:
                        if (
                            isinstance(item, dict)
                            and item.get("ext") == "json3"
                            and isinstance(item.get("url"), str)
                            and item["url"]
                        ):
                            return language, is_generated, item["url"]
        return None

    @staticmethod
    def _parse_json3_caption(
        payload: object,
        *,
        language_code: str,
        is_generated: bool,
    ) -> Optional[TranscriptResult]:
        try:
            document = json.loads(payload)
        except (TypeError, UnicodeError, ValueError, RecursionError):
            return None
        events = document.get("events") if isinstance(document, dict) else None
        if not isinstance(events, list):
            return None
        segments: list[TranscriptSegment] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            start_ms = event.get("tStartMs")
            duration_ms = event.get("dDurationMs")
            text_parts = event.get("segs")
            if (
                isinstance(start_ms, bool)
                or not isinstance(start_ms, (int, float))
                or isinstance(duration_ms, bool)
                or not isinstance(duration_ms, (int, float))
                or start_ms < 0
                or duration_ms <= 0
                or not isinstance(text_parts, list)
            ):
                continue
            text = "".join(
                item.get("utf8", "")
                for item in text_parts
                if isinstance(item, dict) and isinstance(item.get("utf8"), str)
            )
            text = " ".join(text.split())
            if not text:
                continue
            start = float(start_ms) / 1000
            end = float(start_ms + duration_ms) / 1000
            if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                continue
            segments.append(TranscriptSegment(start=start, end=end, text=text))
        if not segments:
            return None
        return TranscriptResult(
            language=language_code,
            full_text=" ".join(segment.text for segment in segments),
            segments=segments,
            raw={
                "source": "yt_dlp_json3",
                "language_code": language_code,
                "is_generated": is_generated,
            },
        )
