import asyncio
import time

import httpx
import pytest
from a2a.types.a2a_pb2 import TaskState
from conftest import data_client, s1_request, s7_request, send, settings
from google.protobuf.json_format import MessageToDict
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import (
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

from eye.a2a_client import A2AClient
from eye.contracts_v2 import SpecialistResponse
from eye.specialists.app import create_app
from eye.specialists.tools import DataClient

SKILLS = {
    "growth": ["ads_pacing", "auction_health", "traffic_sources", "discounts"],
    "marketplace": ["buybox", "pricing", "availability", "purchasing", "new_sellers"],
    "discovery": [
        "search_quality",
        "feed_exposure",
        "checkout_funnel",
        "client_errors",
        "components",
    ],
}


async def ask(
    http: httpx.AsyncClient, agent: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[SpecialistResponse, list[dict[str, str]]]:
    request = s1_request if agent == "growth" else s7_request

    def respond(messages, info):
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            window = {
                "start": request().window_start_ts.isoformat(),
                "end": request().window_end_ts.isoformat(),
            }
            calls = (
                [("ads_pacing", {**window, "advertiser_id": "ADV-3"})]
                if agent == "growth"
                else [("errors", window), ("checkout_funnel", window)]
            )
            return ModelResponse(
                parts=[ToolCallPart(name, args) for name, args in calls]
            )
        rows = returns[0].content.rows
        if agent == "growth":
            row = next(row for row in rows if "10:00" in row["ts"])
            assert row["spent_today"] == row["daily_budget"] == 1000
            assert row["planned_cum_share"] == 0.38
            claim = "ADV-3 spent 100% by 10:00 against 38% scheduled"
            numbers = {"spent_eur": row["spent_today"]}
            supports = ["pacing_bug"]
        else:
            count = sum(
                row["errors"]
                for row in rows
                if row["platform"] == "web" and row["app_version"] == "5.8"
            )
            assert count == 62000
            claim = "checkout-web v5.8 caused 62,000 TypeError errors on web"
            numbers = {"errors": count}
            supports = []
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit",
                    {
                        "agent": "overridden",
                        "answered": True,
                        "findings": [
                            {
                                "claim": claim,
                                "numbers": numbers,
                                "confidence": 0.9,
                                "supports": supports,
                            }
                        ],
                        "data_used": ["untrusted"],
                    },
                )
            ]
        )

    monkeypatch.setattr(
        "eye.specialists.app.build_model",
        lambda *_, **__: FunctionModel(respond, model_name="claude-opus-5-5"),
    )
    task = await send(http, agent, request())
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, MessageToDict(
        task.status
    )
    (artifact,) = task.artifacts
    (part,) = artifact.parts
    steps = MessageToDict(artifact.metadata)["steps"]
    return SpecialistResponse.model_validate(MessageToDict(part.data)), steps


@pytest.mark.asyncio
async def test_each_card_is_served_with_its_skills(specialists) -> None:
    _, http = specialists
    for agent, skills in SKILLS.items():
        response = await http.get(
            f"http://specialists/{agent}/.well-known/agent-card.json"
        )
        assert response.status_code == 200
        card = response.json()
        assert [skill["id"] for skill in card["skills"]] == skills
        assert card["supportedInterfaces"][0]["url"] == f"http://specialists/{agent}"


@pytest.mark.asyncio
async def test_core_client_sees_all_three_specialists(specialists) -> None:
    _, http = specialists
    client = A2AClient(settings().a2a, http_client=http)
    cards = await client.fetch_cards()
    for agent, skills in SKILLS.items():
        assert cards[agent].healthy and cards[agent].skills == skills


@pytest.mark.asyncio
async def test_growth_answers_s1(specialists, monkeypatch) -> None:
    _, http = specialists
    response, steps = await ask(http, "growth", monkeypatch)
    assert response.answered and response.agent == "growth"
    finding = next(f for f in response.findings if "pacing_bug" in f.supports)
    for text in ("ADV-3", "100%", "10:00", "38%"):
        assert text in finding.claim
    assert all("traffic_change" not in f.supports for f in response.findings)
    assert "ads/pacing" in response.data_used
    assert "ads_pacing" in {step["tool"] for step in steps}
    assert all(set(step) == {"tool", "summary"} for step in steps)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{}, {"start": "invalid"}, {"country": "XX"}])
async def test_invalid_tool_arguments_are_retried(
    specialists, monkeypatch, invalid
) -> None:
    request = s1_request()
    valid = {
        "start": request.window_start_ts.isoformat(),
        "end": request.window_end_ts.isoformat(),
    }
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            args = {} if not invalid else {**valid, **invalid}
            return ModelResponse(parts=[ToolCallPart("marketing", args)])
        if calls == 2:
            assert any(isinstance(part, RetryPromptPart) for part in messages[-1].parts)
            return ModelResponse(parts=[ToolCallPart("marketing", valid)])
        assert any(isinstance(part, ToolReturnPart) for part in messages[-1].parts)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit", {"agent": "growth", "answered": False, "findings": []}
                )
            ]
        )

    monkeypatch.setattr(
        "eye.specialists.app.build_model",
        lambda *_, **__: FunctionModel(respond, model_name="claude-opus-5-5"),
    )
    _, http = specialists
    task = await send(http, "growth", request)
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, MessageToDict(
        task.status
    )
    assert calls == 3
    assert MessageToDict(task.artifacts[0].metadata)["steps"][0]["tool"] == "marketing"


