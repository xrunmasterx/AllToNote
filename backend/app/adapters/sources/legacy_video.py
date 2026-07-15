from __future__ import annotations

import hashlib
import importlib
import ipaddress
import os
import re
import socket
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import SplitResult, parse_qs, urljoin, urlsplit, urlunsplit

from app.core.errors import DomainError, ErrorCategory
from app.core.ports.jobs import SourceIdentityBinding
from app.core.ports.source import (
    AcquiredVideoSource,
    CancellationTokenPort,
    LocalMachineBinding,
    MaterializationPolicy,
    ResolvedVideoSource,
    SubtitleAvailability,
)


_BILIBILI_ID = re.compile(r"BV[0-9A-Za-z]+\Z")
_YOUTUBE_ID = re.compile(r"[0-9A-Za-z_-]{11}\Z")
_DOUYIN_ID = re.compile(r"[0-9]{10,}\Z")
_KUAISHOU_ID = re.compile(r"[0-9A-Za-z_-]+\Z")
_CONNECTOR_VERSION = "1.0.0"


class LegacyNoSubtitleError(Exception):
    """A typed legacy outcome proving that the source has no subtitles."""


class LegacyTransientError(Exception):
    pass


class LegacyAuthenticationError(Exception):
    pass


class LegacyUnsupportedSourceError(Exception):
    pass


@dataclass(frozen=True)
class _ConnectorSpec:
    module: str
    class_name: str
    supports_subtitles: bool
    supports_metadata_only: bool


_DEFAULT_SPECS = {
    "bilibili": _ConnectorSpec(
        "app.downloaders.bilibili_downloader",
        "BilibiliDownloader",
        True,
        False,
    ),
    "douyin": _ConnectorSpec(
        "app.downloaders.douyin_downloader",
        "DouyinDownloader",
        False,
        False,
    ),
    "kuaishou": _ConnectorSpec(
        "app.downloaders.kuaishou_downloader",
        "KuaiShouDownloader",
        False,
        False,
    ),
    "local": _ConnectorSpec(
        "app.downloaders.local_downloader",
        "LocalDownloader",
        False,
        False,
    ),
    "youtube": _ConnectorSpec(
        "app.downloaders.youtube_downloader",
        "YoutubeDownloader",
        True,
        True,
    ),
}


def default_connector_ids() -> tuple[str, ...]:
    return tuple(sorted(_DEFAULT_SPECS))


def _lazy_factory(spec: _ConnectorSpec) -> Callable[[], object]:
    def create() -> object:
        module = importlib.import_module(spec.module)
        return getattr(module, spec.class_name)()

    return create


def _source_error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    connector_id: str | None = None,
) -> DomainError:
    details = {} if connector_id is None else {"connector_id": connector_id}
    return DomainError(code, category, message, details)


def _unsupported() -> DomainError:
    return _source_error(
        "source_unsupported",
        ErrorCategory.INVALID_REQUEST,
        "The input is not a supported video source",
    )


def _contract_invalid() -> DomainError:
    return _source_error(
        "source_contract_invalid",
        ErrorCategory.INVALID_REQUEST,
        "The resolved source contract is invalid",
    )


def _local_changed() -> DomainError:
    return _source_error(
        "source_local_changed",
        ErrorCategory.CONFLICT,
        "The local source changed after it was resolved",
        connector_id="local",
    )


