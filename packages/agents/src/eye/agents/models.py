"""Configured model chains and pinned runtime prices."""

import os

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agent_runtime import ModelPrice, PriceTable, model_chain
from eye.config import LlmSettings


def build_model(settings: LlmSettings, agent: str) -> Model:
    def build(name: str) -> Model:
        if settings.provider == "anthropic":
            return AnthropicModel(
                name,
                settings=AnthropicModelSettings(
                    anthropic_effort=settings.effort.get(agent, "medium")
                ),
            )
        return OpenAIChatModel(
            name,
            provider=OpenAIProvider(
                base_url=settings.base_url, api_key=os.environ[settings.api_key_env]
            ),
        )

    return model_chain(
        build(settings.model_for(agent)),
        *(build(name) for name in settings.fallback_models.get(agent, [])),
    )


def price_table(settings: LlmSettings) -> PriceTable:
    return PriceTable(
        prices={
            name: ModelPrice(**price.model_dump())
            for name, price in settings.prices.items()
        }
    )
