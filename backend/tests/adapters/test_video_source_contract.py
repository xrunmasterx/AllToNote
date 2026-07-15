from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.sources.legacy_video import (
    LegacyAuthenticationError,
    LegacyNoSubtitleError,
    LegacyTransientError,
    LegacyUnsupportedSourceError,
    LegacyVideoSourceAdapter,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.source import (
    LocalMachineBinding,
    MaterializationPolicy,
    SubtitleAvailability,
)


class _Token:
    def __init__(self) -> None:
        self.checks = 0

    def raise_if_cancelled(self) -> None:
        self.checks += 1


class _LegacyDownloader:
    def __init__(
        self,
        *,
        subtitle: object | None = None,
        subtitle_error: Exception | None = None,
        supports_skip_download: bool = True,
    ) -> None:
        self.subtitle = subtitle
        self.subtitle_error = subtitle_error
        self.supports_skip_download = supports_skip_download
        self.download_calls: list[tuple[str, dict[str, object]]] = []
        self.subtitle_calls: list[tuple[str, str]] = []

    def download(self, input_value: str, **kwargs: object) -> object:
        self.download_calls.append((input_value, kwargs))
        return SimpleNamespace(
            file_path=str(Path(str(kwargs["output_dir"])) / "audio.mp3"),
            video_path=None,
            title="Course",
            duration=12.5,
            cover_url="https://img.example/cover.jpg",
            platform="youtube",
            video_id="dQw4w9WgXcQ",
            raw_info={"authorization": "must-not-cross-boundary"},
        )

    def download_subtitles(self, input_value: str, output_dir: str) -> object | None:
        self.subtitle_calls.append((input_value, output_dir))
        if self.subtitle_error is not None:
            raise self.subtitle_error
        return self.subtitle


@pytest.mark.parametrize(
    ("input_value", "connector_id", "stable_video_identity"),
    (
        (
            "https://www.bilibili.com/video/BV1vc411b7Wa?p=2&token=secret",
            "bilibili",
            "BV1vc411b7Wa:p=2",
        ),
        (
            "https://www.bilibili.com/video/BV1vc411b7Wa",
            "bilibili",
            "BV1vc411b7Wa:p=1",
        ),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=secret",
            "youtube",
            "dQw4w9WgXcQ",
        ),
        (
            "https://www.douyin.com/video/7280000000000000001?msToken=secret",
            "douyin",
            "7280000000000000001",
        ),
        (
            "https://www.kuaishou.com/short-video/3xabcdef123?authToken=secret",
            "kuaishou",
            "3xabcdef123",
        ),
    ),
)
def test_resolve_returns_stable_canonical_identity_without_credentials(
    input_value: str,
    connector_id: str,
    stable_video_identity: str,
) -> None:
    adapter = LegacyVideoSourceAdapter(local_machine_id="machine-a")

    resolved = adapter.resolve(input_value)

    assert resolved.connector_id == connector_id
    assert resolved.stable_video_identity == stable_video_identity
    expected_scheme = {
        "bilibili": "bilibili-video-v1",
        "youtube": "youtube-video-v1",
        "douyin": "douyin-aweme-v1",
        "kuaishou": "kuaishou-photo-v1",
    }[connector_id]
    assert resolved.canonical_identity == f"{expected_scheme}:{stable_video_identity}"
    assert "secret" not in resolved.canonical_identity
    assert "token" not in resolved.canonical_identity.casefold()
    assert resolved.canonical_uri is not None
    assert "secret" not in resolved.canonical_uri


