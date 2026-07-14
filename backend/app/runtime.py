from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.core.application.video_service import (
    CHECKPOINT_SCHEMA,
    VideoPreflightCapabilities,
    VideoRecipeOperations,
    VideoService,
)
from app.core.domain.video import (
    GeneratedVideoDraft,
    JobSnapshot,
    ScreenshotPolicy,
    TranscriptDocument,
    TranscriptSegment,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import CheckpointMetadata
from app.core.portable.bundle_assembler import DisplayAssetInput, VideoSourceMetadata
from app.core.sdk import AllToNoteSDK
from iwiki.workspace import open_workspace


@dataclass
class FakeCallCounts:
    download: int = 0
    transcribe: int = 0
    model: int = 0
    ffmpeg: int = 0
    commit: int = 0


class _FakeVideoOperations(VideoRecipeOperations):
    def __init__(
        self,
        calls: FakeCallCounts,
        *,
        capabilities: VideoPreflightCapabilities,
        quality_fail: bool,
        crash_after_commit_once: bool,
        crash_operation_once: str | None,
        call_log_path: Path | None,
        operation_hooks: Mapping[str, Callable[[Callable[[], None]], None]],
    ) -> None:
        self._calls = calls
        self._capabilities = capabilities
        self._quality_fail = quality_fail
        self._crash_after_commit_once = crash_after_commit_once
        self._crash_operation_once = crash_operation_once
        self._call_log_path = call_log_path
        self._operation_hooks = dict(operation_hooks)

    def preflight_capabilities(
        self, request: VideoProduceRequest
    ) -> VideoPreflightCapabilities:
        del request
        return self._capabilities

    def _record(self, operation: str) -> None:
        if self._call_log_path is None:
            return
        with self._call_log_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"operation": operation}, separators=(",", ":")))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _crash_if_requested(self, operation: str) -> None:
        if self._crash_operation_once == operation:
            self._crash_operation_once = None
            raise RuntimeError(f"injected {operation} crash")

    def _run_operation_hook(
        self,
        operation: str,
        heartbeat: Callable[[], None],
    ) -> None:
        hook = self._operation_hooks.get(operation)
        if hook is not None:
            hook(heartbeat)

    def resolve_source(
        self,
        request: VideoProduceRequest,
        *,
        source_id: str,
        source_revision_id: str,
    ) -> VideoSourceMetadata:
        stable_identity = request.input_value.removeprefix("fixture://")
        if not stable_identity:
            stable_identity = "course"
        return VideoSourceMetadata(
            source_id=source_id,
            source_revision_id=source_revision_id,
            connector_id="fixture",
            connector_version="1.0.0",
            platform="fixture",
            canonical_identity_scheme="fixture-video",
            stable_video_identity=stable_identity,
            canonical_uri=f"https://fixtures.alltonote.invalid/{stable_identity}",
            title="AllToNote Fixture Course",
            author="AllToNote",
            channel="AllToNote",
            duration_ms=2_000,
            published_at="2026-07-14T00:00:00.000Z",
            observed_at="2026-07-14T00:00:00.000Z",
            language="zh-CN",
            subtitle_acquisition="generated",
            source_link=f"https://fixtures.alltonote.invalid/{stable_identity}",
            materialization_reason="remote_video_reference",
            license="unknown",
            privacy="personal",
            freshness="point_in_time",
        )

    def acquire(
        self,
        source: VideoSourceMetadata,
        *,
        external: bool,
        heartbeat: Callable[[], None],
    ) -> object:
        if external:
            self._calls.download += 1
            self._record("download")
            self._crash_if_requested("download")
            self._run_operation_hook("download", heartbeat)
        return source.stable_video_identity

    def transcribe(
        self,
        request: VideoProduceRequest,
        acquired: object,
        *,
        external: bool,
        heartbeat: Callable[[], None],
    ) -> TranscriptDocument:
        del request, acquired
        if external:
            self._calls.transcribe += 1
            self._record("transcribe")
            self._crash_if_requested("transcribe")
            self._run_operation_hook("transcribe", heartbeat)
        return TranscriptDocument(
            language="zh-CN",
            segments=(
                TranscriptSegment(
                    segment_id="seg_000001",
                    start_ms=0,
                    end_ms=2_000,
                    text="知识生产工具应把来源整理为可验证的开放笔记。",
                ),
            ),
        )

    def generate_draft(
        self,
        request: VideoProduceRequest,
        transcript: TranscriptDocument,
        evidence_ids: dict[str, str],
        *,
        external: bool,
        heartbeat: Callable[[], None],
    ) -> GeneratedVideoDraft:
        del request, transcript
        if external:
            self._calls.model += 1
            self._record("model")
            self._crash_if_requested("model")
            self._run_operation_hook("model", heartbeat)
        evidence_id = evidence_ids["seg_000001"]
        if self._quality_fail:
            markdown = "# 视频笔记\n\nTODO：补充证据引用。\n"
            cited_segment_ids: tuple[str, ...] = ()
        else:
            markdown = (
                "# 视频笔记\n\n"
                f"知识生产应保留可验证证据。[^{evidence_id}]\n\n"
                f"[^{evidence_id}]: 视频 00:00–00:02\n"
            )
            cited_segment_ids = ("seg_000001",)
        return GeneratedVideoDraft(
            markdown=markdown,
            cited_segment_ids=cited_segment_ids,
            screenshot_requests=(),
            model_identity="fake/model-v1",
            usage={"input_tokens": 12, "output_tokens": 6},
            warnings=(),
        )

    def screenshots(
        self,
        request: VideoProduceRequest,
        draft: GeneratedVideoDraft,
        *,
        external: bool,
        heartbeat: Callable[[], None],
    ) -> tuple[DisplayAssetInput, ...]:
        del draft
        self._crash_if_requested("screenshots")
        if external and request.screenshot_policy is ScreenshotPolicy.ON_DEMAND:
            self._calls.ffmpeg += 1
            self._record("ffmpeg")
            self._run_operation_hook("ffmpeg", heartbeat)
        return ()

    def after_portable_commit(self, result: object) -> None:
        if not bool(getattr(result, "idempotent", False)):
            self._calls.commit += 1
            self._record("portable_commit")
        if self._crash_after_commit_once:
            self._crash_after_commit_once = False
            raise RuntimeError("injected crash after portable rename")


