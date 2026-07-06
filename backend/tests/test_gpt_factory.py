from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.gpt.codex_app_server_gpt import CodexAppServerGPT
from app.gpt.gpt_factory import GPTFactory
from app.gpt.universal_gpt import UniversalGPT
from app.models.model_config import ModelConfig


def _config(provider: str):
    return ModelConfig(
        name="provider name",
        provider=provider,
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model_name="test-model",
    )


def test_factory_routes_codex_app_server_provider():
    gpt = GPTFactory.create_gpt(_config("codex_app_server"))

    assert isinstance(gpt, CodexAppServerGPT)


def test_factory_keeps_universal_gpt_for_regular_provider(monkeypatch):
    class _FakeProvider:
        def __init__(self, api_key: str, base_url: str):
            self.get_client = object()

    monkeypatch.setattr("app.gpt.gpt_factory.OpenAICompatibleProvider", _FakeProvider)

    gpt = GPTFactory.create_gpt(_config("openai"))

    assert isinstance(gpt, UniversalGPT)
