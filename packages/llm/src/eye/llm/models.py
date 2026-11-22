"""PydanticAI provider construction and pinned USD-per-million-token prices."""

import os

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agent_runtime import ModelPrice, PriceTable, model_chain
from eye.config import LlmSettings
from eye.llm.client import MissingProviderCredentials

PRICES = PriceTable(
    prices={
        "claude-opus-5-5": ModelPrice(input_per_mtok=5, output_per_mtok=25),
        "detective": ModelPrice(input_per_mtok=0, output_per_mtok=0),
    }
)


def build_model(settings: LlmSettings, agent: str) -> Model:
    """Use the agent override, falling back to the configured default model."""

    def model(name: str) -> Model:
        if settings.provider == "anthropic":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise MissingProviderCredentials("ANTHROPIC_API_KEY")
            return AnthropicModel(name)
        key = os.environ.get(settings.api_key_env)
        if not key:
            raise MissingProviderCredentials(settings.api_key_env)
        return OpenAIChatModel(
            name,
            provider=OpenAIProvider(
                base_url=settings.base_url,
                api_key=key,
            ),
        )

    primary = settings.model_for(agent)
    default = settings.models["default"]
    return model_chain(
        model(primary), *([model(default)] if default != primary else [])
    )
