import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage

from agent_runtime import RunLimits, RunScope, UnpricedModel, run_agent
from eye.agents.models import build_model, price_table
from eye.config import LlmPrice, LlmSettings

OPENROUTER = "https://openrouter.ai/api/v1"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("routes", "base_url", "cap", "status", "cost"),
    [
        (["vendor/model:free"], OPENROUTER, 0, "done", 0),
        (["openrouter/free"], OPENROUTER, 0, "done", 0),
        (["vendor/model"], OPENROUTER, 1, "done", 0.03),
        (["vendor/model:free", "vendor/model"], OPENROUTER, 0.01, "gave_up", 0.03),
        (["vendor/model:free"], "https://provider.example/v1", 0, "failed", 0),
        (["vendor/unpriced"], OPENROUTER, 1, "failed", 0),
    ],
    ids=[
        "openrouter-free-without-price",
        "openrouter-free-router",
        "pinned-paid-alias",
        "paid-fallback-still-capped",
        "free-suffix-on-other-provider",
        "reported-free-suffix-is-not-a-free-route",
    ],
)
async def test_cost_of_routed_response(
    monkeypatch: pytest.MonkeyPatch,
    routes: list[str],
    base_url: str,
    cap: float,
    status: str,
    cost: float,
) -> None:
    requested: list[str] = []

    def completion(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["model"]
        requested.append(name)
        if len(routes) > 1 and name == routes[0]:
            return httpx.Response(400, json={"error": {"message": "route unavailable"}})
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 1,
                "model": {
                    "vendor/model:free": "vendor/model",
                    "vendor/unpriced": "vendor/model-resolved:free",
                }.get(name, "vendor/model-resolved"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "offline"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 1000,
                    "total_tokens": 2000,
                },
            },
        )

    monkeypatch.setenv("TEST_MODEL_KEY", "offline-test-only")
    settings = LlmSettings(
        provider="openai_compatible",
        base_url=base_url,
        api_key_env="TEST_MODEL_KEY",
        models={"cartographer": routes[0]},
        fallback_models={"cartographer": routes[1:]},
        prices={
            name: LlmPrice(input_per_mtok=5, output_per_mtok=25)
            for name in routes
            if name == "vendor/model"
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(completion)) as http:

        def offline_provider(*, base_url: str, api_key: str) -> OpenAIProvider:
            return OpenAIProvider(
                openai_client=AsyncOpenAI(
                    base_url=base_url, api_key=api_key, http_client=http
                )
            )

        monkeypatch.setattr("eye.agents.models.OpenAIProvider", offline_provider)
        agent = Agent(build_model(settings, "cartographer"))
        with models.override_allow_model_requests(True):
            outcome = await run_agent(
                agent,
                "test route pricing",
                scope=RunScope(run_id="test-pricing", tenant_id="test"),
                limits=RunLimits(max_cost_usd=cap),
                prices=price_table(settings),
            )
    assert requested == routes
    assert outcome.result.status == status, outcome.result.reason
    assert outcome.result.usage.cost_usd == pytest.approx(cost)
    assert outcome.result.usage.input_tokens == 1000
    assert outcome.result.usage.output_tokens == 1000
