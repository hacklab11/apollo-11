"""Configured model chains and pinned runtime prices."""

import os
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles import JsonSchemaTransformer, ModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from agent_runtime import ModelPrice, PriceTable, model_chain


class _Price(Protocol):
    input_per_mtok: float
    output_per_mtok: float


class ModelConfig(Protocol):
    provider: str
    api_key_env: str
    base_url: str
    effort: Mapping[str, Literal["low", "medium", "high", "xhigh", "max"]]
    fallback_models: Mapping[str, Sequence[str]]
    prices: Mapping[str, _Price]

    def model_for(self, agent: str) -> str: ...


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


class _PassThrough(JsonSchemaTransformer):
    def transform(self, schema: dict[str, Any]) -> dict[str, Any]:
        return schema


def _compatible_profile(profile: ModelProfile) -> ModelProfile:
    """Tool schemas that OpenAI-compatible backends accept.

    Some backends reject `propertyNames` (moved into the description; Pydantic still
    validates the keys) or a mix of strict and non-strict tools (strict is turned off).
    """
    base = profile.get("json_schema_transformer") or _PassThrough

    class Transformer(base):  # type: ignore[valid-type,misc]
        def transform(self, schema: dict[str, Any]) -> dict[str, Any]:
            names = schema.pop("propertyNames", None)
            if isinstance(names, dict) and "enum" in names:
                keys = ", ".join(str(key) for key in names["enum"])
                hint = f"Allowed keys: {keys}."
                schema["description"] = (
                    f"{schema.get('description', '')} {hint}".strip()
                )
            return super().transform(schema)

    return {
        **profile,
        "json_schema_transformer": Transformer,
        "openai_supports_strict_tool_definition": False,
    }


def build_model(
    settings: ModelConfig, agent: str, *, effort_key: str | None = None
) -> Model:
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
                    anthropic_effort=settings.effort.get(effort_key or agent, "medium")
                ),
            )
        return OpenAIChatModel(
            name,
            provider=OpenAIProvider(
                base_url=settings.base_url, api_key=os.environ[settings.api_key_env]
            ),
            profile=_compatible_profile,
        )

    return model_chain(
        build(settings.model_for(agent)),
        *(build(name) for name in settings.fallback_models.get(agent, [])),
    )


def price_table(settings: ModelConfig) -> PriceTable:
    return PriceTable(
        prices={
            name: ModelPrice(
                input_per_mtok=price.input_per_mtok,
                output_per_mtok=price.output_per_mtok,
            )
            for name, price in settings.prices.items()
        }
    )
