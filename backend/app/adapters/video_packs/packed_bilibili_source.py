from __future__ import annotations

import math
import stat
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

from app.adapters.sources.legacy_video import LegacyVideoSourceAdapter
from app.adapters.video_packs.official_pack_process import (
    minimal_worker_environment,
    run_json_worker,
)
from app.adapters.video_packs.official_video_pack import MEDIA_BASIC
from app.adapters.video_packs.official_video_pack_resolver import (
    ResolvedOfficialVideoPack,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.source import (
    AcquiredVideoSource,
    CancellationTokenPort,
    ResolvedVideoSource,
    SubtitleAvailability,
)


_EXPECTED_IDENTITY = {
    "worker_protocol_version": 1,
    "downloader": "yt-dlp",
    "downloader_version": "2026.7.4",
    "http_client": "requests",
    "http_client_version": "2.32.3",
}
_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "identity",
        "title",
        "duration_ms",
        "cover_uri",
        "media_path",
        "subtitle_status",
        "subtitle",
    }
)
_SUBTITLE_KEYS = frozenset({"language", "segments"})
_SEGMENT_KEYS = frozenset({"start", "end", "text"})
_MAXIMUM_OUTPUT_BYTES = 32 * 1024 * 1024
_MAXIMUM_PROBE_OUTPUT_BYTES = 64 * 1024

WorkerRunner = Callable[..., dict[str, object]]
CookieResolver = Callable[[], str | None]


def _source_error(
    code: str,
    category: ErrorCategory,
    message: str,
) -> DomainError:
    return DomainError(
        code,
        category,
        message,
        {"connector_id": "bilibili"},
    )


def _result_invalid() -> DomainError:
    return _source_error(
        "source_result_invalid",
        ErrorCategory.RECIPE_FAILED,
        "The media-basic Pack returned an invalid acquisition result",
    )


