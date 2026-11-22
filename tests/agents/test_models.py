import pytest
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.usage import RequestUsage

from agent_runtime import UnpricedModel
from eye.agents.models import build_model, price_table
from eye.config import LlmPrice, LlmSettings


def test_anthropic_model_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    model = build_model(LlmSettings(), "cartographer")
    assert isinstance(model, AnthropicModel)
    assert model.model_name == "claude-opus-5-5"
    assert model.settings["anthropic_effort"] == "high"


def test_openai_compatible_uses_explicit_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "offline-test-only")
    settings = LlmSettings(
        provider="openai_compatible",
        base_url="http://127.0.0.1:8400/v1",
        api_key_env="TEST_MODEL_KEY",
        models={"cartographer": "primary:free", "default": "secondary:free"},
        fallback_models={"cartographer": ["fallback:free", "last:free"]},
    )
    chain = build_model(settings, "cartographer")
    assert isinstance(chain, FallbackModel)
    assert [model.model_name for model in chain.models] == [
        "primary:free",
        "fallback:free",
        "last:free",
    ]
    assert all(isinstance(model, OpenAIChatModel) for model in chain.models)
    assert all(
        str(model.base_url).rstrip("/") == settings.base_url for model in chain.models
    )


@pytest.mark.asyncio
async def test_missing_openai_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    model = build_model(
        LlmSettings(provider="openai_compatible", api_key_env="TEST_MODEL_KEY"),
        "cartographer",
    )
    with pytest.raises(ModelAPIError, match="Missing credential: TEST_MODEL_KEY"):
        await model.request([], None, ModelRequestParameters())


def test_prices_fail_closed_for_unknown_paid_model() -> None:
    settings = LlmSettings()
    settings.prices["primary:free"] = LlmPrice(input_per_mtok=0, output_per_mtok=0)
    prices = price_table(settings)
    usage = RequestUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert prices.cost("claude-opus-5-5", usage) == 30
    assert prices.cost("primary:free", usage) == 0
    with pytest.raises(UnpricedModel):
        prices.cost("unknown-paid", usage)
