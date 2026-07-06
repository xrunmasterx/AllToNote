from pathlib import Path
import json
import queue
import subprocess
import sys
import time

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.gpt.codex_app_server_client import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexTurnState,
)


def test_handle_agent_message_delta_accumulates_text():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {"method": "item/agentMessage/delta", "params": {"delta": "# Title"}},
        state,
    )
    CodexAppServerClient.handle_notification(
        {"method": "item/agentMessage/delta", "params": {"delta": "\nBody"}},
        state,
    )

    assert state.text == "# Title\nBody"


def test_handle_completed_agent_message_uses_fallback_when_no_delta():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "# Fallback"}},
        },
        state,
    )

    assert state.text == "# Fallback"


def test_handle_completed_agent_message_does_not_duplicate_existing_delta():
    state = CodexTurnState(text="# Delta")

    CodexAppServerClient.handle_notification(
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "# Fallback"}},
        },
        state,
    )

    assert state.text == "# Delta"


def test_handle_turn_completed_marks_done():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {"method": "turn/completed", "params": {"status": "completed"}},
        state,
    )

    assert state.done is True
    assert state.error is None


def test_handle_turn_completed_failed_records_error_message():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {
            "method": "turn/completed",
            "params": {"status": "failed", "error": {"message": "model failed"}},
        },
        state,
    )

    assert state.done is True
    assert state.error == "model failed"


def test_handle_turn_completed_interrupted_records_error_and_done():
    state = CodexTurnState(text="# Partial note")

    CodexAppServerClient.handle_notification(
        {"method": "turn/completed", "params": {"status": "interrupted"}},
        state,
    )

    assert state.done is True
    assert state.error is not None
    assert "interrupted" in state.error


def test_handle_turn_completed_unknown_status_records_error_and_done():
    state = CodexTurnState(text="# Partial note")

    CodexAppServerClient.handle_notification(
        {"method": "turn/completed", "params": {"status": "cancelled"}},
        state,
    )

    assert state.done is True
    assert state.error is not None
    assert "cancelled" in state.error


def test_handle_nested_turn_completed_failed_records_error_message():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "failed", "error": {"message": "model failed"}}},
        },
        state,
    )

    assert state.done is True
    assert state.error == "model failed"


def test_handle_error_notification_records_error_and_done():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {"method": "error", "params": {"message": "bad request"}},
        state,
    )

    assert state.done is True
    assert state.error == "bad request"


def test_handle_retryable_error_notification_does_not_mark_done_or_error():
    state = CodexTurnState(text="# Partial note")

    CodexAppServerClient.handle_notification(
        {"method": "error", "params": {"message": "temporary failure", "willRetry": True}},
        state,
    )

    assert state.done is False
    assert state.error is None
    assert state.text == "# Partial note"


def test_handle_nested_error_notification_records_error_and_done():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {"method": "error", "params": {"error": {"message": "bad request"}}},
        state,
    )

    assert state.done is True
    assert state.error == "bad request"


def test_clean_markdown_rejects_empty_output():
    with pytest.raises(CodexAppServerError, match="empty Markdown"):
        CodexAppServerClient.clean_markdown("   \n")


def test_clean_markdown_strips_markdown_fenced_code_block():
    text = "```markdown\n# Title\n\nBody\n```\n"

    assert CodexAppServerClient.clean_markdown(text) == "# Title\n\nBody"


def test_clean_markdown_strips_plain_fenced_code_block():
    text = "```\n# Title\n```\n"

    assert CodexAppServerClient.clean_markdown(text) == text.strip()


def test_clean_markdown_keeps_multiple_top_level_fenced_blocks():
    text = "```\na\n```\n\n# Note\n\n```\nb\n```"

    assert CodexAppServerClient.clean_markdown(text) == text


def test_extract_thread_id_reads_result_thread_id():
    assert (
        CodexAppServerClient._extract_thread_id(
            {"result": {"thread": {"id": "thread-123"}}}
        )
        == "thread-123"
    )


def test_extract_thread_id_rejects_missing_thread_id():
    assert CodexAppServerClient._extract_thread_id({"result": {}}) is None


def test_extract_thread_id_rejects_legacy_thread_id_fields():
    assert CodexAppServerClient._extract_thread_id({"result": {"threadId": "legacy"}}) is None
    assert CodexAppServerClient._extract_thread_id({"result": {"id": "legacy"}}) is None


def test_consume_next_message_rejects_server_request():
    stdout_queue = queue.Queue()
    stdout_queue.put({"id": 99, "method": "attestation/generate", "params": {}})
    client = CodexAppServerClient(codex_bin="codex")

    with pytest.raises(
        CodexAppServerError,
        match="Unsupported Codex app-server request.*attestation/generate",
    ):
        client._consume_next_message(stdout_queue, CodexTurnState(), time.monotonic() + 1)