def _path_chain_is_unsafe(path: Path) -> bool:
    try:
        return any(
            component.is_symlink()
            or bool(
                getattr(component.lstat(), "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            for component in (path.absolute(), *path.absolute().parents)
            if component.exists() or component.is_symlink()
        )
    except OSError:
        return True


class PackedBilibiliVideoSourceAdapter:
    def __init__(
        self,
        legacy: LegacyVideoSourceAdapter,
        pack: ResolvedOfficialVideoPack,
        *,
        cookie_resolver: CookieResolver,
        timeout_seconds: int = 1800,
        runner: WorkerRunner = run_json_worker,
        probe_runner: WorkerRunner = run_json_worker,
        backend_root: Path | None = None,
    ) -> None:
        if (
            not isinstance(legacy, LegacyVideoSourceAdapter)
            or not isinstance(pack, ResolvedOfficialVideoPack)
            or pack.pack_id != MEDIA_BASIC.pack_id
            or pack.pack_version != MEDIA_BASIC.pack_version
            or frozenset(pack.entrypoints)
            != frozenset({"python", "ffmpeg", "ffprobe"})
            or not callable(cookie_resolver)
            or not callable(probe_runner)
            or type(timeout_seconds) is not int
            or timeout_seconds < 1
        ):
            raise ValueError("media-basic Pack binding is invalid")
        root = (
            Path(__file__).resolve().parents[3]
            if backend_root is None
            else Path(backend_root).resolve(strict=True)
        )
        if not root.is_dir():
            raise ValueError("Runtime package root is invalid")
        self._legacy = legacy
        self._pack = pack
        self._cookie_resolver = cookie_resolver
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        self._probe_runner = probe_runner
        self._backend_root = root

    def resolve(self, input_value: str) -> ResolvedVideoSource:
        source = self._legacy.resolve(input_value)
        if source.connector_id not in {"bilibili", "local"}:
            raise DomainError(
                "source_unsupported",
                ErrorCategory.INVALID_REQUEST,
                "The media-basic Pack supports Bilibili and local video sources",
            )
        return source

    def acquire(
        self,
        source: ResolvedVideoSource,
        *,
        need_media: bool,
        need_subtitles: bool = True,
        output_dir: Path,
        token: CancellationTokenPort,
    ) -> AcquiredVideoSource:
        if source.connector_id == "local":
            acquired = self._legacy.acquire(
                source,
                need_media=need_media,
                need_subtitles=need_subtitles,
                output_dir=output_dir,
                token=token,
            )
            if acquired.duration_ms is not None:
                return acquired
            binding = source.local_binding
            if binding is None:
                raise DomainError(
                    "source_contract_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "The resolved source contract is invalid",
                )
            duration_ms = self._probe_local_duration(
                acquired.media_path or binding.path,
                token,
            )
            return AcquiredVideoSource(
                source=acquired.source,
                title=acquired.title,
                duration_ms=duration_ms,
                cover_uri=acquired.cover_uri,
                media_path=acquired.media_path,
                video_path=acquired.video_path,
                subtitle_availability=acquired.subtitle_availability,
                opaque_subtitle=acquired.opaque_subtitle,
            )
        if source.connector_id != "bilibili" or source.canonical_uri is None:
            raise DomainError(
                "source_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "The resolved source contract is invalid",
            )
        try:
            expected = self._legacy.resolve(source.canonical_uri)
        except DomainError:
            raise DomainError(
                "source_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "The resolved source contract is invalid",
            ) from None
        if source != expected:
            raise DomainError(
                "source_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "The resolved source contract is invalid",
            )
        output = Path(output_dir).absolute()
        if _path_chain_is_unsafe(output):
            raise _source_error(
                "source_output_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The source acquisition directory is unsafe",
            )
        try:
            output.mkdir(parents=True, exist_ok=True)
            output = output.resolve(strict=True)
        except OSError as error:
            raise _source_error(
                "source_output_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The source acquisition directory is unavailable",
            ) from error
        if _path_chain_is_unsafe(output):
            raise _source_error(
                "source_output_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The source acquisition directory is unsafe",
            )

        token.raise_if_cancelled()
        cookie = self._cookie_resolver()
        if cookie is not None and type(cookie) is not str:
            raise _source_error(
                "source_authentication_invalid",
                ErrorCategory.POLICY_DENIED,
                "The configured Bilibili authentication is invalid",
            )
        result = self._runner(
            (
                str(self._pack.entrypoints["python"]),
                "-B",
                "-m",
                "app.adapters.video_packs.media_basic_worker",
            ),
            {
                "schema_version": 1,
                "canonical_uri": source.canonical_uri,
                "output_dir": str(output),
                "need_media": need_media,
                "need_subtitles": need_subtitles,
                "cookie": cookie,
            },
            cwd=output,
            environment=minimal_worker_environment(
                overrides={
                    "PYTHONPATH": str(self._backend_root),
                    "PATH": str(self._pack.entrypoints["ffmpeg"].parent),
                    "ALLTONOTE_FFMPEG_DIR": str(
                        self._pack.entrypoints["ffmpeg"].parent
                    ),
                }
            ),
            timeout_seconds=self._timeout_seconds,
            maximum_output_bytes=_MAXIMUM_OUTPUT_BYTES,
            check_cancelled=token.raise_if_cancelled,
        )
        token.raise_if_cancelled()
        return self._decode_result(
            result,
            source=source,
            output_dir=output,
            need_media=need_media,
            need_subtitles=need_subtitles,
        )

    def _probe_local_duration(
        self,
        media_path: Path,
        token: CancellationTokenPort,
    ) -> int:
        token.raise_if_cancelled()
        result = self._probe_runner(
            (
                str(self._pack.entrypoints["ffprobe"]),
                "-hide_banner",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(media_path),
            ),
            {},
            cwd=media_path.parent,
            environment=minimal_worker_environment(
                overrides={
                    "PATH": str(self._pack.entrypoints["ffprobe"].parent),
                }
            ),
            timeout_seconds=self._timeout_seconds,
            maximum_output_bytes=_MAXIMUM_PROBE_OUTPUT_BYTES,
            check_cancelled=token.raise_if_cancelled,
        )
        token.raise_if_cancelled()
        if type(result) is not dict or frozenset(result) != {"format"}:
            raise _result_invalid()
        format_result = result.get("format")
        if (
            type(format_result) is not dict
            or frozenset(format_result) != {"duration"}
            or type(format_result.get("duration")) is not str
        ):
            raise _result_invalid()
        try:
            duration = Decimal(format_result["duration"])
        except (InvalidOperation, ValueError):
            raise _result_invalid() from None
        if not duration.is_finite():
            raise _result_invalid()
        duration_ms = int(duration * 1000)
        if duration_ms < 1:
            raise _result_invalid()
        return duration_ms

    @staticmethod
    def _decode_result(
        result: Mapping[str, object],
        *,
        source: ResolvedVideoSource,
        output_dir: Path,
        need_media: bool,
        need_subtitles: bool,
    ) -> AcquiredVideoSource:
        if (
            type(result) is not dict
            or frozenset(result) != _RESULT_KEYS
            or result.get("schema_version") != 1
            or result.get("identity") != _EXPECTED_IDENTITY
        ):
            raise _result_invalid()
        title = result.get("title")
        duration_ms = result.get("duration_ms")
        cover_uri = result.get("cover_uri")
        if (
            title is not None
            and (type(title) is not str or not title.strip())
        ) or (
            duration_ms is not None
            and (
                type(duration_ms) is not int
                or duration_ms < 0
            )
        ) or (
            cover_uri is not None
            and (type(cover_uri) is not str or not cover_uri)
        ):
            raise _result_invalid()

        media_path = PackedBilibiliVideoSourceAdapter._decode_media(
            result.get("media_path"),
            output_dir,
            required=need_media,
        )
        subtitle_status = result.get("subtitle_status")
        expected_statuses = (
            {"available", "unavailable", "unknown"}
            if need_subtitles
            else {"not_supported"}
        )
        if subtitle_status not in expected_statuses:
            raise _result_invalid()
        subtitle = PackedBilibiliVideoSourceAdapter._decode_subtitle(
            result.get("subtitle"),
            required=subtitle_status == "available",
        )
        availability = {
            "available": SubtitleAvailability.AVAILABLE,
            "unavailable": SubtitleAvailability.UNAVAILABLE,
            "unknown": SubtitleAvailability.UNKNOWN,
            "not_supported": SubtitleAvailability.NOT_SUPPORTED,
        }[subtitle_status]
        return AcquiredVideoSource(
            source=source,
            title=title,
            duration_ms=duration_ms,
            cover_uri=cover_uri,
            media_path=media_path,
            video_path=None,
            subtitle_availability=availability,
            opaque_subtitle=subtitle,
        )

    @staticmethod
    def _decode_media(
        value: object,
        output_dir: Path,
        *,
        required: bool,
    ) -> Path | None:
        if value is None and not required:
            return None
        if (
            type(value) is not str
            or not value
            or Path(value).is_absolute()
            or "\\" in value
        ):
            raise _result_invalid()
        candidate = output_dir.joinpath(*value.split("/"))
        try:
            if _path_chain_is_unsafe(candidate):
                raise OSError("unsafe")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(output_dir)
        except (OSError, RuntimeError, ValueError):
            raise _result_invalid() from None
        if not resolved.is_file():
            raise _result_invalid()
        return resolved

    @staticmethod
    def _decode_subtitle(
        value: object,
        *,
        required: bool,
    ) -> object | None:
        if value is None and not required:
            return None
        if type(value) is not dict or frozenset(value) != _SUBTITLE_KEYS:
            raise _result_invalid()
        language = value.get("language")
        segments = value.get("segments")
        if (
            type(language) is not str
            or not language.strip()
            or not isinstance(segments, Sequence)
            or isinstance(segments, (str, bytes, bytearray))
            or not segments
        ):
            raise _result_invalid()
        normalized: list[dict[str, object]] = []
        previous_start = -1.0
        for segment in segments:
            if type(segment) is not dict or frozenset(segment) != _SEGMENT_KEYS:
                raise _result_invalid()
            start = segment.get("start")
            end = segment.get("end")
            text = segment.get("text")
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
                raise _result_invalid()
            previous_start = float(start)
            normalized.append(
                {
                    "start": float(start),
                    "end": float(end),
                    "text": text.strip(),
                }
            )
        return SimpleNamespace(language=language.strip(), segments=normalized)


__all__ = ["PackedBilibiliVideoSourceAdapter"]
