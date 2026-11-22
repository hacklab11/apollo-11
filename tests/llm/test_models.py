from unittest.mock import Mock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.usage import RequestUsage

from agent_runtime import UnpricedModel
from eye.config import LlmSettings
from eye.llm.client import MissingProviderCredentials
from eye.llm.models import PRICES, build_model


def test_openai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "offline-test-only")
    model = build_model(
        LlmSettings(
            provider="openai_compatible",
            api_key_env="TEST_PROVIDER_KEY",
            base_url="http://127.0.0.1:9999/v1",
            models={"default": "detective"},
        ),
        "detective",
    )
    assert isinstance(model, OpenAIChatModel)
    assert str(model.client.base_url) == "http://127.0.0.1:9999/v1/"
    assert model.client.api_key == "offline-test-only"


def test_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    with pytest.raises(MissingProviderCredentials):
        build_model(
            LlmSettings(provider="openai_compatible", api_key_env="TEST_PROVIDER_KEY"),
            "detective",
        )


def test_anthropic_and_fallback_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    constructor = Mock(
        side_effect=lambda name: FunctionModel(
            lambda *_: ModelResponse(parts=[TextPart(name)]), model_name=name
        )
    )
    monkeypatch.setattr("eye.llm.models.AnthropicModel", constructor)
    model = build_model(
        LlmSettings(models={"default": "default-model", "detective": "primary-model"}),
        "detective",
    )
    assert isinstance(model, FallbackModel)
    assert [call.args[0] for call in constructor.call_args_list] == [
        "primary-model",
        "default-model",
    ]


def test_prices_fail_closed() -> None:
    assert PRICES.cost("detective", RequestUsage(input_tokens=100)) == 0
    with pytest.raises(UnpricedModel):
        PRICES.cost("unknown", RequestUsage())
