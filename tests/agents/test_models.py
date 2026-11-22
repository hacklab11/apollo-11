import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.usage import RequestUsage

from agent_runtime import UnpricedModel
from eye.agents.models import configured_model, configured_prices
from eye.config import LlmSettings


def test_anthropic_model_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    model = configured_model(LlmSettings(), "cartographer")
    assert isinstance(model, AnthropicModel)
    assert model.model_name == "claude-opus-5-5"
    assert model.settings["anthropic_thinking"] == {"type": "adaptive"}
    assert model.settings["anthropic_effort"] == "high"


def test_openai_compatible_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "offline-test-only")
    settings = LlmSettings(
        provider="openai_compatible",
        base_url="http://127.0.0.1:8400/v1",
        api_key_env="TEST_MODEL_KEY",
        models={"cartographer": "primary:free", "default": "secondary:free"},
    )
    chain = configured_model(settings, "cartographer")
    assert isinstance(chain, FallbackModel)
    assert [model.model_name for model in chain.models] == [
        "primary:free",
        "secondary:free",
    ]
    assert all(isinstance(model, OpenAIChatModel) for model in chain.models)
    assert all(
        str(model.base_url).rstrip("/") == settings.base_url for model in chain.models
    )


def test_missing_openai_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    with pytest.raises(ValueError, match="Missing provider credential: TEST_MODEL_KEY"):
        configured_model(
            LlmSettings(provider="openai_compatible", api_key_env="TEST_MODEL_KEY"),
            "cartographer",
        )


def test_prices_fail_closed_for_unknown_paid_model() -> None:
    prices = configured_prices(LlmSettings(models={"cartographer": "primary:free"}))
    usage = RequestUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert prices.cost("claude-opus-5-5", usage) == 30
    assert prices.cost("primary:free", usage) == 0
    with pytest.raises(UnpricedModel):
        prices.cost("unknown-paid", usage)
