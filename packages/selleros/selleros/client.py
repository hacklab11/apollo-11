"""Pinned SellerOS model settings for the shared runtime."""

import os
from functools import cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from agent_runtime import ModelPrice, PriceTable, model_chain

CLAUDE = "claude-opus-5-5"
OPENROUTER_MODELS = (
    "openai/gpt-oss-20b",
    "qwen/qwen3.7-flash",
    "nvidia/nemotron-3.5-lightning:free",
)
PRICES = PriceTable(
    prices={
        CLAUDE: ModelPrice(input_per_mtok=5, output_per_mtok=25),
        OPENROUTER_MODELS[0]: ModelPrice(input_per_mtok=0.017, output_per_mtok=0.085),
        OPENROUTER_MODELS[1]: ModelPrice(input_per_mtok=0.03, output_per_mtok=0.13),
        OPENROUTER_MODELS[2]: ModelPrice(input_per_mtok=0, output_per_mtok=0),
    }
)


class LlmSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @classmethod
    def from_env(cls) -> "LlmSettings":
        return cls.model_validate(
            {
                name: os.environ[key]
                for name in cls.model_fields
                if (key := f"SELLEROS_{name.upper()}") in os.environ
            }
        )


@cache
def build_model(settings: LlmSettings) -> Model:
    anthropic = settings.provider == "anthropic"
    default_name = CLAUDE if anthropic else OPENROUTER_MODELS[0]
    model_name = settings.model or default_name
    if model_name not in PRICES.prices:
        raise ValueError(f"No pinned price for SellerOS model {model_name!r}")
    key_env = settings.api_key_env or (
        "ANTHROPIC_API_KEY" if anthropic else "OPENROUTER_API_KEY"
    )
    key = os.environ.get(key_env)
    if not key:
        raise ValueError(f"Missing SellerOS provider credential: {key_env}")
    if anthropic:
        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(api_key=key),
            settings={
                "anthropic_effort": "medium",
                "anthropic_cache_instructions": True,
            },
        )
    provider = OpenAIProvider(base_url=settings.base_url, api_key=key)
    names = OPENROUTER_MODELS if model_name == default_name else (model_name,)
    return model_chain(*(OpenAIChatModel(name, provider=provider) for name in names))
