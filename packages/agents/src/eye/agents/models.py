"""Configured model chains and pinned runtime prices."""

import os

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from agent_runtime import ModelPrice, PriceTable, model_chain
from eye.config import LlmSettings


class _MissingCredentialsModel(Model):
    def __init__(self, name: str, provider: str, key_env: str) -> None:
        super().__init__()
        self._name = name
        self._system = provider
        self._key_env = key_env

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def system(self) -> str:
        return self._system

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        raise ModelAPIError(self.model_name, f"Missing credential: {self._key_env}")


def build_model(settings: LlmSettings, agent: str) -> Model:
    def build(name: str) -> Model:
        key_env = (
            "ANTHROPIC_API_KEY"
            if settings.provider == "anthropic"
            else settings.api_key_env
        )
        if not os.environ.get(key_env):
            return _MissingCredentialsModel(name, settings.provider, key_env)
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
