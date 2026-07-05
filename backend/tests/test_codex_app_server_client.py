from pathlib import Path
import json
import queue
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

    assert CodexAppServerClient.clean_markdown(text) == "# Title"


def test_extract_thread_id_reads_result_thread_id():
    assert (
        CodexAppServerClient._extract_thread_id(
            {"result": {"thread": {"id": "thread-123"}}}
        )
        == "thread-123"
    )


def test_extract_thread_id_rejects_missing_thread_id():
    with pytest.raises(CodexAppServerError, match="thread id"):
        CodexAppServerClient._extract_thread_id({"result": {}})


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
    monkeypatch.setattr("app.gpt.codex_app_server_client.subprocess.Popen", fake_popen)

    result = CodexAppServerClient(codex_bin="codex", timeout_seconds=1).run_markdown_turn(
        "make note",
        "gpt-5",
        cwd="E:\\VideoToNote",
    )

    assert result == "# Note"
    turn_start = fake_processes[0].stdin.messages[2]
    assert turn_start["method"] == "turn/start"
    assert turn_start["params"]["threadId"] == "thread-123"