class AllToNoteRuntime:
    def __init__(
        self,
        sdk: AllToNoteSDK,
        job_repository: SqliteJobRepository,
    ) -> None:
        self._sdk = sdk
        self.job_repository = job_repository

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        return self._sdk.submit_video(request)

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        return self._sdk.wait_job(job_id, event_sink)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._sdk.get_job(job_id)


def _checkpoint_payload_is_valid(payload: bytes) -> bool:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return type(value) is dict and type(value.get("step")) is str


def _read_checkpoint(
    storage: FileAttemptStorage,
    metadata: CheckpointMetadata,
) -> bytes:
    relative = Path(metadata.relative_path)
    expected = (
        Path("jobs")
        / metadata.job_id
        / "checkpoints"
        / f"{metadata.checkpoint_id}.payload"
    )
    if relative.parts != expected.parts:
        raise DomainError(
            "checkpoint_content_invalid",
            ErrorCategory.INTERNAL,
            "Candidate checkpoint path is invalid",
        )
    return (storage.root / relative).read_bytes()


def create_fake_runtime(
    machine_root: Path,
    *,
    calls: FakeCallCounts | None = None,
    capabilities: VideoPreflightCapabilities | None = None,
    quality_fail: bool = False,
    crash_after_commit_once: bool = False,
    crash_operation_once: str | None = None,
    call_log_path: Path | None = None,
    owner_id: str | None = None,
    local_instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    operation_hooks: Mapping[str, Callable[[Callable[[], None]], None]] | None = None,
) -> AllToNoteRuntime:
    call_counts = calls or FakeCallCounts()
    resolved_machine_root = Path(machine_root).resolve()
    resolved_machine_root.mkdir(parents=True, exist_ok=True)
    resolved_call_log = Path(call_log_path).resolve() if call_log_path is not None else None
    if resolved_call_log is not None:
        resolved_call_log.parent.mkdir(parents=True, exist_ok=True)
    repository = SqliteJobRepository.open(
        resolved_machine_root / "job-store", clock=clock
    )
    storage = FileAttemptStorage(
        resolved_machine_root / "attempts",
        repository,
        validators={CHECKPOINT_SCHEMA: _checkpoint_payload_is_valid},
    )
    operations = _FakeVideoOperations(
        call_counts,
        capabilities=capabilities or VideoPreflightCapabilities(),
        quality_fail=quality_fail,
        crash_after_commit_once=crash_after_commit_once,
        crash_operation_once=crash_operation_once,
        call_log_path=resolved_call_log,
        operation_hooks=operation_hooks or {},
    )
    service = VideoService(
        repository,
        storage,
        IWikiPortableGateway(),
        operations,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id=owner_id or f"runtime-{uuid4().hex}",
        work_root=storage.root,
        local_instance_id=local_instance_id
        or hashlib.sha256(str(resolved_machine_root).encode("utf-8")).hexdigest()[:32],
    )
    return AllToNoteRuntime(AllToNoteSDK(service), repository)


def create_fake_runtime_for_workspace(
    workspace_root: Path,
    *,
    local_app_data: Path | None = None,
) -> AllToNoteRuntime:
    trusted_root = local_app_data or _default_local_app_data()
    trusted_root.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceInstanceRegistry(
        trusted_root,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace_root)
    return create_fake_runtime(
        instance.machine_root,
        local_instance_id=instance.instance_id,
    )


def _default_local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".local" / "share"


__all__ = [
    "AllToNoteRuntime",
    "FakeCallCounts",
    "VideoPreflightCapabilities",
    "create_fake_runtime",
    "create_fake_runtime_for_workspace",
]
