from pathlib import Path
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.gpt.codex_app_server_client import CodexAppServerError
from app.gpt.codex_app_server_gpt import CodexAppServerGPT
from app.models.gpt_model import GPTSource
from app.models.transcriber_model import TranscriptSegment


class _FakeCodexClient:
    def __init__(self):
        self.prompts = []

    def generate_note(self, prompt: str, timeout_s=None):
        self.prompts.append((prompt, timeout_s))
        return "# Generated note\n\nBody"


def _source(**kwargs):
    defaults = {
        "title": "Demo Video",
        "tags": "demo, test",
        "segment": [
            TranscriptSegment(start=0, end=3, text="Intro text"),
            TranscriptSegment(start=3, end=7, text="Main body"),
        ],
    }
    defaults.update(kwargs)
    return GPTSource(**defaults)


def test_summarize_uses_codex_client_with_markdown_only_prompt(tmp_path: Path):
    fake_client = _FakeCodexClient()
    gpt = CodexAppServerGPT(client=fake_client, model="gpt-5")
    gpt.checkpoint_dir = tmp_path

    result = gpt.summarize(_source())

    assert result == "# Generated note\n\nBody"
    assert len(fake_client.prompts) == 1
    prompt, timeout_s = fake_client.prompts[0]
    assert timeout_s is None
    assert "Demo Video" in prompt
    assert "Intro text" in prompt
    assert "Main body" in prompt
    assert "Only output the final Markdown note" in prompt


def test_summarize_rejects_images_before_calling_codex_client(tmp_path: Path):
    fake_client = _FakeCodexClient()
    gpt = CodexAppServerGPT(client=fake_client, model="gpt-5")
    gpt.checkpoint_dir = tmp_path

    with pytest.raises(CodexAppServerError, match="does not support video screenshots/images"):
        gpt.summarize(_source(video_img_urls=["https://example.com/screenshot.jpg"]))

    assert fake_client.prompts == []
