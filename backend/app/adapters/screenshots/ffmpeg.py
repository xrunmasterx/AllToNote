from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from app.core.application.video_acquisition import VideoAcquisition
from app.core.application.video_service import (
    VideoStepExecutionContext,
)
from app.core.domain.video import ScreenshotPlanItem, TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.cancellation import CancellationToken
from app.core.jobs.model import CheckpointMetadata
from app.core.portable.bundle_assembler import DisplayAssetInput
from app.core.portable.webp import is_valid_webp
from app.core.ports.jobs import (
    AttemptStoragePort,
    ScreenshotOutputCapability,
    ScreenshotSourceCapability,
)


_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)


class _Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def _error(code: str, category: ErrorCategory, message: str) -> DomainError:
    return DomainError(code, category, message)


def _default_killpg(pid: int, sig: int) -> None:
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise OSError("process groups are unavailable")
    killpg(pid, sig)


class _CancellationRepository(Protocol):
    def is_cancellation_requested(self, job_id: str) -> bool: ...


class FFmpegScreenshotAdapter:
    def __init__(
        self,
        storage: AttemptStoragePort,
        repository: _CancellationRepository,
        *,
        ffmpeg_executable: str = "ffmpeg",
        process_factory: Callable[..., _Process] = subprocess.Popen,
        taskkill_factory: Callable[..., object] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        killpg_factory: Callable[[int, int], None] = _default_killpg,
        platform_name: str | None = None,
        timeout_seconds: float = 30.0,
        termination_timeout_seconds: float = 2.0,
    ) -> None:
        self._storage = storage
        self._repository = repository
        self._ffmpeg_executable = ffmpeg_executable
        self._process_factory = process_factory
        self._taskkill_factory = taskkill_factory
        self._monotonic = monotonic
        self._sleep = sleep
        self._killpg_factory = killpg_factory
        self._platform_name = platform_name or ("windows" if os.name == "nt" else "posix")
        self._timeout_seconds = timeout_seconds
        self._termination_timeout_seconds = termination_timeout_seconds

    def extract(
        self,
        plan: tuple[ScreenshotPlanItem, ...],
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> tuple[DisplayAssetInput, ...]:
        plan = tuple(plan)
        if not plan:
            return ()
        self._validate_boundary(
            plan, transcript, acquired, acquisition_checkpoint, execution
        )
        token = CancellationToken(self._repository, execution.job_id)
        token.raise_if_cancelled()
        stored = acquired.stored_media
        assert stored is not None
        try:
            source = self._storage.verify_screenshot_source(
                stored,
                expected_job_id=acquisition_checkpoint.job_id,
                expected_attempt_id=acquisition_checkpoint.attempt_id,
            )
        except DomainError as error:
            self._raise_storage_error(error)
        except Exception:
            raise _error(
                "screenshot_io_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Screenshot media could not be read",
            ) from None
        assets = []
        for item in plan:
            assets.append(
                self._extract_one(
                    item,
                    execution,
                    token,
                    source,
                )
            )
        return tuple(assets)

    @staticmethod
    def _validate_boundary(
        plan: tuple[ScreenshotPlanItem, ...],
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> None:
        if (
            not isinstance(transcript, TranscriptDocument)
            or not isinstance(acquired, VideoAcquisition)
            or not isinstance(acquisition_checkpoint, CheckpointMetadata)
            or not isinstance(execution, VideoStepExecutionContext)
            or acquired.stored_media is None
            or acquisition_checkpoint.job_id != execution.job_id
            or acquisition_checkpoint.step_id != "acquire"
            or execution.step_id != "optional_screenshots"
        ):
            raise _error(
                "screenshot_plan_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Screenshot plan context is invalid",
            )
        segments = {segment.segment_id: segment for segment in transcript.segments}
        artifact_ids: set[str] = set()
        paths: set[str] = set()
        for ordinal, item in enumerate(plan):
            segment = segments.get(item.segment_id) if isinstance(item, ScreenshotPlanItem) else None
            if (
                segment is None
                or item.ordinal != ordinal
                or item.segment_start_ms != segment.start_ms
                or item.segment_end_ms != segment.end_ms
                or not segment.start_ms <= item.timestamp_ms < segment.end_ms
                or item.artifact_id in artifact_ids
                or item.relative_path in paths
            ):
                raise _error(
                    "screenshot_plan_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "Screenshot plan context is invalid",
                )
            artifact_ids.add(item.artifact_id)
            paths.add(item.relative_path)

    def _extract_one(
        self,
        item: ScreenshotPlanItem,
        execution: VideoStepExecutionContext,
        token: CancellationToken,
        source: ScreenshotSourceCapability,
    ) -> DisplayAssetInput:
        try:
            output = self._storage.allocate_screenshot_output(
                job_id=execution.job_id,
                attempt_id=execution.attempt_id,
                artifact_id=item.artifact_id,
                authority=execution.authority,
            )
        except DomainError as error:
            self._raise_storage_error(error)
        except Exception:
            raise _error(
                "screenshot_io_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Screenshot output could not be allocated",
            ) from None

        process: _Process | None = None
        reaped = False
        try:
            try:
                media_path = self._storage.validate_screenshot_source(source)
                output_path = self._storage.validate_screenshot_output(
                    output, authority=execution.authority
                )
            except DomainError as error:
                self._raise_storage_error(error)
            except Exception:
                raise _error(
                    "screenshot_io_failed",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Screenshot private storage operation failed",
                ) from None
            process = self._start_process(item.timestamp_ms, media_path, output_path)
            return_code = self._wait_for_process(process, execution, token)
            try:
                process.wait(timeout=self._termination_timeout_seconds)
                reaped = True
            except Exception:
                raise _error(
                    "screenshot_process_failed",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Screenshot extraction process failed",
                ) from None
            if return_code != 0:
                raise _error(
                    "screenshot_process_failed",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Screenshot extraction process failed",
                )
            try:
                payload = self._storage.read_screenshot_output(
                    output,
                    job_id=execution.job_id,
                    attempt_id=execution.attempt_id,
                    artifact_id=item.artifact_id,
                    authority=execution.authority,
                )
            except DomainError as error:
                self._raise_storage_error(error)
            except Exception:
                raise _error(
                    "screenshot_io_failed",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Screenshot output could not be read",
                ) from None
            if not is_valid_webp(payload):
                raise _error(
                    "screenshot_output_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "Screenshot output is not a valid WebP image",
                )
            asset = DisplayAssetInput(
                artifact_id=item.artifact_id,
                relative_path=item.relative_path,
                media_type="image/webp",
                payload=bytes(payload),
            )
        except BaseException:
            if process is not None and not reaped:
                self._terminate_tree(process)
            self._cleanup_output(output, execution, suppress=True)
            raise
        self._cleanup_output(output, execution, suppress=False)
        return asset

    def _cleanup_output(
        self,
        output: ScreenshotOutputCapability,
        execution: VideoStepExecutionContext,
        *,
        suppress: bool,
    ) -> None:
        try:
            self._storage.cleanup_screenshot_output(
                output, authority=execution.authority
            )
        except BaseException as error:
            if suppress:
                return
            if isinstance(error, DomainError):
                self._raise_storage_error(error)
            if isinstance(error, Exception):
                raise _error(
                    "screenshot_io_failed",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Screenshot private output cleanup failed",
                ) from None
            raise

    def _start_process(
        self,
        timestamp_ms: int,
        media_path: Path,
        output_path: Path,
    ) -> _Process:
        seconds = f"{timestamp_ms // 1_000}.{timestamp_ms % 1_000:03d}"
        argv = [
            self._ffmpeg_executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            seconds,
            "-i",
            str(media_path),
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            str(output_path),
        ]
        options: dict[str, object] = {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if self._platform_name == "windows":
            options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
            )
        else:
            options["start_new_session"] = True
        try:
            return self._process_factory(argv, **options)
        except Exception:
            raise _error(
                "screenshot_process_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Screenshot extraction process could not start",
            ) from None

    def _wait_for_process(
        self,
        process: _Process,
        execution: VideoStepExecutionContext,
        token: CancellationToken,
    ) -> int:
        try:
            started_at = self._monotonic()
            deadline = started_at + self._timeout_seconds
            next_heartbeat = started_at
        except Exception:
            raise self._process_interrupted() from None
        while True:
            try:
                token.raise_if_cancelled()
                now = self._monotonic()
                if now >= next_heartbeat:
                    self._heartbeat(execution)
                    next_heartbeat = now + 1.0
                return_code = process.poll()
            except DomainError as error:
                self._raise_execution_error(error)
            except Exception:
                raise self._process_interrupted() from None
            if return_code is not None:
                try:
                    token.raise_if_cancelled()
                    self._heartbeat(execution)
                except DomainError as error:
                    self._raise_execution_error(error)
                except Exception:
                    raise self._process_interrupted() from None
                return return_code
            if now >= deadline:
                raise _error(
                    "screenshot_process_timeout",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Screenshot extraction process timed out",
                )
            try:
                self._sleep(0.05)
            except Exception:
                raise self._process_interrupted() from None

    @staticmethod
    def _heartbeat(execution: VideoStepExecutionContext) -> None:
        execution.heartbeat()

    @staticmethod
    def _process_interrupted() -> DomainError:
        return _error(
            "screenshot_process_failed",
            ErrorCategory.RETRYABLE_RUNTIME,
            "Screenshot extraction process was interrupted",
        )

    @staticmethod
    def _raise_execution_error(error: DomainError) -> None:
        if error.category is ErrorCategory.CANCELLED:
            raise _error(error.code, error.category, "Screenshot extraction was cancelled") from None
        if error.code in {
            "attempt_fenced",
            "job_claim_fenced",
            "scheduler_lease_lost",
        }:
            raise _error(
                error.code,
                ErrorCategory.CONFLICT,
                "Screenshot extraction lost execution authority",
            ) from None
        raise FFmpegScreenshotAdapter._process_interrupted() from None

    def _terminate_tree(self, process: _Process) -> None:
        if self._platform_name == "windows":
            try:
                self._taskkill_factory(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    shell=False,
                    check=False,
                    timeout=self._termination_timeout_seconds,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except BaseException:
                pass
            try:
                process.wait(timeout=self._termination_timeout_seconds)
                return
            except BaseException:
                pass
            try:
                process.terminate()
                process.wait(timeout=self._termination_timeout_seconds)
                return
            except BaseException:
                pass
            try:
                process.kill()
                process.wait(timeout=self._termination_timeout_seconds)
            except BaseException:
                pass
            return

        try:
            self._killpg_factory(process.pid, _SIGTERM)
        except BaseException:
            pass
        try:
            process.wait(timeout=self._termination_timeout_seconds)
            return
        except BaseException:
            pass
        try:
            self._killpg_factory(process.pid, _SIGKILL)
        except BaseException:
            pass
        try:
            process.wait(timeout=self._termination_timeout_seconds)
        except BaseException:
            pass

    @staticmethod
    def _raise_storage_error(error: DomainError) -> None:
        if error.code in {
            "attempt_fenced",
            "job_claim_fenced",
            "scheduler_lease_lost",
        }:
            raise _error(
                error.code,
                ErrorCategory.CONFLICT,
                "Screenshot extraction lost execution authority",
            ) from None
        if error.category is ErrorCategory.CANCELLED:
            raise _error(error.code, error.category, "Screenshot extraction was cancelled") from None
        raise _error(
            "screenshot_io_failed",
            ErrorCategory.RETRYABLE_RUNTIME,
            "Screenshot private storage operation failed",
        ) from None


__all__ = ["FFmpegScreenshotAdapter"]
