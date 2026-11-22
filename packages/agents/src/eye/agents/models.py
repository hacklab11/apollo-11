"""Construct provider models from the existing application settings."""

import os

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agent_runtime import model_chain
from eye.config import LlmSettings


def configured_model(settings: LlmSettings, agent: str) -> Model:
    def build(name: str) -> Model:
        if settings.provider == "anthropic":
            return AnthropicModel(name)
        return OpenAIChatModel(
            name,
            provider=OpenAIProvider(
                base_url=str(settings.base_url),
                api_key=os.environ[settings.api_key_env],
            ),
        )

    names = list(dict.fromkeys([settings.model_for(agent), settings.models["default"]]))
    return model_chain(build(names[0]), *(build(name) for name in names[1:]))
