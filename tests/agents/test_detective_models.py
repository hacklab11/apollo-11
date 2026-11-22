from unittest.mock import Mock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_runtime import UnpricedModel
from agent_runtime.configured_models import build_model, price_table
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
    monkeypatch.setattr("agent_runtime.configured_models.AnthropicModel", constructor)
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


def test_openai_compatible_tool_schema_drops_property_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.tools import ToolDefinition

    from eye.tools.models import MetricWindowInput

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    model = build_model(LlmSettings(provider="openai_compatible"), "sentinel")
    schema = MetricWindowInput.model_json_schema()
    assert "propertyNames" in schema["properties"]["segment"]
    params = model.customize_request_parameters(
        ModelRequestParameters(
            function_tools=[ToolDefinition(name="t", parameters_json_schema=schema)]
        )
    )
    segment = params.function_tools[0].parameters_json_schema["properties"]["segment"]
    assert "propertyNames" not in segment
    assert "country" in segment["description"]
    # Mixed strict and non-strict tools are rejected by some providers (Azure).
    assert model.profile.get("openai_supports_strict_tool_definition") is False