@pytest.mark.parametrize(
    ("short_url", "final_url", "connector_id", "stable_video_identity"),
    (
        (
            "https://b23.tv/temporary?token=secret",
            "https://www.bilibili.com/video/BV1vc411b7Wa?p=3&signature=short-lived",
            "bilibili",
            "BV1vc411b7Wa:p=3",
        ),
        (
            "https://v.douyin.com/temporary/",
            "https://www.douyin.com/video/7280000000000000001?msToken=short-lived",
            "douyin",
            "7280000000000000001",
        ),
        (
            "https://v.kuaishou.com/temporary",
            "https://www.kuaishou.com/short-video/3xabcdef123?authToken=short-lived",
            "kuaishou",
            "3xabcdef123",
        ),
    ),
)
def test_short_or_redirect_url_never_becomes_source_identity(
    short_url: str,
    final_url: str,
    connector_id: str,
    stable_video_identity: str,
) -> None:
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        redirect_resolver=lambda value: final_url,
    )

    resolved = adapter.resolve(short_url)

    assert resolved.connector_id == connector_id
    assert resolved.stable_video_identity == stable_video_identity
    assert "temporary" not in resolved.canonical_identity
    assert "short-lived" not in resolved.canonical_identity


class _RedirectResponse:
    def __init__(self, location: str) -> None:
        self.status_code = 302
        self.headers = {"Location": location}
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_short_redirect_accepts_public_official_final_host() -> None:
    response = _RedirectResponse(
        "https://www.bilibili.com/video/BV1vc411b7Wa?p=2"
    )
    requested: list[str] = []

    def transport(url: str) -> _RedirectResponse:
        requested.append(url)
        return response

    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        redirect_transport=transport,
        address_resolver=lambda _host: ("93.184.216.34",),
    )

    resolved = adapter.resolve("https://b23.tv/first")

    assert resolved.canonical_identity == "bilibili-video-v1:BV1vc411b7Wa:p=2"
    assert requested == ["https://b23.tv/first"]
    assert response.closed is True


@pytest.mark.parametrize(
    "dangerous_hop",
    (
        "http://127.0.0.1/private",
        "http://10.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
    ),
)
def test_short_redirect_rejects_dangerous_hop_before_request(
    dangerous_hop: str,
) -> None:
    first = _RedirectResponse("https://b23.tv/second")
    second = _RedirectResponse(dangerous_hop)
    responses = iter((first, second))
    requested: list[str] = []

    def transport(url: str) -> _RedirectResponse:
        requested.append(url)
        return next(responses)

    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        redirect_transport=transport,
        address_resolver=lambda _host: ("93.184.216.34",),
    )

    with pytest.raises(DomainError) as caught:
        adapter.resolve("https://b23.tv/first")

    assert caught.value.code == "source_unsupported"
    assert requested == ["https://b23.tv/first", "https://b23.tv/second"]
    assert first.closed is True
    assert second.closed is True


def test_local_source_uses_machine_binding_without_portable_absolute_path(
    tmp_path: Path,
) -> None:
    video = tmp_path / "personal" / "course.mp4"
    video.parent.mkdir()
    video.write_bytes(b"fixture")

    renamed = tmp_path / "renamed.mp4"
    renamed.write_bytes(video.read_bytes())

    changed = tmp_path / "changed.mp4"
    changed.write_bytes(b"different")

    first = LegacyVideoSourceAdapter(local_machine_id="machine-a").resolve(str(video))
    same_content = LegacyVideoSourceAdapter(local_machine_id="machine-a").resolve(
        str(renamed)
    )
    changed_content = LegacyVideoSourceAdapter(local_machine_id="machine-a").resolve(
        str(changed)
    )
    other_machine = LegacyVideoSourceAdapter(local_machine_id="machine-b").resolve(
        str(video)
    )

    assert first.connector_id == "local"
    assert first.materialization_policy is MaterializationPolicy.EXTERNAL_LOCAL
    assert first.local_binding is not None
    assert first.local_binding.path == video.resolve()
    assert first.local_binding.binding_id != same_content.local_binding.binding_id
    assert first.canonical_identity == same_content.canonical_identity
    assert first.canonical_identity != changed_content.canonical_identity
    assert first.canonical_identity == other_machine.canonical_identity
    assert first.local_binding.binding_id != other_machine.local_binding.binding_id
    assert str(video.resolve()) not in first.canonical_identity
    assert first.canonical_uri is None
    assert first.logical_reference == f"urn:alltonote:local-content:{first.content_sha256}"


def test_local_source_rejects_symlink_binding(tmp_path: Path) -> None:
    target = tmp_path / "target.mp4"
    target.write_bytes(b"fixture")
    link = tmp_path / "link.mp4"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows host")

    with pytest.raises(DomainError) as caught:
        LegacyVideoSourceAdapter(local_machine_id="machine-a").resolve(str(link))

    assert caught.value.code == "source_unsupported"


