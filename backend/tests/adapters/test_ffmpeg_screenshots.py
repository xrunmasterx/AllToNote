from __future__ import annotations

import importlib
import signal
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application.video_acquisition import (
    AttemptStoredAsset,
    StoredAssetRole,
    VideoAcquisition,
)
from app.core.application.video_service import (
    VideoStepExecutionContext,
    build_screenshot_plan,
)
from app.core.domain.video import (
    GeneratedVideoDraft,
    JobState,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
)
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import AttemptState, CheckpointMetadata
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.portable.bundle_assembler import VideoSourceMetadata
from app.core.ports.jobs import ScreenshotOutputCapability, ScreenshotSourceCapability
from app.core.ports.source import SubtitleAvailability


VALID_WEBP = bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"
)
JOB_ID = "job_018cc251-f400-7000-8000-000000000000"
ACQUIRE_ATTEMPT_ID = "att_018cc251-f400-7000-8000-000000000001"
SCREENSHOT_ATTEMPT_ID = "att_018cc251-f400-7000-8000-000000000002"


class _Storage:
    def __init__(self, root: Path, media: Path) -> None:
        self.root = root
        self.media = media
        self.resolve_calls = 0
        self.allocate_calls = 0
        self.read_calls = 0
        self.fail_resolve = False
        self.fail_read = False
        self.fail_cleanup = False

    def resolve_asset(self, stored: object, *, expected_job_id: str, expected_attempt_id: str) -> Path:
        del stored
        self.resolve_calls += 1
        assert expected_job_id == JOB_ID
        assert expected_attempt_id == ACQUIRE_ATTEMPT_ID
        if self.fail_resolve:
            raise OSError(f"private source {self.media}")
        return self.media

    def verify_screenshot_source(
        self, stored: object, *, expected_job_id: str, expected_attempt_id: str
    ) -> ScreenshotSourceCapability:
        path = self.resolve_asset(
            stored,
            expected_job_id=expected_job_id,
            expected_attempt_id=expected_attempt_id,
        )
        path_stat = path.stat()
        return ScreenshotSourceCapability(
            expected_job_id, expected_attempt_id, str(path),
            path_stat.st_dev, path_stat.st_ino, path_stat.st_size,
        )

    def validate_screenshot_source(self, capability: ScreenshotSourceCapability) -> Path:
        assert capability.job_id == JOB_ID
        return self.media

    def allocate_screenshot_output(
        self,
        *,
        job_id: str,
        attempt_id: str,
        artifact_id: str,
        authority: ExecutionAuthority,
    ) -> ScreenshotOutputCapability:
        del authority
        self.allocate_calls += 1
        assert job_id == JOB_ID
        assert attempt_id == SCREENSHOT_ATTEMPT_ID
        output = self.root / "jobs" / job_id / "attempts" / attempt_id / "screenshots" / f".{artifact_id}.partial.webp"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.touch()
        parent_stat = output.parent.stat()
        leaf_stat = output.stat()
        return ScreenshotOutputCapability(
            job_id, attempt_id, artifact_id, str(output),
            parent_stat.st_dev, parent_stat.st_ino, leaf_stat.st_dev, leaf_stat.st_ino,
            "screenshot-owner", 1,
        )

    def validate_screenshot_output(
        self, capability: ScreenshotOutputCapability, *, authority: ExecutionAuthority
    ) -> Path:
        del authority
        return Path(capability.relative_locator)

    def read_screenshot_output(
        self,
        capability: ScreenshotOutputCapability,
        *,
        job_id: str,
        attempt_id: str,
        artifact_id: str,
        authority: ExecutionAuthority,
    ) -> bytes:
        del authority
        self.read_calls += 1
        assert job_id == JOB_ID
        assert attempt_id == SCREENSHOT_ATTEMPT_ID
        output_path = Path(capability.relative_locator)
        assert output_path.name == f".{artifact_id}.partial.webp"
        if self.fail_read:
            raise OSError(f"private output {output_path}")
        return output_path.read_bytes()

    def cleanup_screenshot_output(
        self, capability: ScreenshotOutputCapability, *, authority: ExecutionAuthority
    ) -> None:
        del authority
        if self.fail_cleanup:
            raise OSError(f"private cleanup {capability.relative_locator}")
        output = Path(capability.relative_locator)
        if output.exists():
            output.unlink()