def _map_legacy_error(error: BaseException, connector_id: str) -> DomainError:
    if isinstance(error, (LegacyTransientError, TimeoutError, ConnectionError)):
        return _source_error(
            "source_acquire_transient",
            ErrorCategory.RETRYABLE_RUNTIME,
            "The video source could not be acquired temporarily",
            connector_id=connector_id,
        )
    if isinstance(error, (LegacyAuthenticationError, PermissionError)):
        return _source_error(
            "source_authentication_required",
            ErrorCategory.POLICY_DENIED,
            "The video source requires authentication",
            connector_id=connector_id,
        )
    if isinstance(error, (LegacyUnsupportedSourceError, FileNotFoundError)):
        return _source_error(
            "source_unsupported",
            ErrorCategory.INVALID_REQUEST,
            "The video source is unavailable or unsupported",
            connector_id=connector_id,
        )
    return _source_error(
        "source_acquire_failed",
        ErrorCategory.INTERNAL,
        "The legacy video source adapter failed",
        connector_id=connector_id,
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except AttributeError:
        return path.is_symlink()
    return bool(attributes & 0x400)


def _path_chain_has_reparse_point(path: Path) -> bool:
    absolute = path.absolute()
    try:
        return any(
            _is_reparse_point(component)
            for component in (absolute, *absolute.parents)
            if component.exists() or component.is_symlink()
        )
    except OSError:
        return True


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise _source_error(
            "source_local_unreadable",
            ErrorCategory.INVALID_REQUEST,
            "The local video source is not readable",
        ) from None
    return f"sha256:{digest.hexdigest()}"


def _path_binding_id(machine_id: str, path: Path) -> str:
    normalized = str(path).casefold() if path.drive else str(path)
    value = hashlib.sha256(f"{machine_id}\0{normalized}".encode("utf-8")).hexdigest()
    return f"local_binding_{value}"


def _host(parsed: SplitResult) -> str:
    value = parsed.hostname
    return value.casefold().rstrip(".") if isinstance(value, str) else ""


def _host_is(host: str, base: str) -> bool:
    return host == base or host.endswith(f".{base}")


def _strict_http_url(input_value: str) -> SplitResult:
    try:
        parsed = urlsplit(input_value)
    except (UnicodeError, ValueError):
        raise _unsupported() from None
    scheme = parsed.scheme.casefold()
    try:
        port = parsed.port
    except ValueError:
        raise _unsupported() from None
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != (443 if scheme == "https" else 80))
    ):
        raise _unsupported()
    return parsed


class _RedirectResponsePort(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def close(self) -> None: ...


def _requests_redirect_transport(input_value: str) -> _RedirectResponsePort:
    requests = importlib.import_module("requests")
    return requests.head(input_value, allow_redirects=False, timeout=15)


def _default_address_resolver(host: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                address[4][0]
                for address in socket.getaddrinfo(
                    host,
                    None,
                    type=socket.SOCK_STREAM,
                )
            }
        )
    )


def _require_public_host(
    host: str,
    address_resolver: Callable[[str], tuple[str, ...]],
) -> None:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = address_resolver(host)
        except OSError:
            raise _unsupported() from None
        if not addresses:
            raise _unsupported()
        try:
            parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
        except ValueError:
            raise _unsupported() from None
    else:
        parsed_addresses = (literal,)
    if any(not address.is_global for address in parsed_addresses):
        raise _unsupported()


