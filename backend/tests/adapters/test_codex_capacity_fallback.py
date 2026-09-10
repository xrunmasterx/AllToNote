from dataclasses import replace

import pytest

from app.adapters.models.codex_app_server_bridge import CodexAppServerCompletionBridge
from app.adapters.models.legacy_model_executor import LegacyModelExecutor
from app.core.errors import DomainError
from app.core.ports.model_executor import ModelExecutionBinding, ModelExecutionRequest, ModelOutputMode
from app.gpt.codex_app_server_client import CodexAppServerError


PRIMARY, FALLBACK = "gpt-5.6-terra", "gpt-5.6-luna"
BINDING = ModelExecutionBinding(1, "codex-app-server", PRIMARY, "local", 4096, 1024, 4, True, False, 60,
                                fallback_model_identity=FALLBACK)
REQUEST = ModelExecutionRequest(1, "faithful-review", 1, "review", 1, "Read images", "source",
                                ModelOutputMode.JSON_SCHEMA, 512, 30, response_schema_json='{"type":"object"}',
                                image_webp=(b"RIFF1234WEBPtest",))


class Token:
    def raise_if_cancelled(self):
        pass


class Client:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def run_markdown_turn(self, prompt, model, **kwargs):
        self.calls.append((prompt, model, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_capacity_switch_preserves_images_schema_effort_and_reports_luna():
    client = Client(CodexAppServerError("Selected model is at capacity. Please try a different model."), '{"ok":true}')
    bridge = CodexAppServerCompletionBridge(model_identity=PRIMARY, client=client, fallback_model_identity=FALLBACK)
    result = LegacyModelExecutor(binding=BINDING, bridge=bridge).complete(REQUEST, Token())
    assert result.actual_model_identity == FALLBACK
    assert [c[1] for c in client.calls] == [PRIMARY, FALLBACK]
    assert client.calls[0][2] == client.calls[1][2]
    assert client.calls[1][2]["image_webp"] == REQUEST.image_webp
    assert client.calls[1][2]["reasoning_effort"] == "high"
    assert f"model_capacity_fallback:{PRIMARY}->{FALLBACK}" in result.warnings


def test_both_models_full_returns_specific_error_without_unknown_or_loop():
    client = Client(*(CodexAppServerError("Selected model is at capacity") for _ in range(2)))
    bridge = CodexAppServerCompletionBridge(model_identity=PRIMARY, client=client, fallback_model_identity=FALLBACK)
    with pytest.raises(DomainError, match="model_capacity_exhausted"):
        LegacyModelExecutor(binding=BINDING, bridge=bridge).complete(REQUEST, Token())
    assert len(client.calls) == 2


@pytest.mark.parametrize("error", [CodexAppServerError("policy refusal"), CodexAppServerError("not authenticated"),
                                   CodexAppServerError("connection lost"), TimeoutError("timeout")])
def test_other_refusals_do_not_trigger_cross_model_retry(error):
    client = Client(error)
    bridge = CodexAppServerCompletionBridge(model_identity=PRIMARY, client=client, fallback_model_identity=FALLBACK)
    with pytest.raises(Exception):
        bridge.complete_request("source", REQUEST)
    assert len(client.calls) == 1


def test_fallback_requires_binding_authorization():
    client = Client(CodexAppServerError("Selected model is at capacity"), '{}')
    bridge = CodexAppServerCompletionBridge(model_identity=PRIMARY, client=client, fallback_model_identity=FALLBACK)
    with pytest.raises(DomainError, match="model_identity_mismatch"):
        LegacyModelExecutor(binding=replace(BINDING, fallback_model_identity=None), bridge=bridge).complete(REQUEST, Token())