class _Repository:
    def __init__(self, cancellations: tuple[bool, ...] = (False,)) -> None:
        self._cancellations = list(cancellations)

    def is_cancellation_requested(self, job_id: str) -> bool:
        assert job_id == JOB_ID
        if len(self._cancellations) > 1:
            return self._cancellations.pop(0)
        return self._cancellations[0]


class _Process:
    def __init__(self, polls: tuple[int | None, ...] = (0,), *, pid: int = 4321) -> None:
        self._polls = list(polls)
        self.pid = pid
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float] = []
        self.wait_timeouts = 0
        self.output_path: Path | None = None
        self.payload = b""

    def poll(self) -> int | None:
        if len(self._polls) > 1:
            result = self._polls.pop(0)
        else:
            result = self._polls[0]
        if result is not None and self.output_path is not None:
            self.output_path.write_bytes(self.payload)
        return result

    def wait(self, timeout: float) -> int:
        self.wait_calls.append(timeout)
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired("private argv", timeout)
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _ProcessFactory:
    def __init__(
        self,
        payload: bytes = VALID_WEBP,
        *,
        process: _Process | None = None,
        error: OSError | None = None,
    ) -> None:
        self.payload = payload
        self.process = process or _Process()
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> _Process:
        self.calls.append((list(argv), dict(kwargs)))
        if self.error is not None:
            raise self.error
        self.process.output_path = Path(argv[-1])
        self.process.payload = self.payload
        return self.process


def _adapter_type() -> type:
    try:
        module = importlib.import_module("app.adapters.screenshots.ffmpeg")
    except ModuleNotFoundError:
        pytest.fail("FFmpeg screenshot adapter is missing")
    adapter = getattr(module, "FFmpegScreenshotAdapter", None)
    assert isinstance(adapter, type), "FFmpegScreenshotAdapter is missing"
    return adapter


def _transcript() -> TranscriptDocument:
    return TranscriptDocument(
        language="en",
        segments=(TranscriptSegment("seg_000001", 1_000, 2_000, "segment"),),
    )


def _plan() -> tuple[object, ...]:
    transcript = _transcript()
    draft = GeneratedVideoDraft(
        markdown="# Note\n",
        cited_segment_ids=(),
        screenshot_requests=(ScreenshotRequest("seg_000001", 250),),
        model_identity="fixture/model-v1",
        usage={},
        warnings=(),
    )
    return build_screenshot_plan(JOB_ID, ScreenshotPolicy.ON_DEMAND, draft, transcript)


def _acquisition() -> VideoAcquisition:
    content_identity = "sha256:" + "a" * 64
    metadata = VideoSourceMetadata(
        source_id="src_018cc251-f400-7000-8000-000000000003",
        source_revision_id="rev_018cc251-f400-7000-8000-000000000004",
        connector_id="fixture",
        connector_version="1.0.0",
        platform="local",
        canonical_identity_scheme="local-content-sha256",
        stable_video_identity=content_identity,
        canonical_uri=None,
        title="Fixture",
        author="AllToNote",
        channel="Local",
        duration_ms=2_000,
        published_at=None,
        observed_at="2026-07-15T00:00:00.000Z",
        language="en",
        subtitle_acquisition="generated",
        source_link=None,
        materialization_reason="external_local_content",
        license="unknown",
        privacy="personal",
        freshness="point_in_time",
        logical_reference=f"urn:alltonote:local-content:{content_identity}",
        materialization_kind="external_local",
    )
    return VideoAcquisition(
        metadata=metadata,
        subtitle_availability=SubtitleAvailability.NOT_SUPPORTED,
        transcript=None,
        transcript_identity=None,
        transcript_provenance=None,
        stored_media=AttemptStoredAsset(
            relative_locator=f"jobs/{JOB_ID}/attempts/{ACQUIRE_ATTEMPT_ID}/assets/source_media.mp4",
            sha256="sha256:" + "0" * 64,
            byte_length=1,
            role=StoredAssetRole.SOURCE_MEDIA,
        ),
    )