def test_youtube_metadata_only_acquisition_sets_both_legacy_skip_flags(
    tmp_path: Path,
) -> None:
    subtitle = object()
    downloader = _LegacyDownloader(subtitle=subtitle)
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"youtube": lambda: downloader},
    )
    resolved = adapter.resolve("https://youtu.be/dQw4w9WgXcQ")
    token = _Token()

    acquired = adapter.acquire(
        resolved,
        need_media=False,
        output_dir=tmp_path,
        token=token,
    )

    assert downloader.download_calls == [
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            {
                "output_dir": str(tmp_path),
                "quality": "fast",
                "need_video": False,
                "skip_download": True,
            },
        )
    ]
    assert acquired.media_path is None
    assert acquired.video_path is None
    assert acquired.subtitle_availability is SubtitleAvailability.AVAILABLE
    assert acquired.opaque_subtitle is subtitle
    assert token.checks >= 2


@pytest.mark.parametrize(
    "mutate",
    (
        lambda source: replace(
            source,
            canonical_uri=(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&token=secret"
            ),
        ),
        lambda source: replace(source, connector_id="unknown"),
        lambda source: replace(
            source,
            stable_video_identity="aaaaaaaaaaa",
            canonical_identity="youtube-video-v1:aaaaaaaaaaa",
        ),
        lambda source: replace(
            source,
            canonical_uri="https://www.youtube.com:444/watch?v=dQw4w9WgXcQ",
        ),
        lambda source: replace(
            source,
            canonical_uri="http://127.0.0.1/watch?v=dQw4w9WgXcQ",
        ),
        lambda source: replace(
            source,
            local_binding=LocalMachineBinding(
                binding_id="forged-binding",
                machine_id="machine-a",
                content_sha256="f" * 64,
                path=Path("C:/secret/video.mp4"),
            ),
        ),
        lambda source: replace(source, content_sha256="f" * 64),
    ),
)
def test_acquire_rejects_forged_remote_source_before_downloader_call(
    mutate: object,
    tmp_path: Path,
) -> None:
    downloader = _LegacyDownloader()
    factory_calls: list[None] = []

    def create_downloader() -> _LegacyDownloader:
        factory_calls.append(None)
        return downloader

    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"youtube": create_downloader},
    )
    source = adapter.resolve("https://youtu.be/dQw4w9WgXcQ")

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            mutate(source),
            need_media=False,
            output_dir=tmp_path,
            token=_Token(),
        )

    assert caught.value.code == "source_contract_invalid"
    assert factory_calls == []
    assert downloader.download_calls == []
    assert downloader.subtitle_calls == []


def test_remote_media_path_must_exist_inside_requested_output_dir(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"media")

    class _OutsideDownloader(_LegacyDownloader):
        def download(self, input_value: str, **kwargs: object) -> object:
            super().download(input_value, **kwargs)
            return SimpleNamespace(
                file_path=str(outside),
                video_path=None,
                title="Course",
                duration=12.5,
                cover_url=None,
            )

    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"youtube": _OutsideDownloader},
    )

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            adapter.resolve("https://youtu.be/dQw4w9WgXcQ"),
            need_media=True,
            output_dir=tmp_path / "attempt",
            token=_Token(),
        )

    assert caught.value.code == "source_media_path_invalid"


@pytest.mark.parametrize("result_kind", ("none", "empty", "missing"))
def test_remote_media_acquisition_requires_existing_media_file(
    result_kind: str,
    tmp_path: Path,
) -> None:
    class _MissingMediaDownloader(_LegacyDownloader):
        def download(self, input_value: str, **kwargs: object) -> object | None:
            super().download(input_value, **kwargs)
            if result_kind == "none":
                return None
            file_path = "" if result_kind == "empty" else str(
                Path(str(kwargs["output_dir"])) / "missing.mp3"
            )
            return SimpleNamespace(
                file_path=file_path,
                video_path=None,
                title="Course",
                duration=12.5,
                cover_url=None,
            )

    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"youtube": _MissingMediaDownloader},
    )

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            adapter.resolve("https://youtu.be/dQw4w9WgXcQ"),
            need_media=True,
            output_dir=tmp_path / "attempt",
            token=_Token(),
        )

    assert caught.value.code == "source_media_missing"