class LegacyVideoSourceAdapter:
    def __init__(
        self,
        *,
        local_machine_id: str,
        factories: Mapping[str, Callable[[], object]] | None = None,
        redirect_resolver: Callable[[str], str] | None = None,
        redirect_transport: Callable[[str], _RedirectResponsePort] | None = None,
        address_resolver: Callable[[str], tuple[str, ...]] | None = None,
    ) -> None:
        if not isinstance(local_machine_id, str) or not local_machine_id:
            raise ValueError("local_machine_id must not be empty")
        self._local_machine_id = local_machine_id
        self._factories = {
            connector_id: _lazy_factory(spec)
            for connector_id, spec in _DEFAULT_SPECS.items()
        }
        self._factories.update(dict(factories or {}))
        self._redirect_resolver = redirect_resolver
        self._redirect_transport = redirect_transport or _requests_redirect_transport
        self._address_resolver = address_resolver or _default_address_resolver

    def resolve(self, input_value: str) -> ResolvedVideoSource:
        if not isinstance(input_value, str) or not input_value.strip():
            raise _unsupported()
        value = input_value.strip()
        path = Path(value).expanduser()
        if path.exists():
            return self._resolve_local(path)

        parsed = _strict_http_url(value)
        host = _host(parsed)
        if _host_is(host, "tiktok.com") or _host_is(host, "xiaoyuzhoufm.com"):
            raise _unsupported()
        if host == "b23.tv" or host == "v.douyin.com" or host == "v.kuaishou.com":
            connector_id = {
                "b23.tv": "bilibili",
                "v.douyin.com": "douyin",
                "v.kuaishou.com": "kuaishou",
            }[host]
            parsed = self._resolve_redirect(value, connector_id)
            host = _host(parsed)

        if _host_is(host, "bilibili.com"):
            return self._resolve_bilibili(parsed)
        if _host_is(host, "youtube.com") or host == "youtu.be":
            return self._resolve_youtube(parsed)
        if _host_is(host, "douyin.com"):
            return self._resolve_douyin(parsed)
        if _host_is(host, "kuaishou.com"):
            return self._resolve_kuaishou(parsed)
        raise _unsupported()

    def acquire(
        self,
        source: ResolvedVideoSource,
        *,
        need_media: bool,
        need_subtitles: bool = True,
        output_dir: Path,
        token: CancellationTokenPort,
    ) -> AcquiredVideoSource:
        if not isinstance(source, ResolvedVideoSource):
            raise _source_error(
                "source_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "The resolved source contract is invalid",
            )
        token.raise_if_cancelled()
        self._validate_resolved_contract(
            source,
            verify_local_content=not need_media,
        )
        output = Path(output_dir)
        if _path_chain_has_reparse_point(output):
            raise _source_error(
                "source_output_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The source acquisition directory is unsafe",
            )
        output.mkdir(parents=True, exist_ok=True)
        if _path_chain_has_reparse_point(output):
            raise _source_error(
                "source_output_path_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The source acquisition directory is unsafe",
            )
        output = output.resolve()

        if source.connector_id == "local":
            return self._acquire_local(
                source,
                need_media=need_media,
                output_dir=output,
                token=token,
            )

        spec = _DEFAULT_SPECS[source.connector_id]
        if not need_media and not (
            (need_subtitles and spec.supports_subtitles)
            or spec.supports_metadata_only
        ):
            token.raise_if_cancelled()
            return AcquiredVideoSource(
                source=source,
                title=None,
                duration_ms=None,
                cover_uri=None,
                media_path=None,
                video_path=None,
                subtitle_availability=SubtitleAvailability.NOT_SUPPORTED,
            )

        downloader = self._create_downloader(source.connector_id)
        legacy_result: object | None = None
        if need_media or spec.supports_metadata_only:
            kwargs: dict[str, object] = {
                "output_dir": str(output),
                "quality": "fast",
                "need_video": bool(need_media),
            }
            if spec.supports_metadata_only:
                kwargs["skip_download"] = not need_media
            try:
                legacy_result = downloader.download(source.canonical_uri, **kwargs)
            except Exception as error:
                raise _map_legacy_error(error, source.connector_id) from None
            token.raise_if_cancelled()

        subtitle, subtitle_availability = (
            self._acquire_subtitle(
                downloader,
                source,
                output,
                token,
            )
            if need_subtitles
            else (None, SubtitleAvailability.NOT_SUPPORTED)
        )
        return self._build_acquired(
            source,
            legacy_result,
            output_dir=output,
            need_media=need_media,
            subtitle=subtitle,
            subtitle_availability=subtitle_availability,
        )

    def _validate_resolved_contract(
        self,
        source: ResolvedVideoSource,
        *,
        verify_local_content: bool,
    ) -> None:
        if source.connector_id == "local":
            self._validate_local_contract(
                source,
                verify_content=verify_local_content,
            )
            return
        if (
            source.connector_id not in _DEFAULT_SPECS
            or source.canonical_uri is None
            or source.local_binding is not None
            or source.content_sha256 is not None
            or source.logical_reference is not None
        ):
            raise _contract_invalid()
        try:
            expected = self._resolve_direct_remote(
                _strict_http_url(source.canonical_uri)
            )
        except DomainError:
            raise _contract_invalid() from None
        if source != expected:
            raise _contract_invalid()

    def _validate_local_contract(
        self,
        source: ResolvedVideoSource,
        *,
        verify_content: bool,
    ) -> Path:
        binding = source.local_binding
        digest = source.content_sha256
        if (
            binding is None
            or digest is None
            or binding.machine_id != self._local_machine_id
            or binding.content_sha256 != digest
            or binding.binding_id != _path_binding_id(binding.machine_id, binding.path)
            or source.connector_version != _CONNECTOR_VERSION
            or source.platform != "local"
            or source.canonical_identity_scheme != "local-content-v1"
            or source.stable_video_identity != digest
            or source.canonical_identity != f"local-content-v1:{digest}"
            or source.canonical_uri is not None
            or source.logical_reference != f"urn:alltonote:local-content:{digest}"
            or source.materialization_policy is not MaterializationPolicy.EXTERNAL_LOCAL
        ):
            raise _contract_invalid()
        path = binding.path
        if _path_chain_has_reparse_point(path):
            raise _local_changed()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise _local_changed() from None
        if resolved != path or not resolved.is_file():
            raise _local_changed()
        if verify_content:
            try:
                current_digest = _content_digest(resolved)
            except DomainError:
                raise _local_changed() from None
            if current_digest != digest:
                raise _local_changed()
        return resolved

    @staticmethod
    def _resolve_direct_remote(parsed: SplitResult) -> ResolvedVideoSource:
        host = _host(parsed)
        if _host_is(host, "bilibili.com"):
            return LegacyVideoSourceAdapter._resolve_bilibili(parsed)
        if _host_is(host, "youtube.com") or host == "youtu.be":
            return LegacyVideoSourceAdapter._resolve_youtube(parsed)
        if _host_is(host, "douyin.com"):
            return LegacyVideoSourceAdapter._resolve_douyin(parsed)
        if _host_is(host, "kuaishou.com"):
            return LegacyVideoSourceAdapter._resolve_kuaishou(parsed)
        raise _unsupported()

    def _resolve_redirect(self, value: str, connector_id: str) -> SplitResult:
        try:
            final_value = (
                self._redirect_resolver(value)
                if self._redirect_resolver is not None
                else self._resolve_redirect_chain(value, connector_id)
            )
            parsed = _strict_http_url(final_value)
        except DomainError:
            raise
        except Exception as error:
            raise _source_error(
                "source_resolve_transient",
                ErrorCategory.RETRYABLE_RUNTIME,
                "The video short link could not be resolved",
                connector_id=connector_id,
            ) from None
        host = _host(parsed)
        expected = {
            "bilibili": "bilibili.com",
            "douyin": "douyin.com",
            "kuaishou": "kuaishou.com",
        }[connector_id]
        if not _host_is(host, expected):
            raise _unsupported()
        return parsed

    def _resolve_redirect_chain(self, value: str, connector_id: str) -> str:
        short_host = {
            "bilibili": "b23.tv",
            "douyin": "v.douyin.com",
            "kuaishou": "v.kuaishou.com",
        }[connector_id]
        final_host = {
            "bilibili": "bilibili.com",
            "douyin": "douyin.com",
            "kuaishou": "kuaishou.com",
        }[connector_id]
        current = value
        for _ in range(5):
            parsed = _strict_http_url(current)
            host = _host(parsed)
            if host != short_host:
                raise _unsupported()
            _require_public_host(host, self._address_resolver)
            response = self._redirect_transport(current)
            try:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    raise _unsupported()
                location = response.headers.get("Location") or response.headers.get(
                    "location"
                )
                if not isinstance(location, str) or not location:
                    raise _unsupported()
                next_value = urljoin(current, location)
            finally:
                response.close()
            next_parsed = _strict_http_url(next_value)
            next_host = _host(next_parsed)
            if _host_is(next_host, final_host):
                _require_public_host(next_host, self._address_resolver)
                return next_value
            if next_host != short_host:
                raise _unsupported()
            current = next_value
        raise _unsupported()

    def _resolve_local(self, path: Path) -> ResolvedVideoSource:
        if _path_chain_has_reparse_point(path):
            raise _unsupported()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise _source_error(
                "source_local_unreadable",
                ErrorCategory.INVALID_REQUEST,
                "The local video source is not readable",
            ) from None
        if not resolved.is_file() or _is_reparse_point(resolved):
            raise _unsupported()
        digest = _content_digest(resolved)
        binding = LocalMachineBinding(
            binding_id=_path_binding_id(self._local_machine_id, resolved),
            machine_id=self._local_machine_id,
            content_sha256=digest,
            path=resolved,
        )
        return ResolvedVideoSource(
            connector_id="local",
            connector_version=_CONNECTOR_VERSION,
            platform="local",
            canonical_identity_scheme="local-content-v1",
            stable_video_identity=digest,
            canonical_identity=f"local-content-v1:{digest}",
            canonical_uri=None,
            logical_reference=f"urn:alltonote:local-content:{digest}",
            materialization_policy=MaterializationPolicy.EXTERNAL_LOCAL,
            content_sha256=digest,
            local_binding=binding,
        )

    @staticmethod
    def _resolve_bilibili(parsed: SplitResult) -> ResolvedVideoSource:
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2 or path_parts[0].casefold() != "video":
            raise _unsupported()
        video_id = path_parts[1]
        if _BILIBILI_ID.fullmatch(video_id) is None:
            raise _unsupported()
        page_values = parse_qs(parsed.query).get("p", ["1"])
        if len(page_values) != 1 or not page_values[0].isdigit():
            raise _unsupported()
        page = int(page_values[0])
        if page < 1:
            raise _unsupported()
        stable = f"{video_id}:p={page}"
        canonical_uri = f"https://www.bilibili.com/video/{video_id}?p={page}"
        return _remote_source(
            connector_id="bilibili",
            scheme="bilibili-video-v1",
            stable_identity=stable,
            canonical_uri=canonical_uri,
        )

    @staticmethod
    def _resolve_youtube(parsed: SplitResult) -> ResolvedVideoSource:
        host = _host(parsed)
        path_parts = [part for part in parsed.path.split("/") if part]
        video_id: str | None = None
        if host == "youtu.be" and path_parts:
            video_id = path_parts[0]
        elif path_parts and path_parts[0].casefold() in {"shorts", "embed"}:
            if len(path_parts) >= 2:
                video_id = path_parts[1]
        elif parsed.path.rstrip("/").casefold() == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            if len(values) == 1:
                video_id = values[0]
        if video_id is None or _YOUTUBE_ID.fullmatch(video_id) is None:
            raise _unsupported()
        return _remote_source(
            connector_id="youtube",
            scheme="youtube-video-v1",
            stable_identity=video_id,
            canonical_uri=f"https://www.youtube.com/watch?v={video_id}",
        )

    @staticmethod
    def _resolve_douyin(parsed: SplitResult) -> ResolvedVideoSource:
        video_id = _path_identifier(parsed.path, "video", _DOUYIN_ID)
        return _remote_source(
            connector_id="douyin",
            scheme="douyin-aweme-v1",
            stable_identity=video_id,
            canonical_uri=f"https://www.douyin.com/video/{video_id}",
        )

    @staticmethod
    def _resolve_kuaishou(parsed: SplitResult) -> ResolvedVideoSource:
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2 or path_parts[-2].casefold() not in {
            "short-video",
            "photo",
        }:
            raise _unsupported()
        photo_id = path_parts[-1]
        if _KUAISHOU_ID.fullmatch(photo_id) is None:
            raise _unsupported()
        return _remote_source(
            connector_id="kuaishou",
            scheme="kuaishou-photo-v1",
            stable_identity=photo_id,
            canonical_uri=f"https://www.kuaishou.com/short-video/{photo_id}",
        )

    def _create_downloader(self, connector_id: str) -> object:
        factory = self._factories.get(connector_id)
        if factory is None:
            raise _unsupported()
        try:
            return factory()
        except Exception as error:
            raise _map_legacy_error(error, connector_id) from None

    def _acquire_local(
        self,
        source: ResolvedVideoSource,
        *,
        need_media: bool,
        output_dir: Path,
        token: CancellationTokenPort,
    ) -> AcquiredVideoSource:
        binding = source.local_binding
        assert binding is not None
        token.raise_if_cancelled()
        snapshot = (
            self._snapshot_local_source(source, output_dir, token)
            if need_media
            else None
        )
        return AcquiredVideoSource(
            source=source,
            title=binding.path.stem,
            duration_ms=None,
            cover_uri=None,
            media_path=snapshot,
            video_path=snapshot,
            subtitle_availability=SubtitleAvailability.NOT_SUPPORTED,
        )

    def _snapshot_local_source(
        self,
        source: ResolvedVideoSource,
        output_dir: Path,
        token: CancellationTokenPort,
    ) -> Path:
        source_path = self._validate_local_contract(source, verify_content=False)
        digest = source.content_sha256
        assert digest is not None
        suffix = source_path.suffix
        if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix) is None:
            suffix = ".media"
        digest_hex = digest.removeprefix("sha256:")
        final_path = output_dir / f"local-source-{digest_hex[:16]}{suffix}"
        partial_path = output_dir / f".{final_path.name}.partial"
        if final_path.exists() or partial_path.exists():
            raise _source_error(
                "source_snapshot_conflict",
                ErrorCategory.CONFLICT,
                "The local source snapshot already exists",
                connector_id="local",
            )
        copied_digest = hashlib.sha256()
        try:
            with source_path.open("rb") as source_stream:
                source_stat = os.fstat(source_stream.fileno())
                path_stat = source_path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(source_stat.st_mode)
                    or (source_stat.st_dev, source_stat.st_ino)
                    != (path_stat.st_dev, path_stat.st_ino)
                    or _path_chain_has_reparse_point(source_path)
                ):
                    raise _local_changed()
                with partial_path.open("xb") as target_stream:
                    while chunk := source_stream.read(1024 * 1024):
                        token.raise_if_cancelled()
                        target_stream.write(chunk)
                        copied_digest.update(chunk)
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
            if f"sha256:{copied_digest.hexdigest()}" != digest:
                raise _local_changed()
            os.rename(partial_path, final_path)
        except Exception:
            try:
                partial_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return final_path.resolve(strict=True)

    @staticmethod
    def _acquire_subtitle(
        downloader: object,
        source: ResolvedVideoSource,
        output_dir: Path,
        token: CancellationTokenPort,
    ) -> tuple[object | None, SubtitleAvailability]:
        spec = _DEFAULT_SPECS[source.connector_id]
        if not spec.supports_subtitles:
            return None, SubtitleAvailability.NOT_SUPPORTED
        try:
            subtitle = downloader.download_subtitles(
                source.canonical_uri,
                output_dir=str(output_dir),
            )
        except LegacyNoSubtitleError:
            return None, SubtitleAvailability.UNAVAILABLE
        except Exception as error:
            raise _map_legacy_error(error, source.connector_id) from None
        token.raise_if_cancelled()
        if subtitle is None:
            return None, SubtitleAvailability.UNKNOWN
        return subtitle, SubtitleAvailability.AVAILABLE

    @staticmethod
    def _build_acquired(
        source: ResolvedVideoSource,
        result: object | None,
        *,
        output_dir: Path,
        need_media: bool,
        subtitle: object | None,
        subtitle_availability: SubtitleAvailability,
    ) -> AcquiredVideoSource:
        title = getattr(result, "title", None) if result is not None else None
        duration = getattr(result, "duration", None) if result is not None else None
        duration_ms: int | None = None
        if type(duration) in {int, float} and duration >= 0:
            multiplier = 1 if source.connector_id in {"douyin", "kuaishou"} else 1000
            duration_ms = int(duration * multiplier)
        cover_uri = _safe_public_url(getattr(result, "cover_url", None))
        media_path = None
        video_path = None
        if need_media:
            if result is None:
                raise _media_missing(source.connector_id)
            file_path = getattr(result, "file_path", None)
            raw_video_path = getattr(result, "video_path", None)
            if not isinstance(file_path, str) or not file_path:
                raise _media_missing(source.connector_id)
            media_path = _validated_remote_media_path(file_path, output_dir)
            if isinstance(raw_video_path, str) and raw_video_path:
                video_path = _validated_remote_media_path(
                    raw_video_path,
                    output_dir,
                )
        return AcquiredVideoSource(
            source=source,
            title=title if isinstance(title, str) and title else None,
            duration_ms=duration_ms,
            cover_uri=cover_uri,
            media_path=media_path,
            video_path=video_path,
            subtitle_availability=subtitle_availability,
            opaque_subtitle=subtitle,
        )


