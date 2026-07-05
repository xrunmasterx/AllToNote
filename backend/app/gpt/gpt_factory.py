from openai import OpenAI

from app.gpt.base import GPT
from app.gpt.codex_app_server_gpt import CodexAppServerGPT
from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider
from app.gpt.universal_gpt import UniversalGPT
from app.models.model_config import ModelConfig


class GPTFactory:
    @staticmethod
    def from_config(config: ModelConfig) -> GPT:
        return GPTFactory.create_gpt(config)

    @staticmethod
    def create_gpt(config: ModelConfig) -> GPT:
        if config.provider == "codex_app_server":
            return CodexAppServerGPT(model=config.model_name)

        client = OpenAICompatibleProvider(api_key=config.api_key, base_url=config.base_url).get_client
        return UniversalGPT(client=client, model=config.model_name)