def _checkpoint() -> CheckpointMetadata:
    return CheckpointMetadata(
        checkpoint_id="cp_fixture",
        job_id=JOB_ID,
        step_id="acquire",
        attempt_id=ACQUIRE_ATTEMPT_ID,
        relative_path=f"jobs/{JOB_ID}/checkpoints/cp_fixture.payload",
        schema_id="video-step.v1",
        input_hash="sha256:" + "1" * 64,
        output_hash="sha256:" + "2" * 64,
        byte_length=1,
        metadata_json="{}",
        created_at="2026-07-15T00:00:00.000Z",
    )


def _execution(heartbeat=lambda: None) -> VideoStepExecutionContext:
    return VideoStepExecutionContext(
        job_id=JOB_ID,
        step_id="optional_screenshots",
        attempt_id=SCREENSHOT_ATTEMPT_ID,
        authority=ExecutionAuthority("screenshot-owner", 1),
        heartbeat=heartbeat,
    )


def _adapter(
    tmp_path: Path,
    factory: _ProcessFactory,
    *,
    repository: _Repository | None = None,
    platform_name: str = "posix",
    monotonic=lambda: 0.0,
    sleep=lambda _seconds: None,
    taskkill_factory=lambda *_args, **_kwargs: None,
    killpg_factory=lambda *_args: None,
) -> tuple[object, _Storage]:
    media = tmp_path / "课程 clip & $HOME [raw].mp4"
    media.write_bytes(b"media")
    storage = _Storage(tmp_path / "attempt-storage", media)
    adapter = _adapter_type()(
        storage,
        repository or _Repository(),
        ffmpeg_executable="private-ffmpeg",
        process_factory=factory,
        taskkill_factory=taskkill_factory,
        monotonic=monotonic,
        sleep=sleep,
        killpg_factory=killpg_factory,
        platform_name=platform_name,
        timeout_seconds=1.0,
        termination_timeout_seconds=0.25,
    )
    return adapter, storage


def _extract(adapter: object, execution: VideoStepExecutionContext | None = None) -> tuple[object, ...]:
    return adapter.extract(
        _plan(),
        _transcript(),
        _acquisition(),
        acquisition_checkpoint=_checkpoint(),
        execution=execution or _execution(),
    )


def test_exact_argv_keeps_unicode_spaces_and_metacharacters_opaque(tmp_path: Path) -> None:
    factory = _ProcessFactory()
    adapter, storage = _adapter(tmp_path, factory)

    assets = _extract(adapter)

    assert len(assets) == 1
    plan_item = _plan()[0]
    argv, kwargs = factory.calls[0]
    assert argv == [
        "private-ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "1.250",
        "-i",
        str(storage.media),
        "-frames:v",
        "1",
        "-c:v",
        "libwebp",
        argv[-1],
    ]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert argv[9] == str(storage.media)
    assert Path(argv[-1]).is_relative_to(storage.root)
    assert assets[0].artifact_id == plan_item.artifact_id
    assert assets[0].relative_path == plan_item.relative_path
    assert assets[0].media_type == "image/webp"
    assert assets[0].artifact_type == "evidence.asset.v1"
    assert assets[0].payload == VALID_WEBP
    assert not Path(argv[-1]).exists()
    assert storage.resolve_calls == storage.allocate_calls == storage.read_calls == 1


@pytest.mark.parametrize(
    "payload",
    (
        b"",
        b"RIFF",
        b"RIFF\x0d\x00\x00\x00WEBPVP8L\x00\x00\x00\x00",
        b"NOPE\x0c\x00\x00\x00WEBPVP8L\x00\x00\x00\x00",
        b"RIFF\x0c\x00\x00\x00NOPEVP8L\x00\x00\x00\x00",
        b"RIFF\x0c\x00\x00\x00WEBPJUNK\x00\x00\x00\x00",
    ),
)
def test_malformed_webp_never_returns_asset_or_partial(tmp_path: Path, payload: bytes) -> None:
    factory = _ProcessFactory(payload)
    adapter, _storage = _adapter(tmp_path, factory)

    with pytest.raises(DomainError, match="screenshot_output_invalid"):
        _extract(adapter)

    assert not Path(factory.calls[0][0][-1]).exists()