def _remote_source(
    *,
    connector_id: str,
    scheme: str,
    stable_identity: str,
    canonical_uri: str,
) -> ResolvedVideoSource:
    return ResolvedVideoSource(
        connector_id=connector_id,
        connector_version=_CONNECTOR_VERSION,
        platform=connector_id,
        canonical_identity_scheme=scheme,
        stable_video_identity=stable_identity,
        canonical_identity=f"{scheme}:{stable_identity}",
        canonical_uri=canonical_uri,
        logical_reference=None,
        materialization_policy=MaterializationPolicy.REFERENCE_ONLY,
    )


def _path_identifier(path: str, prefix: str, pattern: re.Pattern[str]) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[-2].casefold() != prefix:
        raise _unsupported()
    value = parts[-1]
    if pattern.fullmatch(value) is None:
        raise _unsupported()
    return value


def _safe_public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = _strict_http_url(value)
    except DomainError:
        return None
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path, "", ""))


def _validated_remote_media_path(value: str, output_dir: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = output_dir / candidate
    if _path_chain_has_reparse_point(candidate):
        raise _source_error(
            "source_media_path_invalid",
            ErrorCategory.INTERNAL,
            "The legacy downloader returned an unsafe media path",
        )
    if not candidate.exists():
        raise _media_missing(None)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(output_dir)
    except (OSError, RuntimeError, ValueError):
        raise _source_error(
            "source_media_path_invalid",
            ErrorCategory.INTERNAL,
            "The legacy downloader returned an unsafe media path",
        ) from None
    if not resolved.is_file():
        raise _source_error(
            "source_media_path_invalid",
            ErrorCategory.INTERNAL,
            "The legacy downloader returned an unsafe media path",
        )
    return resolved


def _media_missing(connector_id: str | None) -> DomainError:
    return _source_error(
        "source_media_missing",
        ErrorCategory.RETRYABLE_RUNTIME,
        "The legacy downloader did not produce the requested media",
        connector_id=connector_id,
    )


class _SourceIdentityCachePort(Protocol):
    def read_source_identity_candidate(
        self, connector_id: str, canonical_identity: str
    ) -> SourceIdentityBinding | None: ...

    def cache_source_identity_candidate(
        self, binding: SourceIdentityBinding
    ) -> None: ...

    def discard_source_identity_candidate(
        self, binding: SourceIdentityBinding
    ) -> None: ...

    def replace_source_identity_candidate(
        self,
        observed: SourceIdentityBinding,
        replacement: SourceIdentityBinding,
    ) -> bool: ...


class _PortableSourceTruthPort(Protocol):
    def verify_committed_source_binding(
        self, workspace_root: Path, binding: SourceIdentityBinding
    ) -> bool: ...

    def iter_verified_source_bindings(
        self, workspace_root: Path
    ) -> tuple[SourceIdentityBinding, ...]: ...


class VerifiedSourceIdentityRegistry:
    """Workspace-local cache whose every returned binding is Portable-verified."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        cache: _SourceIdentityCachePort,
        truth: _PortableSourceTruthPort,
    ) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._cache = cache
        self._truth = truth

    def resolve_verified(
        self, connector_id: str, canonical_identity: str
    ) -> SourceIdentityBinding | None:
        candidate = self._cache.read_source_identity_candidate(
            connector_id,
            canonical_identity,
        )
        binding = self._unique_truth_binding(connector_id, canonical_identity)
        if binding is None:
            if candidate is not None and not self._truth.verify_committed_source_binding(
                self._workspace_root,
                candidate,
            ):
                self._cache.discard_source_identity_candidate(candidate)
            return None
        if candidate == binding:
            return binding
        if candidate is not None:
            if self._cache.replace_source_identity_candidate(candidate, binding):
                return binding
            return (
                binding
                if self._cache.read_source_identity_candidate(
                    connector_id,
                    canonical_identity,
                )
                == binding
                else None
            )
        try:
            self._cache.cache_source_identity_candidate(binding)
        except DomainError:
            return (
                binding
                if self._cache.read_source_identity_candidate(
                    connector_id,
                    canonical_identity,
                )
                == binding
                else None
            )
        return binding

    def rebuild_from_portable_truth(self) -> int:
        rebuilt = 0
        for binding in self._unique_truth_bindings():
            observed = self._cache.read_source_identity_candidate(
                binding.connector_id,
                binding.canonical_identity,
            )
            if observed == binding:
                rebuilt += 1
                continue
            if observed is not None:
                if self._cache.replace_source_identity_candidate(observed, binding):
                    rebuilt += 1
                continue
            try:
                self._cache.cache_source_identity_candidate(binding)
            except DomainError:
                continue
            rebuilt += 1
        return rebuilt

    def _unique_truth_binding(
        self,
        connector_id: str,
        canonical_identity: str,
    ) -> SourceIdentityBinding | None:
        return next(
            (
                binding
                for binding in self._unique_truth_bindings()
                if self._binding_matches(
                    binding,
                    connector_id,
                    canonical_identity,
                )
            ),
            None,
        )

    def _unique_truth_bindings(self) -> tuple[SourceIdentityBinding, ...]:
        grouped: dict[tuple[str, str], list[SourceIdentityBinding]] = {}
        for binding in self._truth.iter_verified_source_bindings(self._workspace_root):
            if not self._truth.verify_committed_source_binding(
                self._workspace_root,
                binding,
            ):
                continue
            grouped.setdefault(
                (binding.connector_id, binding.canonical_identity), []
            ).append(binding)
        unique: list[SourceIdentityBinding] = []
        for candidates in grouped.values():
            if len({candidate.source_id for candidate in candidates}) != 1:
                continue
            unique.append(
                max(candidates, key=lambda candidate: candidate.owning_bundle_id)
            )
        return tuple(unique)

    @staticmethod
    def _binding_matches(
        binding: SourceIdentityBinding,
        connector_id: str,
        canonical_identity: str,
    ) -> bool:
        return (
            binding.connector_id == connector_id
            and binding.canonical_identity == canonical_identity
        )


__all__ = [
    "LegacyAuthenticationError",
    "LegacyNoSubtitleError",
    "LegacyTransientError",
    "LegacyUnsupportedSourceError",
    "LegacyVideoSourceAdapter",
    "VerifiedSourceIdentityRegistry",
    "default_connector_ids",
]