@pytest.mark.asyncio
async def test_discovery_answers_s7(specialists, monkeypatch) -> None:
    _, http = specialists
    response, steps = await ask(http, "discovery", monkeypatch)
    assert response.answered and response.agent == "discovery"
    claims = " ".join(f.claim for f in response.findings)
    assert "checkout-web v5.8" in claims and "TypeError" in claims
    # 4 broken hours x 15,500 checkout TypeErrors per platform in the S7 world.
    assert any(
        "62,000" in f.claim and 62000 in f.numbers.values() for f in response.findings
    )
    assert "errors" in response.data_used
    assert {s["tool"] for s in steps} >= {"errors", "checkout_funnel"}


@pytest.mark.asyncio
async def test_data_api_down_fails_task_promptly(monkeypatch) -> None:
    def unexpected_model(*_):
        raise AssertionError("model must not be built when data API is down")

    monkeypatch.setattr("eye.specialists.app.build_model", unexpected_model)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    dead = DataClient(
        httpx.AsyncClient(transport=httpx.MockTransport(down)), "http://core/data"
    )
    app = create_app(settings(), dead)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        started = time.monotonic()
        task = await asyncio.wait_for(send(http, "growth", s1_request()), timeout=5)
        assert time.monotonic() - started < 5
        assert task.status.state == TaskState.TASK_STATE_FAILED
        text = task.status.message.parts[0].text
        assert "core data API unavailable at http://core/data: ConnectError" in text
        assert task.artifacts == []
        core = await A2AClient(settings().a2a, http_client=http).ask(
            "growth", s1_request()
        )
        assert not core.answered and core.findings == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["needs_input", "gave_up", "provider", "limit"])
async def test_unsuccessful_runs_fail_a2a_task(
    specialists, monkeypatch, ending
) -> None:
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        if ending == "provider":
            raise ModelAPIError("claude-opus-5-5", "offline provider failure")
        if ending == "limit":
            request = s1_request()
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "changes",
                        {
                            "start": request.window_start_ts.isoformat(),
                            "end": request.window_end_ts.isoformat(),
                            "system": f"system-{calls}",
                        },
                    )
                ]
            )
        output = next(tool for tool in info.output_tools if ending in tool.name.lower())
        return ModelResponse(
            parts=[ToolCallPart(output.name, {"reason": "No evidence"})]
        )

    monkeypatch.setattr(
        "eye.specialists.app.build_model",
        lambda *_, **__: FunctionModel(respond, model_name="claude-opus-5-5"),
    )
    _, http = specialists
    task = await send(http, "growth", s1_request())
    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert task.artifacts == []
    assert calls == (8 if ending == "limit" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["growth", "marketplace", "discovery"])
async def test_empty_answer_and_tool_results(monkeypatch, agent) -> None:
    requested = []

    def data(request):
        requested.append(request)
        return httpx.Response(200, json={"rows": []})

    def respond(messages, info):
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            request = s1_request()
            window = {
                "start": request.window_start_ts.isoformat(),
                "end": request.window_end_ts.isoformat(),
            }
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "metric", {**window, "name": "orders", "country": "DE"}
                    ),
                    ToolCallPart("changes", window),
                ]
            )
        assert all(not part.content.rows for part in returns)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit",
                    {
                        "agent": "growth",
                        "answered": False,
                        "findings": [],
                    },
                )
            ]
        )

    monkeypatch.setattr(
        "eye.specialists.app.build_model",
        lambda *_, **__: FunctionModel(respond, model_name="claude-opus-5-5"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(data)) as core:
        app = create_app(settings(), DataClient(core, "http://core/data"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            task = await send(http, agent, s1_request())
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, MessageToDict(
        task.status
    )
    response = MessageToDict(task.artifacts[0].parts[0].data)
    assert response["answered"] is False and response["findings"] == []
    assert response["agent"] == agent
    assert response["data_used"] == ["changes", "metric"]
    metric = next(
        request for request in requested if request.url.path.endswith("/metric")
    )
    assert metric.url.params["segment"] == '{"country": "DE"}'


@pytest.mark.asyncio
async def test_configured_fallback(monkeypatch, world) -> None:
    attempted = []

    def constructor(name, **kwargs):
        assert kwargs["settings"]["anthropic_effort"] == "high"

        def respond(messages, info):
            attempted.append(name)
            if name == "claude-opus-5-5":
                raise ModelAPIError(name, "offline primary unavailable")
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "submit",
                        {
                            "agent": "growth",
                            "answered": False,
                            "findings": [],
                        },
                    )
                ]
            )

        return FunctionModel(respond, model_name=name)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    monkeypatch.setattr("agent_runtime.configured_models.AnthropicModel", constructor)
    configured = settings()
    configured.llm.fallback_models["growth"] = ["backup"]
    configured.llm.effort["specialists"] = "high"
    configured.llm.prices["backup"] = configured.llm.prices["claude-opus-5-5"]
    client = data_client(world)
    try:
        app = create_app(configured, client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            task = await send(http, "growth", s1_request())
    finally:
        await client.http.aclose()
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, MessageToDict(
        task.status
    )
    assert attempted == ["claude-opus-5-5", "backup"]


@pytest.mark.asyncio
async def test_truncated_rows_are_flagged_in_summary() -> None:
    rows = [{"ts": f"2026-11-27T{i % 24:02d}:00:00Z", "n": i} for i in range(250)]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"rows": rows}))
    async with httpx.AsyncClient(transport=transport) as http:
        result = await DataClient(http, "http://core/data").get("offers", {})
    assert result.total_rows == 250 and len(result.rows) == 200
    assert "only the first 200 shown" in result.summary
    assert "narrow the window" in result.summary
