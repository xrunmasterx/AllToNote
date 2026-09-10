from __future__ import annotations

import base64
import atexit

from collections import deque
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Optional

from app.services.codex_app_server import CodexAppServerStatusService
from app.gpt.model_slots import model_call_slot
from app.adapters.worker_process import _CREATE_SUSPENDED, _WindowsJob


class CodexAppServerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        outcome_known: bool = False,
    ) -> None:
        self.code = code
        self.outcome_known = outcome_known
        super().__init__(message)


_CANCELLATION_POLL_SECONDS = 0.05


@dataclass
class CodexTurnState:
    text: str = ""
    done: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_outcome_known: bool = False
    thread_id: str | None = None
    filter_thread: bool = False


@dataclass(eq=False)
class _Connection:
    process: subprocess.Popen[str]
    messages: queue.Queue
    stderr: deque[str]
    readers: tuple[threading.Thread, threading.Thread]
    cwd: str
    windows_job: _WindowsJob | None = None
    initialized: bool = False
    reusable: bool = False
    request_id: int = 0

    def next_id(self) -> int:
        self.request_id += 1
        return self.request_id


class CodexAppServerClient:
    def __init__(self, codex_bin: Optional[str] = None, timeout_seconds: int = 600,
                 *, pool_size: int = 0, machine_slot_root: Path | None = None):
        self.codex_bin = codex_bin or CodexAppServerStatusService.find_codex_bin()
        if not self.codex_bin:
            raise CodexAppServerError(
                "Codex CLI is not installed or not on PATH. Install Codex CLI and sign in before using app-server."
            )
        self.timeout_seconds = timeout_seconds
        self._turn_local = threading.local()
        if type(pool_size) is not int or pool_size < 0:
            raise ValueError("invalid_codex_pool_size")
        self._pool_size = pool_size
        self._machine_slot_root = machine_slot_root
        self._condition = threading.Condition()
        self._idle: list[_Connection] = []
        self._connections: set[_Connection] = set()
        self._waiters: deque[object] = deque()
        self._leased = 0
        self._closed = False
        if pool_size:
            atexit.register(self.close)

    def close(self) -> None:
        """Stop owned transports; other clients and workers are unaffected."""
        with self._condition:
            self._closed = True
            connections = tuple(self._connections)
            self._idle.clear()
            self._condition.notify_all()
        for connection in connections:
            self._close_connection(connection)
        if self._pool_size:
            atexit.unregister(self.close)

    def _close_connection(self, connection: _Connection) -> None:
        with self._condition:
            if connection not in self._connections:
                return
            self._connections.remove(connection)
        if connection.windows_job is not None:
            connection.windows_job.close()
            connection.process.wait(timeout=5)
        else:
            self._terminate_process(connection.process)
        for reader in connection.readers:
            reader.join(timeout=1)
        for stream in (connection.process.stdin, connection.process.stdout, connection.process.stderr):
            if hasattr(stream, "close"):
                stream.close()

    def _start_connection(self, cwd: str) -> _Connection:
        try:
            CodexAppServerStatusService.assert_ready()
        except RuntimeError as exc:
            raise CodexAppServerError(str(exc), outcome_known=True) from exc
        messages: queue.Queue = queue.Queue()
        stderr: deque[str] = deque(maxlen=200)
        # Reuse the worker's kill-on-parent-exit containment: a crashed CLI
        # must not leave model processes running after its admission locks close.
        windows_job = _WindowsJob() if self._is_windows() else None
        process = None
        try:
            process = subprocess.Popen(
                [self.codex_bin, "app-server", "--stdio"], cwd=cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
                **({"creationflags": subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED}
                   if windows_job is not None else {}),
            )
            if windows_job is not None:
                windows_job.assign_and_resume(process)
        except BaseException:
            if windows_job is not None:
                windows_job.close()
            if process is not None:
                self._terminate_process(process)
            raise
        readers = (
            threading.Thread(target=self._read_stdout_messages, args=(process, messages), daemon=True),
            threading.Thread(target=self._read_stderr_logs, args=(process, stderr), daemon=True),
        )
        connection = _Connection(process, messages, stderr, readers, cwd, windows_job)
        with self._condition:
            self._connections.add(connection)
        for reader in readers:
            reader.start()
        return connection

    @contextmanager
    def _connection(self, cwd: str, deadline: float, check_cancelled: Callable[[], None] | None):
        started = time.monotonic()
        timings = {}
        self._turn_local.timings = timings
        ticket = object()
        connection = None
        reserved = False
        try:
            with self._condition:
                self._waiters.append(ticket)
            while not reserved:
                if check_cancelled is not None:
                    check_cancelled()
                with self._condition:
                    if self._closed:
                        raise CodexAppServerError("Codex client is closed", outcome_known=True)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexAppServerError("Timed out waiting for Codex connection capacity", outcome_known=True)
                    if self._waiters[0] is ticket and (not self._pool_size or self._leased < self._pool_size):
                        self._waiters.popleft()
                        self._leased += 1
                        reserved = True
                        if self._idle:
                            connection = self._idle.pop()
                    else:
                        self._condition.wait(timeout=min(remaining, _CANCELLATION_POLL_SECONDS))
            gate = (model_call_slot(self._machine_slot_root, deadline=deadline, check_cancelled=check_cancelled)
                    if self._machine_slot_root is not None else nullcontext())
            locally_admitted = time.monotonic()
            timings["local_wait_seconds"] = locally_admitted - started
            with gate:
                admitted = time.monotonic()
                timings["machine_wait_seconds"] = admitted - locally_admitted
                if connection is not None and (connection.cwd != cwd or connection.process.poll() is not None
                                                or not connection.readers[0].is_alive()):
                    self._close_connection(connection)
                    connection = None
                if connection is None:
                    connection = self._start_connection(cwd)
                ready = time.monotonic()
                timings["process_start_seconds"] = ready - admitted
                connection.reusable = False
                if connection.initialized:
                    connection.stderr.clear()
                try:
                    with self._condition:
                        if self._closed:
                            raise CodexAppServerError("Codex client is closed", outcome_known=True)
                    yield connection
                finally:
                    timings["protocol_seconds"] = time.monotonic() - ready
                    if not connection.reusable or not self._pool_size:
                        self._close_connection(connection)
                    self._turn_local.stderr_logs = tuple(connection.stderr)
        except TimeoutError as error:
            raise CodexAppServerError(str(error), outcome_known=True) from error
        finally:
            with self._condition:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                if reserved:
                    self._leased -= 1
                if connection is not None and connection in self._connections and not self._closed:
                    self._idle.append(connection)
                self._condition.notify_all()

    @property
    def timings(self) -> dict[str, float]:
        """Transport timing, not an estimate of provider-side inference time."""
        return dict(getattr(self._turn_local, "timings", {}))

    @property
    def stderr_logs(self) -> list[str]:
        return list(getattr(self._turn_local, "stderr_logs", ()))

    @staticmethod
    def clean_markdown(text: str) -> str:
        cleaned = text.strip()
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[0].strip().lower() == "```markdown":
            closing_index = next(
                (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "```"),
                None,
            )
            if closing_index == len(lines) - 1:
                cleaned = "\n".join(lines[1:-1]).strip()

        if not cleaned:
            raise CodexAppServerError(
                "Codex app-server returned empty Markdown",
                outcome_known=True,
            )
        return cleaned

    @staticmethod
    def handle_notification(message: dict[str, Any], state: CodexTurnState) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if state.filter_thread and (
            (params.get("threadId") is not None and params["threadId"] != state.thread_id)
            or (method in {"item/agentMessage/delta", "item/completed", "turn/completed"}
                and (state.thread_id is None or params.get("threadId") != state.thread_id))
        ):
            return

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                state.text += delta
            return

        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and not state.text:
                text = item.get("text")
                if isinstance(text, str):
                    state.text = text
            return

        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
            state.done = True
            status = turn.get("status")
            if status == "completed":
                state.error = None
            elif status == "failed":
                error = turn.get("error") or turn
                state.error = CodexAppServerClient._extract_error_message(error)
                state.error_code = CodexAppServerClient._extract_error_code(error)
                state.error_outcome_known = True
            elif status == "interrupted":
                state.error = "Codex app-server turn interrupted"
            else:
                status_name = status if isinstance(status, str) and status else "unknown"
                state.error = f"Codex app-server turn completed with unsupported status: {status_name}"
            return

        if method == "error":
            if params.get("willRetry") is True:
                return
            state.done = True
            error = params.get("error") or params
            state.error = CodexAppServerClient._extract_error_message(error)
            state.error_code = CodexAppServerClient._extract_error_code(error)
            state.error_outcome_known = True

    def run_markdown_turn(
        self,
        prompt: str,
        model: str,
        cwd: Optional[str] = None,
        *,
        timeout_seconds: int | float | None = None,
        output_schema: dict[str, object] | None = None,
        reasoning_effort: str | None = None,
        check_cancelled: Callable[[], None] | None = None,
        image_webp: tuple[bytes, ...] = (),
    ) -> str:
        self._turn_local.stderr_logs = ()
        resolved_timeout = (
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if (
            type(resolved_timeout) not in (int, float)
            or not math.isfinite(resolved_timeout)
            or resolved_timeout <= 0
            or (output_schema is not None and type(output_schema) is not dict)
            or (
                reasoning_effort is not None
                and (
                    type(reasoning_effort) is not str
                    or not reasoning_effort.strip()
                )
            )
        ):
            raise CodexAppServerError("Codex app-server turn options are invalid")
        if check_cancelled is not None:
            check_cancelled()
        deadline = time.monotonic() + resolved_timeout
        with self._connection(cwd or str(Path.cwd()), deadline, check_cancelled) as connection:
            process, stdout_queue = connection.process, connection.messages
            state = CodexTurnState(filter_thread=bool(self._pool_size))
            if not connection.initialized:
                initialize_id = connection.next_id()
                self._send_request(
                    process,
                    initialize_id,
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "alltonote",
                            "title": "AllToNote",
                            "version": "0.0.0",
                        },
                        "capabilities": {
                            "experimentalApi": True,
                            "requestAttestation": False,
                        },
                    },
                )
                self._wait_for_response(stdout_queue, state, initialize_id, deadline, check_cancelled)
                self._send_notification(process, "initialized")
                connection.initialized = True

            thread_params = {
                "model": model,
                "cwd": cwd or str(Path.cwd()),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "baseInstructions": (
                    "You are AllToNote's single-request model backend. "
                    "Return only the exact format requested by the prompt. Do not call tools, "
                    "run commands, inspect files, or modify files."
                ),
            }
            thread_request_id = connection.next_id()
            self._send_request(process, thread_request_id, "thread/start", thread_params)
            thread_response = self._wait_for_response(
                stdout_queue,
                state,
                thread_request_id,
                deadline,
                check_cancelled,
            )
            thread_id = self._extract_thread_id(thread_response)
            if not thread_id:
                raise CodexAppServerError("Codex app-server thread/start response did not include a thread id")
            state.thread_id = thread_id

            turn_params: dict[str, Any] = {
                "input": [{"type": "text", "text": prompt, "text_elements": []}]
                + [{"type": "image", "url": "data:image/webp;base64," + base64.b64encode(value).decode("ascii")}
                   for value in image_webp],
                "approvalPolicy": "never",
                "model": model,
                "threadId": thread_id,
            }
            if output_schema is not None:
                turn_params["outputSchema"] = output_schema
            if reasoning_effort is not None:
                turn_params["effort"] = reasoning_effort
            turn_request_id = connection.next_id()
            self._send_request(process, turn_request_id, "turn/start", turn_params)
            self._wait_for_response(
                stdout_queue,
                state,
                turn_request_id,
                deadline,
                check_cancelled,
            )

            while not state.done:
                self._consume_next_message(
                    stdout_queue,
                    state,
                    deadline,
                    check_cancelled,
                )

            if state.error:
                raise CodexAppServerError(
                    state.error,
                    code=state.error_code,
                    outcome_known=state.error_outcome_known,
                )
            result = self.clean_markdown(state.text)
            if self._pool_size:
                # Unload the completed ephemeral thread before lending the
                # transport to another independent generation/review request.
                try:
                    unsubscribe_id = connection.next_id()
                    self._send_request(process, unsubscribe_id, "thread/unsubscribe", {"threadId": thread_id})
                    self._wait_for_response(stdout_queue, state, unsubscribe_id,
                                            min(deadline, time.monotonic() + 5), check_cancelled)
                    connection.reusable = True
                except (CodexAppServerError, OSError):
                    # A completed answer remains valid; discard the transport.
                    connection.reusable = False
            return result

    def _send_request(
        self,
        process: subprocess.Popen[str],
        request_id: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if process.stdin is None:
            raise CodexAppServerError("Codex app-server stdin is unavailable")

        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    @staticmethod
    def _send_notification(
        process: subprocess.Popen[str],
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        if process.stdin is None:
            raise CodexAppServerError("Codex app-server stdin is unavailable")

        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _wait_for_response(
        self,
        stdout_queue: queue.Queue[dict[str, Any] | Exception],
        state: CodexTurnState,
        request_id: int,
        deadline: float,
        check_cancelled: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        while True:
            message = self._consume_next_message(
                stdout_queue,
                state,
                deadline,
                check_cancelled,
            )
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise CodexAppServerError(
                    self._extract_error_message(error),
                    code=self._extract_error_code(error),
                    outcome_known=True,
                )
            return message

    def _consume_next_message(
        self,
        stdout_queue: queue.Queue[dict[str, Any] | Exception],
        state: CodexTurnState,
        deadline: float,
        check_cancelled: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        while True:
            if check_cancelled is not None:
                check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerError("Timed out waiting for Codex app-server")
            wait_seconds = (
                min(remaining, _CANCELLATION_POLL_SECONDS)
                if check_cancelled is not None
                else remaining
            )
            try:
                message = stdout_queue.get(timeout=wait_seconds)
                break
            except queue.Empty:
                continue

        if isinstance(message, Exception):
            raise CodexAppServerError(str(message)) from message

        if "method" in message:
            method = message.get("method")
            if "id" in message:
                raise CodexAppServerError(f"Unsupported Codex app-server request: {method}")
            self.handle_notification(message, state)
        return message

    @staticmethod
    def _read_stdout_messages(
        process: subprocess.Popen[str],
        stdout_queue: queue.Queue[dict[str, Any] | Exception],
    ) -> None:
        if process.stdout is None:
            stdout_queue.put(CodexAppServerError("Codex app-server stdout is unavailable"))
            return

        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError as exc:
                stdout_queue.put(CodexAppServerError(f"Invalid Codex app-server JSON-RPC message: {exc}"))
                return
            if isinstance(message, dict):
                stdout_queue.put(message)
            else:
                stdout_queue.put(CodexAppServerError("Codex app-server JSON-RPC message must be an object"))
                return
        stdout_queue.put(CodexAppServerError("Codex app-server stdout closed"))

    @staticmethod
    def _read_stderr_logs(
        process: subprocess.Popen[str],
        stderr_logs: list[str] | deque[str],
    ) -> None:
        if process.stderr is None:
            return

        for line in process.stderr:
            stderr_logs.append(line.rstrip())

    @staticmethod
    def _extract_error_message(value: Any) -> str:
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
            error = value.get("error")
            if isinstance(error, str) and error:
                return error
            if isinstance(error, dict):
                return CodexAppServerClient._extract_error_message(error)
        if isinstance(value, str) and value:
            return value
        return "Codex app-server turn failed"

    @staticmethod
    def _extract_error_code(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            code = value.get("code")
            if isinstance(code, str) and code:
                return code
            for key in ("error", "message"):
                nested = CodexAppServerClient._extract_error_code(value.get(key))
                if nested:
                    return nested
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    return None
                return CodexAppServerClient._extract_error_code(parsed)
        return None

    @staticmethod
    def _extract_thread_id(response: dict[str, Any]) -> Optional[str]:
        result = response.get("result")
        if not isinstance(result, dict):
            return None

        thread = result.get("thread")
        if not isinstance(thread, dict):
            return None

        thread_id = thread.get("id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id

        return None

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return

        if CodexAppServerClient._is_windows():
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except (OSError, subprocess.CalledProcessError):
                pass

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _is_windows() -> bool:
        return os.name == "nt"
