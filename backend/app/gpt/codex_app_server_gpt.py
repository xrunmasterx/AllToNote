from types import SimpleNamespace
from typing import Any

from app.gpt.codex_app_server_client import CodexAppServerClient, CodexAppServerError
from app.gpt.universal_gpt import UniversalGPT
from app.models.gpt_model import GPTSource


MARKDOWN_ONLY_INSTRUCTION = (
    "Only output the final Markdown note. Do not describe your process."
)


class CodexAppServerGPT(UniversalGPT):
    def __init__(self, client: Any | None = None, model: str = "codex-app-server"):
        super().__init__(client=client, model=model)

    def list_models(self):
        return []

    def summarize(self, source: GPTSource) -> str:
        if source.video_img_urls:
            raise CodexAppServerError(
                "Codex app-server first version does not support video screenshots/images."
            )
        return super().summarize(source)

    def _chat_completion_create(self, messages: list):
        prompt = self._extract_prompt(messages)
        prompt = f"{prompt}\n\n{MARKDOWN_ONLY_INSTRUCTION}"
        content = self._codex_client().generate_note(prompt)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                )
            ]
        )

    def _codex_client(self):
        return self.client or CodexAppServerClient()

    @staticmethod
    def _extract_prompt(messages: list) -> str:
        parts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(CodexAppServerGPT._extract_text_parts(content))

        prompt = "\n\n".join(part.strip() for part in parts if part.strip())
        if not prompt:
            raise CodexAppServerError("Codex app-server prompt is empty")
        return prompt

    @staticmethod
    def _extract_text_parts(content: list) -> list[str]:
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if item.get("type") == "text" and isinstance(text, str):
                parts.append(text)
        return parts