def test_remote_media_acquisition_returns_existing_attempt_file(
    tmp_path: Path,
) -> None:
    class _ValidMediaDownloader(_LegacyDownloader):
        def download(self, input_value: str, **kwargs: object) -> object:
            super().download(input_value, **kwargs)
            media = Path(str(kwargs["output_dir"])) / "audio.mp3"
            media.write_bytes(b"media")
            return SimpleNamespace(
                file_path=str(media),
                video_path=None,
                title="Course",
                duration=12.5,
                cover_url=None,
            )

    output = tmp_path / "attempt"
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"youtube": _ValidMediaDownloader},
    )

    acquired = adapter.acquire(
        adapter.resolve("https://youtu.be/dQw4w9WgXcQ"),
        need_media=True,
        output_dir=output,
        token=_Token(),
    )

    assert acquired.media_path == (output / "audio.mp3").resolve()
    assert acquired.media_path.is_file()


def test_remote_acquisition_rejects_reparse_output_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    output_link = tmp_path / "attempt-link"
    try:
        output_link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows host")
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"youtube": _LegacyDownloader},
    )

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            adapter.resolve("https://youtu.be/dQw4w9WgXcQ"),
            need_media=True,
            output_dir=output_link,
            token=_Token(),
        )

    assert caught.value.code == "source_output_path_invalid"


@pytest.mark.parametrize("mutation", ("overwrite", "delete", "symlink"))
def test_local_acquire_detects_binding_change_after_resolve(
    mutation: str,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"original")
    adapter = LegacyVideoSourceAdapter(local_machine_id="machine-a")
    resolved = adapter.resolve(str(source_path))
    if mutation == "overwrite":
        source_path.write_bytes(b"changed")
    elif mutation == "delete":
        source_path.unlink()
    else:
        target = tmp_path / "replacement.mp4"
        target.write_bytes(b"original")
        source_path.unlink()
        try:
            source_path.symlink_to(target)
        except OSError:
            pytest.skip("symlinks are unavailable on this Windows host")

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            resolved,
            need_media=False,
            output_dir=tmp_path / "attempt",
            token=_Token(),
        )

    assert caught.value.code == "source_local_changed"


def test_local_media_is_snapshotted_into_attempt_directory(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"local-media")
    output = tmp_path / "attempt"
    adapter = LegacyVideoSourceAdapter(local_machine_id="machine-a")
    resolved = adapter.resolve(str(source_path))

    acquired = adapter.acquire(
        resolved,
        need_media=True,
        output_dir=output,
        token=_Token(),
    )

    assert acquired.media_path is not None
    assert acquired.video_path == acquired.media_path
    assert acquired.media_path != source_path
    assert acquired.media_path.read_bytes() == b"local-media"
    acquired.media_path.resolve().relative_to(output.resolve())


def test_bilibili_subtitle_path_never_calls_media_download(
    tmp_path: Path,
) -> None:
    subtitle = object()
    downloader = _LegacyDownloader(
        subtitle=subtitle,
        supports_skip_download=False,
    )
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"bilibili": lambda: downloader},
    )
    resolved = adapter.resolve("https://www.bilibili.com/video/BV1vc411b7Wa")

    acquired = adapter.acquire(
        resolved,
        need_media=False,
        output_dir=tmp_path,
        token=_Token(),
    )

    assert downloader.download_calls == []
    assert acquired.media_path is None
    assert acquired.subtitle_availability is SubtitleAvailability.AVAILABLE
    assert acquired.opaque_subtitle is subtitle


