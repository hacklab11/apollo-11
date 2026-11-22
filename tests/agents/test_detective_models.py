from unittest.mock import Mock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_runtime import UnpricedModel
from eye.agents.models import build_model, price_table
from eye.config import LlmSettings


@pytest.mark.parametrize("effort", ["high", "low"])
def test_anthropic_effort_and_explicit_fallbacks(
    monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")

    def model(name: str, **kwargs: object) -> FunctionModel:
        return FunctionModel(
            lambda *_: ModelResponse(parts=[TextPart("offline")]), model_name=name
        )

    constructor = Mock(side_effect=model)
    monkeypatch.setattr("eye.agents.models.AnthropicModel", constructor)
    settings = LlmSettings(
        effort={"detective": effort}, fallback_models={"detective": ["backup"]}
    )
    result = build_model(settings, "detective")
    assert isinstance(result, FallbackModel)
    assert [call.args[0] for call in constructor.call_args_list] == [
        "claude-opus-5-5",
        "backup",
    ]
    assert all(
        call.kwargs["settings"]["anthropic_effort"] == effort
        for call in constructor.call_args_list
    )


def test_explicit_free_model_prices() -> None:
    name = "nvidia/nemotron-3-ultra-550b-a55b:free"
    routed = name.removesuffix(":free")
    settings = LlmSettings(
        provider="openai_compatible",
        models={"detective": name},
        prices={
            name: {"input_per_mtok": 0, "output_per_mtok": 0},
            routed: {"input_per_mtok": 0, "output_per_mtok": 0},
        },
    )
    table = price_table(settings)
    usage = RequestUsage(input_tokens=1000, output_tokens=1000)
    assert table.cost(name, usage) == table.cost(routed, usage) == 0
    with pytest.raises(UnpricedModel):
        table.cost("unconfigured:free", usage)
    assert not price_table(LlmSettings(prices={})).prices
