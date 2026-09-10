"""Owned, serial JSON-lines worker. No detached service or listening socket."""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from app.adapters.worker_process import _WindowsJob, _CREATE_SUSPENDED, _terminate_process_tree
from app.adapters.video_packs.official_pack_process import _unique_object, _result_invalid
from app.core.errors import DomainError, ErrorCategory


logger = logging.getLogger(__name__)


class PersistentTranscribeProcess:
    def __init__(self, *, idle_seconds: float = 60) -> None:
        self._lock = threading.Lock()
        self._process = None
        self._job = None
        self._identity = None
        self._timer = None
        self._idle_seconds = idle_seconds

    def _stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._process is not None:
            _terminate_process_tree(self._process, self._job)
            self._process.stdin.close()
            self._process.stdout.close()
        elif self._job is not None:
            self._job.close()
        self._process = self._job = self._identity = None

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _expire(self) -> None:
        with self._lock:
            if time.monotonic() - self._last_used >= self._idle_seconds:
                self._stop()

    def __call__(self, command, request, *, cwd, environment, timeout_seconds,
                 maximum_output_bytes, check_cancelled=None):
        if (not command or any(type(value) is not str or not value for value in command)
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0 or type(maximum_output_bytes) is not int or maximum_output_bytes < 1):
            raise ValueError("Invalid persistent worker invocation")
        started = time.monotonic()
        deadline = started + timeout_seconds
        payload = json.dumps(dict(request), ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > 64 * 1024 or maximum_output_bytes < 1:
            raise ValueError("Invalid persistent worker request")

        def check():
            if check_cancelled is not None:
                check_cancelled()
            if time.monotonic() >= deadline:
                raise DomainError("pack_worker_timeout", ErrorCategory.RETRYABLE_RUNTIME,
                                  "Transcription exceeded its time budget")

        check()
        while not self._lock.acquire(timeout=0.05):
            check()
        exchange = None
        try:
            check()
            if self._timer is not None:
                self._timer.cancel()
            # Media paths are absolute. A stable cwd allows successive videos
            # in different workspaces to reuse this process and its model.
            stable_cwd = Path(environment["PYTHONPATH"]).resolve(strict=True)
            identity = (tuple(command), str(stable_cwd), tuple(sorted(environment.items())),
                        request["model_path"], request["cpu_threads"])
            warm = self._identity == identity and self._process is not None and self._process.poll() is None
            admitted = time.monotonic()
            if not warm:
                self._stop()
                try:
                    options = {"start_new_session": True}
                    if os.name == "nt":
                        self._job = _WindowsJob()
                        options = {"creationflags": subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED}
                    self._process = subprocess.Popen(
                        (*command, "--persistent"), cwd=stable_cwd, env=dict(environment),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        **options,
                    )
                    if self._job is not None:
                        self._job.assign_and_resume(self._process)
                except OSError as error:
                    raise DomainError("pack_worker_unavailable", ErrorCategory.WORKSPACE_INCOMPATIBLE,
                                      "The transcription worker could not be started") from error
                self._identity = identity
            ready = time.monotonic()
            process = self._process
            responses = queue.Queue(maxsize=1)

            def communicate():
                try:
                    process.stdin.write(payload)
                    process.stdin.flush()
                    responses.put(process.stdout.readline(maximum_output_bytes + 1))
                except (OSError, ValueError) as error:
                    responses.put(error)

            exchange = threading.Thread(target=communicate, daemon=True, name="transcribe-json-exchange")
            exchange.start()
            while True:
                check()
                try:
                    output = responses.get(timeout=0.05)
                    break
                except queue.Empty:
                    continue
            if isinstance(output, BaseException) or not output:
                raise DomainError("pack_worker_failed", ErrorCategory.RECIPE_FAILED,
                                  "The transcription worker exited without a result")
            if len(output) > maximum_output_bytes or not output.endswith(b"\n"):
                raise _result_invalid()
            try:
                result = json.loads(output, object_pairs_hook=_unique_object,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                raise _result_invalid() from None
            if (type(result) is not dict or set(result) != {"result", "timings"}
                    or type(result["result"]) is not dict or type(result["timings"]) is not dict):
                raise _result_invalid()
            check()
            logger.info("video_performance %s", json.dumps({
                "stage": "transcribe_worker", "warm_process": warm,
                "local_wait_seconds": admitted - started, "process_start_seconds": ready - admitted,
                "worker_roundtrip_seconds": time.monotonic() - ready,
                "worker": result["timings"],
            }))
            return result["result"]
        except BaseException:
            self._stop()
            raise
        finally:
            if exchange is not None:
                exchange.join(timeout=5)
            if self._process is not None:
                self._last_used = time.monotonic()
                self._timer = threading.Timer(self._idle_seconds, self._expire)
                self._timer.daemon = True
                self._timer.start()
            self._lock.release()


_worker = PersistentTranscribeProcess()
atexit.register(_worker.close)
run_persistent_transcriber = _worker
