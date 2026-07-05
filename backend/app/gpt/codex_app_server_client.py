from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Optional

from app.services.codex_app_server import CodexAppServerStatusService


class CodexAppServerError(RuntimeError):
    pass


@dataclass
class CodexTurnState:
    text: str = ""
    done: bool = False
    error: Optional[str] = None


class CodexAppServerClient:
    def __init__(self, codex_bin: Optional[str] = None, timeout_seconds: int = 600):
        self.codex_bin = codex_bin or CodexAppServerStatusService.find_codex_bin()
        if not self.codex_bin:
            raise CodexAppServerError(
                "Codex CLI is not installed or not on PATH. Install Codex CLI and sign in before using app-server."
            )
        self.timeout_seconds = timeout_seconds
        self.stderr_logs: list[str] = []
        self._next_id = 1

    @staticmethod
    def clean_markdown(text: str) -> str:
        cleaned = text.strip()
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[0].strip().lower() in ("```", "```markdown"):
            closing_index = next(
                (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "```"),
                None,
            )
            if closing_index == len(lines) - 1:
                cleaned = "\n".join(lines[1:-1]).strip()

        if not cleaned:
            raise CodexAppServerError("Codex app-server returned empty Markdown")
        return cleaned

    @staticmethod
    def handle_notification(message: dict[str, Any], state: CodexTurnState) -> None:
        method = message.get("method")
        params = message.get("params") or {}

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
            if turn.get("status") == "failed":
                state.error = CodexAppServerClient._extract_error_message(turn.get("error") or turn)
            return

        if method == "error":
            state.done = True
            state.error = CodexAppServerClient._extract_error_message(params.get("error") or params)

    def run_markdown_turn(self, prompt: str, model: str, cwd: Optional[str] = None) -> str:
        try:
            CodexAppServerStatusService.assert_ready()
        except RuntimeError as exc:
            raise CodexAppServerError(str(exc)) from exc

        stdout_queue: queue.Queue[dict[str, Any] | Exception] = queue.Queue()
        self.stderr_logs = []
        process = subprocess.Popen(
            [self.codex_bin, "app-server", "--stdio"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        stdout_thread = threading.Thread(
            target=self._read_stdout_messages,
            args=(process, stdout_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr_logs,
            args=(process,),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        state = CodexTurnState()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            self._send_request(
                process,
                "initialize",
                {
                    "clientInfo": {
                        "name": "bilinote",
                        "title": "BiliNote",
                        "version": "0.0.0",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            )
            self._wait_for_response(stdout_queue, state, self._next_id - 1, deadline)

            thread_params = {
                "model": model,
                "cwd": cwd or str(Path.cwd()),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "baseInstructions": (
                    "You are BiliNote's Markdown note generation backend. "
                    "Only output the requested Markdown. Do not call tools, "
                    "run commands, inspect files, or modify files."
                ),
            }
            self._send_request(process, "thread/start", thread_params)
            thread_response = self._wait_for_response(stdout_queue, state, self._next_id - 1, deadline)
            thread_id = self._extract_thread_id(thread_response)
            if not thread_id:
                raise CodexAppServerError("Codex app-server thread/start response did not include a thread id")

            turn_params: dict[str, Any] = {
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "approvalPolicy": "never",
                "model": model,
                "threadId": thread_id,
            }
            self._send_request(process, "turn/start", turn_params)
            self._wait_for_response(stdout_queue, state, self._next_id - 1, deadline)

            while not state.done:
                self._consume_next_message(stdout_queue, state, deadline)

            if state.error:
                raise CodexAppServerError(state.error)
            return self.clean_markdown(state.text)
        finally:
            self._terminate_process(process)

    def _send_request(
        self,
        process: subprocess.Popen[str],
        method: str,
        params: dict[str, Any],
    ) -> None:
        if process.stdin is None:
            raise CodexAppServerError("Codex app-server stdin is unavailable")

        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self._next_id += 1
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _wait_for_response(
        self,
        stdout_queue: queue.Queue[dict[str, Any] | Exception],
        state: CodexTurnState,
        request_id: int,
        deadline: float,
    ) -> dict[str, Any]:
        while True:
            message = self._consume_next_message(stdout_queue, state, deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CodexAppServerError(self._extract_error_message(message["error"]))
            return message

    def _consume_next_message(
        self,
        stdout_queue: queue.Queue[dict[str, Any] | Exception],
        state: CodexTurnState,
        deadline: float,
    ) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexAppServerError("Timed out waiting for Codex app-server")

        try:
            message = stdout_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise CodexAppServerError("Timed out waiting for Codex app-server") from exc

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

    def _read_stderr_logs(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return

        for line in process.stderr:
            self.stderr_logs.append(line.rstrip())

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