@pytest.mark.parametrize("failure", ("nonzero", "process", "source_io", "output_io"))
def test_process_and_io_failures_are_stable_private_and_cleanup(
    tmp_path: Path,
    failure: str,
) -> None:
    private = str(tmp_path / "private-secret")
    process = _Process((7,))
    factory = _ProcessFactory(
        process=process,
        error=(OSError(f"spawn failed {private}") if failure == "process" else None),
    )
    adapter, storage = _adapter(tmp_path, factory)
    storage.fail_resolve = failure == "source_io"
    storage.fail_read = failure == "output_io"

    with pytest.raises(DomainError) as caught:
        _extract(adapter)

    assert caught.value.code in {"screenshot_process_failed", "screenshot_io_failed"}
    assert private not in repr(caught.value)
    assert str(storage.media) not in repr(caught.value)
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


@pytest.mark.parametrize(
    ("cancellations", "polls", "expected_starts"),
    (
        ((True,), (0,), 0),
        ((False, True), (None,), 1),
        ((False, True), (0,), 1),
    ),
)
def test_pre_mid_and_post_exit_cancellation_return_no_asset(
    tmp_path: Path,
    cancellations: tuple[bool, ...],
    polls: tuple[int | None, ...],
    expected_starts: int,
) -> None:
    process = _Process(polls)
    factory = _ProcessFactory(process=process)
    adapter, storage = _adapter(
        tmp_path,
        factory,
        repository=_Repository(cancellations),
    )

    with pytest.raises(DomainError) as caught:
        _extract(adapter)

    assert caught.value.code == "job_cancelled"
    assert len(factory.calls) == expected_starts
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