@pytest.mark.parametrize("connector_id", ("douyin", "kuaishou", "local"))
def test_connector_without_metadata_or_subtitle_api_does_not_download_media(
    connector_id: str,
    tmp_path: Path,
) -> None:
    downloader = _LegacyDownloader(supports_skip_download=False)
    input_value = {
        "douyin": "https://www.douyin.com/video/7280000000000000001",
        "kuaishou": "https://www.kuaishou.com/short-video/3xabcdef123",
        "local": str((tmp_path / "source.mp4")),
    }[connector_id]
    if connector_id == "local":
        Path(input_value).write_bytes(b"fixture")
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={connector_id: lambda: downloader},
    )

    acquired = adapter.acquire(
        adapter.resolve(input_value),
        need_media=False,
        output_dir=tmp_path / "work",
        token=_Token(),
    )

    assert downloader.download_calls == []
    assert downloader.subtitle_calls == []
    assert acquired.media_path is None
    assert acquired.subtitle_availability is SubtitleAvailability.NOT_SUPPORTED


def test_legacy_none_subtitle_is_unknown_not_confirmed_unavailable(
    tmp_path: Path,
) -> None:
    downloader = _LegacyDownloader(subtitle=None, supports_skip_download=False)
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"bilibili": lambda: downloader},
    )

    acquired = adapter.acquire(
        adapter.resolve("https://www.bilibili.com/video/BV1vc411b7Wa"),
        need_media=False,
        output_dir=tmp_path,
        token=_Token(),
    )

    assert acquired.subtitle_availability is SubtitleAvailability.UNKNOWN


def test_explicit_no_subtitle_outcome_is_unavailable(tmp_path: Path) -> None:
    downloader = _LegacyDownloader(
        subtitle_error=LegacyNoSubtitleError(),
        supports_skip_download=False,
    )
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"bilibili": lambda: downloader},
    )

    acquired = adapter.acquire(
        adapter.resolve("https://www.bilibili.com/video/BV1vc411b7Wa"),
        need_media=False,
        output_dir=tmp_path,
        token=_Token(),
    )

    assert acquired.subtitle_availability is SubtitleAvailability.UNAVAILABLE


@pytest.mark.parametrize(
    ("legacy_error", "code", "category"),
    (
        (
            LegacyTransientError(),
            "source_acquire_transient",
            ErrorCategory.RETRYABLE_RUNTIME,
        ),
        (
            LegacyAuthenticationError(),
            "source_authentication_required",
            ErrorCategory.POLICY_DENIED,
        ),
        (
            LegacyUnsupportedSourceError(),
            "source_unsupported",
            ErrorCategory.INVALID_REQUEST,
        ),
    ),
)
def test_legacy_failures_map_to_stable_source_errors(
    legacy_error: Exception,
    code: str,
    category: ErrorCategory,
    tmp_path: Path,
) -> None:
    downloader = _LegacyDownloader(
        subtitle_error=legacy_error,
        supports_skip_download=False,
    )
    adapter = LegacyVideoSourceAdapter(
        local_machine_id="machine-a",
        factories={"bilibili": lambda: downloader},
    )

    with pytest.raises(DomainError) as caught:
        adapter.acquire(
            adapter.resolve("https://www.bilibili.com/video/BV1vc411b7Wa"),
            need_media=False,
            output_dir=tmp_path,
            token=_Token(),
        )

    assert caught.value.code == code
    assert caught.value.category is category
    assert caught.value.details == {"connector_id": "bilibili"}


@pytest.mark.parametrize(
    "input_value",
    (
        "https://www.tiktok.com/@creator/video/7280000000000000001",
        "https://www.xiaoyuzhoufm.com/episode/secret",
    ),
)
def test_unproven_connectors_remain_stably_unsupported(input_value: str) -> None:
    adapter = LegacyVideoSourceAdapter(local_machine_id="machine-a")

    with pytest.raises(DomainError) as caught:
        adapter.resolve(input_value)

    assert caught.value.code == "source_unsupported"


@pytest.mark.parametrize(
    "input_value",
    (
        "https://www.bilibili.com.evil.test/video/BV1vc411b7Wa",
        "https://www.youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
    ),
)
def test_platform_lookalike_hosts_are_rejected(input_value: str) -> None:
    with pytest.raises(DomainError) as caught:
        LegacyVideoSourceAdapter(local_machine_id="machine-a").resolve(input_value)

    assert caught.value.code == "source_unsupported"