class _RecordingStdin:
    def __init__(self):
        self.messages = []

    def write(self, text):
        self.messages.append(json.loads(text))
        return len(text)

    def flush(self):
        pass


class _FakeProcess:
    def __init__(self, stdout_messages):
        self.pid = 1234
        self.stdin = _RecordingStdin()
        self.stdout = iter(json.dumps(message) + "\n" for message in stdout_messages)
        self.stderr = iter(())
        self._terminated = False

    def poll(self):
        return 0 if self._terminated else None

    def terminate(self):
        self._terminated = True

    def wait(self, timeout=None):
        self._terminated = True
        return 0

    def kill(self):
        self._terminated = True


class _FakeRunningProcess:
    pid = 1234

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0

    def kill(self):
        self.killed = True


def test_terminate_process_uses_taskkill_for_windows_process_tree(monkeypatch):
    calls = []
    process = _FakeRunningProcess()

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(CodexAppServerClient, "_is_windows", staticmethod(lambda: True))
    monkeypatch.setattr("app.gpt.codex_app_server_client.subprocess.run", fake_run)

    CodexAppServerClient._terminate_process(process)

    assert calls == [
        (
            ["taskkill", "/PID", "1234", "/T", "/F"],
            {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
        )
    ]
    assert process.terminated is False
    assert process.killed is False


def test_terminate_process_falls_back_when_windows_taskkill_fails(monkeypatch):
    process = _FakeRunningProcess()

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(CodexAppServerClient, "_is_windows", staticmethod(lambda: True))
    monkeypatch.setattr("app.gpt.codex_app_server_client.subprocess.run", fake_run)

    CodexAppServerClient._terminate_process(process)

    assert process.terminated is True
    assert process.killed is False
    assert process.wait_calls == [5]


def test_run_markdown_turn_sends_thread_id_from_thread_start_response(monkeypatch):
    stdout_messages = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "thread-123"}}},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "# Note"}},
        {"method": "turn/completed", "params": {"status": "completed"}},
    ]
    fake_processes = []

    def fake_popen(*args, **kwargs):
        process = _FakeProcess(stdout_messages)
        fake_processes.append(process)
        return process

    monkeypatch.setattr(
        "app.gpt.codex_app_server_client.CodexAppServerStatusService.assert_ready",
        lambda: None,
    )
    monkeypatch.setattr(CodexAppServerClient, "_is_windows", staticmethod(lambda: False))
    monkeypatch.setattr("app.gpt.codex_app_server_client.subprocess.Popen", fake_popen)

    result = CodexAppServerClient(codex_bin="codex", timeout_seconds=1).run_markdown_turn(
        "make note",
        "gpt-5",
        cwd="E:\\VideoToNote",
    )

    assert result == "# Note"
    turn_start = fake_processes[0].stdin.messages[3]
    assert turn_start["method"] == "turn/start"
    assert turn_start["params"]["threadId"] == "thread-123"


def test_run_markdown_turn_sends_initialized_notification_after_initialize(monkeypatch):
    stdout_messages = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "thread-123"}}},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "# Note"}},
        {"method": "turn/completed", "params": {"status": "completed"}},
    ]
    fake_processes = []

    def fake_popen(*args, **kwargs):
        process = _FakeProcess(stdout_messages)
        fake_processes.append(process)
        return process

    monkeypatch.setattr(
        "app.gpt.codex_app_server_client.CodexAppServerStatusService.assert_ready",
        lambda: None,
    )
    monkeypatch.setattr(CodexAppServerClient, "_is_windows", staticmethod(lambda: False))
    monkeypatch.setattr("app.gpt.codex_app_server_client.subprocess.Popen", fake_popen)

    CodexAppServerClient(codex_bin="codex", timeout_seconds=1).run_markdown_turn(
        "make note",
        "gpt-5",
        cwd="E:\\VideoToNote",
    )

    messages = fake_processes[0].stdin.messages
    assert [message["method"] for message in messages[:3]] == [
        "initialize",
        "initialized",
        "thread/start",
    ]
    assert messages[1] == {"jsonrpc": "2.0", "method": "initialized", "params": {}}


def test_run_markdown_turn_rejects_legacy_thread_start_response(monkeypatch):
    stdout_messages = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"threadId": "legacy"}},
    ]

    def fake_popen(*args, **kwargs):
        return _FakeProcess(stdout_messages)

    monkeypatch.setattr(
        "app.gpt.codex_app_server_client.CodexAppServerStatusService.assert_ready",
        lambda: None,
    )
    monkeypatch.setattr(CodexAppServerClient, "_is_windows", staticmethod(lambda: False))
    monkeypatch.setattr("app.gpt.codex_app_server_client.subprocess.Popen", fake_popen)

    with pytest.raises(CodexAppServerError, match="thread id"):
        CodexAppServerClient(codex_bin="codex", timeout_seconds=1).run_markdown_turn(
            "make note",
            "gpt-5",
            cwd="E:\\VideoToNote",
        )
