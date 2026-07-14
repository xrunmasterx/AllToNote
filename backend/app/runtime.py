from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application.video_service import (
    CHECKPOINT_SCHEMA,
    PREFLIGHT_CHECKS,
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
        preflight_failure: str | None,
        quality_fail: bool,
        crash_after_commit_once: bool,
    ) -> None:
        self._calls = calls
        self._preflight_failure = preflight_failure
        self._quality_fail = quality_fail
        self._crash_after_commit_once = crash_after_commit_once

    def preflight(self, request: VideoProduceRequest) -> None:
        del request
        for check in PREFLIGHT_CHECKS:
            if check == self._preflight_failure:
                raise DomainError(
                    f"preflight_{check}_failed",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Video preflight check failed",
                    {"check": check},
                )

    def resolve_source(
        self,
        request: VideoProduceRequest,
        *,
        source_id: str,
        source_revision_id: str,
    ) -> VideoSourceMetadata:
        return VideoSourceMetadata(
            source_id=source_id,
            source_revision_id=source_revision_id,
            connector_id="fixture",
            connector_version="1.0.0",
            platform="fixture",
            canonical_identity_scheme="fixture-video",
            stable_video_identity="course",
            canonical_uri="https://fixtures.alltonote.invalid/course",
            title="AllToNote Fixture Course",
            author="AllToNote",
            channel="AllToNote",
            duration_ms=2_000,
            published_at="2026-07-14T00:00:00.000Z",
            observed_at="2026-07-14T00:00:00.000Z",
            language="zh-CN",
            subtitle_acquisition="generated",
            source_link="https://fixtures.alltonote.invalid/course",
            materialization_reason="remote_video_reference",
            license="unknown",
            privacy="personal",
            freshness="point_in_time",
        )

    def acquire(self, source: VideoSourceMetadata, *, external: bool) -> object:
        if external:
            self._calls.download += 1
        return source.stable_video_identity

    def transcribe(
        self,
        request: VideoProduceRequest,
        acquired: object,
        *,
        external: bool,
    ) -> TranscriptDocument:
        del request, acquired
        if external:
            self._calls.transcribe += 1
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
    ) -> GeneratedVideoDraft:
        del request, transcript
        if external:
            self._calls.model += 1
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
    ) -> tuple[DisplayAssetInput, ...]:
        del draft
        if external and request.screenshot_policy is ScreenshotPolicy.ON_DEMAND:
            self._calls.ffmpeg += 1
        return ()

    def after_portable_commit(self, result: object) -> None:
        if not bool(getattr(result, "idempotent", False)):
            self._calls.commit += 1
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
    preflight_failure: str | None = None,
    quality_fail: bool = False,
    crash_after_commit_once: bool = False,
) -> AllToNoteRuntime:
    call_counts = calls or FakeCallCounts()
    repository = SqliteJobRepository.open(Path(machine_root) / "job-store")
    storage = FileAttemptStorage(
        Path(machine_root) / "attempts",
        repository,
        validators={CHECKPOINT_SCHEMA: _checkpoint_payload_is_valid},
    )
    operations = _FakeVideoOperations(
        call_counts,
        preflight_failure=preflight_failure,
        quality_fail=quality_fail,
        crash_after_commit_once=crash_after_commit_once,
    )
    service = VideoService(
        repository,
        storage,
        IWikiPortableGateway(),
        operations,
        checkpoint_reader=lambda metadata: _read_checkpoint(storage, metadata),
        owner_id="fake-runtime",
    )
    return AllToNoteRuntime(AllToNoteSDK(service), repository)


__all__ = [
    "AllToNoteRuntime",
    "FakeCallCounts",
    "create_fake_runtime",
]