def test_timeout_terminates_windows_tree_with_taskkill_and_fallback(tmp_path: Path) -> None:
    process = _Process((None,))
    process.wait_timeouts = 2
    factory = _ProcessFactory(process=process)
    taskkill_calls: list[tuple[list[str], dict[str, object]]] = []
    times = iter((0.0, 2.0))

    def taskkill(argv: list[str], **kwargs: object) -> None:
        taskkill_calls.append((argv, kwargs))

    adapter, storage = _adapter(
        tmp_path,
        factory,
        platform_name="windows",
        monotonic=lambda: next(times),
        taskkill_factory=taskkill,
    )

    with pytest.raises(DomainError, match="screenshot_process_timeout"):
        _extract(adapter)

    assert taskkill_calls[0][0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


def test_timeout_terminates_owned_posix_process_group(tmp_path: Path) -> None:
    process = _Process((None,))
    process.wait_timeouts = 2
    factory = _ProcessFactory(process=process)
    killpg_calls: list[tuple[int, int]] = []
    times = iter((0.0, 2.0))
    adapter, storage = _adapter(
        tmp_path,
        factory,
        platform_name="posix",
        monotonic=lambda: next(times),
        killpg_factory=lambda pid, sig: killpg_calls.append((pid, sig)),
    )

    with pytest.raises(DomainError, match="screenshot_process_timeout"):
        _extract(adapter)

    assert killpg_calls == [
        (4321, getattr(signal, "SIGTERM", 15)),
        (4321, getattr(signal, "SIGKILL", 9)),
    ]
    assert factory.calls[0][1]["start_new_session"] is True
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


def test_heartbeat_fence_failure_terminates_process_and_sanitizes_error(tmp_path: Path) -> None:
    process = _Process((None,))
    factory = _ProcessFactory(process=process)
    adapter, storage = _adapter(tmp_path, factory, platform_name="windows")
    private = str(storage.root)

    def fenced() -> None:
        raise DomainError(
            "attempt_fenced",
            ErrorCategory.CONFLICT,
            f"fenced {private}",
            {"path": private},
        )

    with pytest.raises(DomainError) as caught:
        _extract(adapter, _execution(fenced))

    assert caught.value.code == "attempt_fenced"
    assert caught.value.message == "Screenshot extraction lost execution authority"
    assert dict(caught.value.details) == {}
    assert private not in repr(caught.value)
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


@pytest.mark.parametrize(
    ("failure", "expected"),
    ((RuntimeError("private poll state"), "screenshot_process_failed"),
     (KeyboardInterrupt(), "KeyboardInterrupt")),
)
def test_postspawn_exception_always_reaps_owned_process(
    tmp_path: Path, failure: BaseException, expected: str
) -> None:
    class ExplodingProcess(_Process):
        def poll(self) -> int | None:
            raise failure

    process = ExplodingProcess((None,))
    factory = _ProcessFactory(process=process)
    adapter, storage = _adapter(tmp_path, factory)

    if expected == "KeyboardInterrupt":
        with pytest.raises(KeyboardInterrupt):
            _extract(adapter)
    else:
        with pytest.raises(DomainError, match=expected):
            _extract(adapter)

    assert process.wait_calls
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


def test_polling_is_responsive_without_heartbeat_per_poll(tmp_path: Path) -> None:
    process = _Process((None,) * 19 + (0,))
    factory = _ProcessFactory(process=process)
    now = -0.05
    heartbeats = 0

    def clock() -> float:
        nonlocal now
        now += 0.05
        return now

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    adapter, _storage = _adapter(tmp_path, factory, monotonic=clock)

    assert len(_extract(adapter, _execution(heartbeat))) == 1
    assert heartbeats == 2


def test_two_item_plan_digests_source_once_and_starts_in_plan_order(tmp_path: Path) -> None:
    transcript = TranscriptDocument(
        language="en",
        segments=(
            TranscriptSegment("seg_000001", 1_000, 2_000, "first"),
            TranscriptSegment("seg_000002", 2_000, 3_000, "second"),
        ),
    )
    draft = GeneratedVideoDraft(
        markdown="# Note\n",
        cited_segment_ids=(),
        screenshot_requests=(
            ScreenshotRequest("seg_000002", 100),
            ScreenshotRequest("seg_000001", 200),
        ),
        model_identity="fixture/model-v1",
        usage={},
        warnings=(),
    )
    plan = build_screenshot_plan(JOB_ID, ScreenshotPolicy.ON_DEMAND, draft, transcript)
    factory = _ProcessFactory()
    adapter, storage = _adapter(tmp_path, factory)

    assets = adapter.extract(
        plan,
        transcript,
        _acquisition(),
        acquisition_checkpoint=_checkpoint(),
        execution=_execution(),
    )

    assert storage.resolve_calls == 1
    assert len(factory.calls) == 2
    assert [call[0][7] for call in factory.calls] == ["2.100", "1.200"]
    assert [asset.artifact_id for asset in assets] == [item.artifact_id for item in plan]


def test_successful_acceptance_fails_when_required_cleanup_fails(tmp_path: Path) -> None:
    factory = _ProcessFactory()
    adapter, storage = _adapter(tmp_path, factory)
    storage.fail_cleanup = True

    with pytest.raises(DomainError) as caught:
        _extract(adapter)

    assert caught.value.code == "screenshot_io_failed"
    assert str(storage.root) not in repr(caught.value)
    assert process_reaped(factory.process)


def test_cleanup_failure_never_masks_primary_process_error(tmp_path: Path) -> None:
    factory = _ProcessFactory(process=_Process((7,)))
    adapter, storage = _adapter(tmp_path, factory)
    storage.fail_cleanup = True

    with pytest.raises(DomainError) as caught:
        _extract(adapter)

    assert caught.value.code == "screenshot_process_failed"


def process_reaped(process: _Process) -> bool:
    return bool(process.wait_calls)


@pytest.mark.parametrize("dependency", ("heartbeat", "monotonic", "sleep"))
def test_ordinary_dependency_failure_is_path_free_and_reaps_process(
    tmp_path: Path, dependency: str
) -> None:
    private = str(tmp_path / "private-dependency")
    process = _Process((None,))
    factory = _ProcessFactory(process=process)

    def fail() -> None:
        raise RuntimeError(f"failure at {private}")

    adapter, storage = _adapter(
        tmp_path,
        factory,
        monotonic=(fail if dependency == "monotonic" else lambda: 0.0),
        sleep=(fail if dependency == "sleep" else lambda _seconds: None),
    )
    execution = _execution(fail if dependency == "heartbeat" else lambda: None)

    with pytest.raises(DomainError) as caught:
        _extract(adapter, execution)

    assert caught.value.code == "screenshot_process_failed"
    assert dict(caught.value.details) == {}
    assert private not in repr(caught.value)
    assert process_reaped(process)
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


def test_scheduler_lease_loss_after_exit_is_sanitized_and_cleans_partial(
    tmp_path: Path,
) -> None:
    process = _Process((0,))
    factory = _ProcessFactory(process=process)
    calls = 0
    private = str(tmp_path / "private-scheduler")

    def heartbeat() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DomainError(
                "scheduler_lease_lost",
                ErrorCategory.CONFLICT,
                f"lost at {private}",
                {"path": private},
            )

    adapter, storage = _adapter(tmp_path, factory)

    with pytest.raises(DomainError) as caught:
        _extract(adapter, _execution(heartbeat))

    assert caught.value.code == "scheduler_lease_lost"
    assert caught.value.category is ErrorCategory.CONFLICT
    assert dict(caught.value.details) == {}
    assert private not in repr(caught.value)
    assert process_reaped(process)
    assert all(not path.exists() for path in storage.root.rglob("*.webp"))


def test_windows_process_uses_new_process_group_flag(tmp_path: Path) -> None:
    factory = _ProcessFactory()
    adapter, _storage = _adapter(tmp_path, factory, platform_name="windows")

    assert len(_extract(adapter)) == 1
    assert factory.calls[0][1]["creationflags"] == getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )


def test_forged_execution_step_causes_zero_process_starts(tmp_path: Path) -> None:
    factory = _ProcessFactory()
    adapter, storage = _adapter(tmp_path, factory)

    with pytest.raises(DomainError, match="screenshot_plan_invalid"):
        _extract(adapter, replace(_execution(), step_id="draft"))

    assert factory.calls == []
    assert storage.resolve_calls == storage.allocate_calls == 0


def test_real_storage_cleans_partial_after_concrete_post_exit_scheduler_loss(
    tmp_path: Path,
) -> None:
    now = [1_000]
    repository = SqliteJobRepository.open(
        tmp_path / "repository", clock=lambda: now[0]
    )
    storage = FileAttemptStorage(tmp_path / "attempts", repository, validators={})
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.claim_job(
        job.job_id,
        "real-owner",
        ttl_seconds=300,
    ).authority
    acquire_attempt = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            "acquire",
            authority=authority,
        ).attempt_id,
        authority,
    )
    source = tmp_path / "private-source.mp4"
    source.write_bytes(b"media")

    class Token:
        def raise_if_cancelled(self) -> None:
            return None

    stored = storage.snapshot_asset(
        source,
        job_id=job.job_id,
        attempt_id=acquire_attempt.attempt_id,
        role=StoredAssetRole.SOURCE_MEDIA,
        expected_sha256=sha256_digest(source.read_bytes()),
        authority=authority,
        token=Token(),
    )
    repository.transition_attempt(
        acquire_attempt.attempt_id, AttemptState.SUCCEEDED, authority=authority
    )
    screenshot_attempt = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            "optional_screenshots",
            authority=authority,
        ).attempt_id,
        authority,
    )
    transcript = _transcript()
    draft = GeneratedVideoDraft(
        markdown="# Note\n",
        cited_segment_ids=(),
        screenshot_requests=(ScreenshotRequest("seg_000001", 250),),
        model_identity="fixture/model-v1",
        usage={},
        warnings=(),
    )
    plan = build_screenshot_plan(
        job.job_id, ScreenshotPolicy.ON_DEMAND, draft, transcript
    )
    process = _Process((0,))
    factory = _ProcessFactory(process=process)
    heartbeat_calls = 0

    def heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 2:
            now[0] = 302_001
        repository.heartbeat_job_claim(authority, ttl_seconds=300)

    adapter = _adapter_type()(
        storage,
        repository,
        ffmpeg_executable="private-ffmpeg",
        process_factory=factory,
        killpg_factory=lambda *_args: None,
        platform_name="posix",
    )
    checkpoint = replace(
        _checkpoint(), job_id=job.job_id, attempt_id=acquire_attempt.attempt_id
    )
    execution = VideoStepExecutionContext(
        job_id=job.job_id,
        step_id="optional_screenshots",
        attempt_id=screenshot_attempt.attempt_id,
        authority=authority,
        heartbeat=heartbeat,
    )

    with pytest.raises(DomainError) as caught:
        adapter.extract(
            plan,
            transcript,
            replace(_acquisition(), stored_media=stored),
            acquisition_checkpoint=checkpoint,
            execution=execution,
        )

    assert caught.value.code == "job_claim_fenced"
    assert caught.value.category is ErrorCategory.CONFLICT
    assert process_reaped(process)
    assert not tuple(storage.root.rglob("*.partial.webp"))
