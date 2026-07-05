from pathlib import Path
import sys

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
