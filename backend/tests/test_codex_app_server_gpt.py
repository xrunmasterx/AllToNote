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
        self.calls = []

    def run_markdown_turn(self, prompt: str, model: str, cwd=None):
        self.calls.append((prompt, model, cwd))
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
    assert len(fake_client.calls) == 1
    prompt, model, cwd = fake_client.calls[0]
    assert model == "gpt-5"
    assert cwd is None
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

    assert fake_client.calls == []


def test_extract_prompt_reads_text_parts_from_list_content():
    prompt = CodexAppServerGPT._extract_prompt([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "First text"},
                {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
                {"type": "text", "text": "Second text"},
            ],
        }
    ])

    assert prompt == "First text\n\nSecond text"


def test_extract_prompt_rejects_empty_prompt():
    with pytest.raises(CodexAppServerError, match="prompt is empty"):
        CodexAppServerGPT._extract_prompt([
            {"role": "user", "content": "   "},
            {"role": "user", "content": [{"type": "text", "text": ""}]},
        ])


def test_summarize_merges_multiple_chunks_with_openai_response_shape(tmp_path: Path):
    class _NumberedFakeCodexClient:
        def __init__(self):
            self.calls = []

        def run_markdown_turn(self, prompt: str, model: str, cwd=None):
            self.calls.append((prompt, model, cwd))
            return f"# Partial {len(self.calls)}"

    fake_client = _NumberedFakeCodexClient()
    gpt = CodexAppServerGPT(client=fake_client, model="gpt-5")
    gpt.checkpoint_dir = tmp_path
    source = _source(segment=[
        TranscriptSegment(start=0, end=3, text="Chunk one"),
        TranscriptSegment(start=3, end=7, text="Chunk two"),
    ])
    one_segment_messages = gpt.create_messages(
        [source.segment[0]],
        title=source.title,
        tags=source.tags,
        video_img_urls=[],
        _format=source._format,
        style=source.style,
        extras=source.extras,
    )
    gpt.max_request_bytes = gpt._estimate_messages_bytes(one_segment_messages)

    result = gpt.summarize(source)

    assert result == "# Partial 3"
    assert len(fake_client.calls) == 3
    assert [call[1] for call in fake_client.calls] == ["gpt-5", "gpt-5", "gpt-5"]
    assert "Chunk one" in fake_client.calls[0][0]
    assert "Chunk two" in fake_client.calls[1][0]
    assert "# Partial 1" in fake_client.calls[2][0]
    assert "# Partial 2" in fake_client.calls[2][0]
