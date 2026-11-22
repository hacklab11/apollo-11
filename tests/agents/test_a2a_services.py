import json
from collections import deque
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaMessage
from factories import make_anomaly, make_incident_s7
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from agent_runtime import ModelPrice, PriceTable
from eye.agents.a2a_service import send_skill
from eye.agents.service import PORTS, create_app
from eye.config import LlmPrice, Settings
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    SentinelDecision,
    SpecialistResponse,
)
from eye.contracts_v2 import (
    Budget,
    DetectRequest,
    DetectResult,
    InvestigateRequest,
    InvestigateResult,
    MapRequest,
    ReportRequest,
    ReportResult,
    TaskMetadata,
)
from eye.sim.lake import LakeWriter
from eye.sim.store import Store
from eye.warehouse.client import DataClient

HOUR = timedelta(hours=1)


@pytest.fixture
def metadata() -> TaskMetadata:
    return TaskMetadata(
        tenant_id="default",
        context_id="ctx_s7",
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        budget=Budget(tokens=10_000, cost_usd=0.5, deadline=datetime.now(UTC) + HOUR),
    )


@pytest.fixture(autouse=True)
def model(monkeypatch: pytest.MonkeyPatch) -> deque[list[dict[str, Any]]]:
    turns: deque[list[dict[str, Any]]] = deque()

    class Scripted:
        async def create(self, request: dict) -> BetaMessage:
            if not turns:
                pytest.fail("Unscripted model call")
            content = turns.popleft()
            return BetaMessage.model_validate(
                {
                    "id": "scripted",
                    "type": "message",
                    "role": "assistant",
                    "model": request["model"],
                    "content": content,
                    "stop_reason": "tool_use"
                    if content[0]["type"] == "tool_use"
                    else "end_turn",
                    "usage": {"input_tokens": 7, "output_tokens": 5},
                }
            )

    monkeypatch.setattr("eye.llm.client._client", Scripted())

    def sentinel_response(messages, info):
        content = turns.popleft()
        return ModelResponse(
            parts=[ToolCallPart(item["name"], item["input"]) for item in content],
            model_name="claude-opus-5-5",
            usage=RequestUsage(input_tokens=7, output_tokens=5),
        )

    monkeypatch.setattr(
        "eye.agents.sentinel.build_model",
        lambda *_: FunctionModel(sentinel_response, model_name="claude-opus-5-5"),
    )

    def detective_response(messages: list, info: AgentInfo) -> ModelResponse:
        content = turns.popleft()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    next(
                        t.name
                        for t in info.output_tools
                        if t.name.endswith("_Submission")
                    )
                    if part["name"] == "submit"
                    else part["name"],
                    part["input"],
                )
                for part in content
            ],
            usage=RequestUsage(input_tokens=7, output_tokens=5),
        )

    monkeypatch.setattr(
        "eye.agents.detective.build_model",
        lambda *_: FunctionModel(detective_response, model_name="detective"),
    )

    def skeptic_response(messages: object, info: object) -> ModelResponse:
        content = turns.popleft()
        return ModelResponse(
            usage=RequestUsage(input_tokens=7, output_tokens=5),
            parts=[
                ToolCallPart(part["name"], part["input"])
                if part["type"] == "tool_use"
                else TextPart(part["text"])
                for part in content
            ],
        )

    monkeypatch.setattr(
        "eye.agents.skeptic._model",
        lambda _: FunctionModel(skeptic_response, model_name="claude-opus-5-5"),
    )
    return turns


def use(name: str, payload: dict) -> dict:
    return {"id": f"script_{name}", "type": "tool_use", "name": name, "input": payload}


@pytest.fixture
def warehouse() -> Iterator[DataClient]:
    def answer(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path
        if endpoint == "/v1/anomalies":
            stamp = datetime.fromisoformat(json.loads(request.content)["ts"])
            anm = make_anomaly().model_copy(update={"ts": stamp})
            return httpx.Response(
                200,
                json={
                    "anomalies": [anm.model_dump(mode="json")],
                    "summary": "1 anomaly",
                },
            )
        outputs = {
            "/v1/divergence": {"anomalies": [], "summary": "No divergent segments"},
            "/v1/freshness": {
                "points": [],
                "late_hours": [],
                "verdict": "ok",
                "summary": "Complete",
            },
            "/v1/events": {"events": [], "summary": "No events"},
            "/v1/graph": {"nodes": [], "edges": []},
            "/v1/cycles": [],
            "/v1/metrics": [],
        }
        return httpx.Response(200, json=outputs[endpoint])

    with closing(
        DataClient("http://warehouse", transport=httpx.MockTransport(answer))
    ) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "skill"),
    [
        ("sentinel", "detect"),
        ("detective", "investigate"),
        ("skeptic", "challenge"),
        ("reporter", "report"),
        ("cartographer", "map"),
    ],
)
async def test_service_discovery(name: str, skill: str, tmp_path: Path) -> None:
    url = f"http://{name}:{PORTS[name]}/"
    app = create_app(name, url, Settings(), lake=tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=url) as http,
    ):
        assert (await http.get("/healthz")).json() == {"status": "ok"}
        card = (await http.get("/.well-known/agent-card.json")).json()
    assert [item["id"] for item in card["skills"]] == [skill]
    assert card["supportedInterfaces"] == [
        {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]


@pytest.mark.asyncio
async def test_sentinel_watch_crosses_replicas(
    metadata: TaskMetadata, model: deque, warehouse: DataClient
) -> None:
    url = "http://sentinel:8210/"
    anm = make_anomaly()
    decision = SentinelDecision(anomaly=anm, action="watch", reason="Watch this drop")
    model.extend(
        [[use("submit", {"decisions": [decision.model_dump(mode="json")]})]] * 2
    )
    request = DetectRequest(ts=anm.ts)
    for tick in range(2):
        replica = create_app("sentinel", url, Settings(), data=warehouse)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replica)
        ) as http:
            response = await send_skill(
                http, url, "detect", request, metadata, DetectResult
            )
        assert response.status == "done", response
        assert response.result is not None
        assert response.result.decisions[0].action == (
            "watch" if tick == 0 else "investigate"
        )
        assert response.result.trail[0].actor == "sentinel"
        assert response.usage.total_tokens == 12
        assert response.usage.cost_usd == pytest.approx(0.00016)
        request = DetectRequest(
            ts=anm.ts + HOUR, previous_decisions=response.result.decisions
        )
    assert not model


@pytest.mark.asyncio
@pytest.mark.parametrize("seller_available", [False, True])
async def test_detective_delegates_and_reports(
    metadata: TaskMetadata,
    model: deque,
    warehouse: DataClient,
    monkeypatch: pytest.MonkeyPatch,
    seller_available: bool,
) -> None:
    specialist_calls = []

    async def ask_specialist(self, agent, request):
        specialist_calls.append((agent, request))
        return SpecialistResponse(agent=agent, answered=seller_available, findings=[])

    async def specialist_cards(self):
        return {}

    monkeypatch.setattr("eye.a2a_client.A2AClient.ask", ask_specialist)
    monkeypatch.setattr("eye.a2a_client.A2AClient.fetch_cards", specialist_cards)
    skeptic_url = "http://skeptic:8230/"
    monkeypatch.setenv("EYE_SKEPTIC_URL", skeptic_url)
    settings = Settings()
    settings.llm.prices["detective"] = settings.llm.prices[
        "claude-opus-5-5"
    ].model_copy(update={"input_per_mtok": 0, "output_per_mtok": 0})
    skeptic = create_app("skeptic", skeptic_url, settings, data=warehouse)
    hypothesis = make_incident_s7().root_cause
    assert hypothesis is not None
    model.extend(
        [
            [
                use(
                    "update_hypotheses",
                    {"hypotheses": [hypothesis.model_dump(mode="json")]},
                ),
                use("ask_skeptic", {"hypothesis_id": hypothesis.id}),
                use(
                    "ask_specialist",
                    {
                        "agent": "selleros",
                        "metric": "orders",
                        "question": "Why did orders fall?",
                    },
                ),
            ],
            [
                use(
                    "submit",
                    {
                        "verdict": "not_innocent",
                        "explained_share": 0,
                        "reasons": ["Not planned"],
                        "evidence": [],
                    },
                )
            ],
            [
                use(
                    "submit",
                    {
                        "verdict": "HYPOTHESIS",
                        "summary": "Cause unknown.",
                        "hypotheses": [],
                        "root_cause": None,
                        "contributions": [],
                        "open_questions": [],
                        "suggested_actions": [],
                    },
                )
            ],
            [
                {
                    "type": "text",
                    "text": '{"title":"Orders fell","summary":"No supported explanation yet."}',
                }
            ],
        ]
    )
    calls = []

    async def capture(request: httpx.Request) -> None:
        assert request.url.host == "skeptic"
        if request.method == "POST":
            assert request.headers["traceparent"] == metadata.traceparent
            calls.append(json.loads(request.content)["params"])

    detective_url = "http://detective:8220/"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=skeptic), event_hooks={"request": [capture]}
    ) as delegated:
        detective = create_app(
            "detective", detective_url, settings, data=warehouse, http=delegated
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=detective)
        ) as http:
            investigated = await send_skill(
                http,
                detective_url,
                "investigate",
                InvestigateRequest(anomaly=make_anomaly(), investigation_id="inv_s7"),
                metadata,
                InvestigateResult,
            )
    assert investigated.status == "done", investigated
    assert investigated.result is not None
    assert investigated.usage.requests == 3
    assert investigated.usage.total_tokens == 36
    assert {step.actor for step in investigated.result.trail} == {
        "detective",
        "skeptic",
    }
    assert all(
        not step.input and step.output is None for step in investigated.result.trail
    )
    assert len(specialist_calls) == 1
    assert specialist_calls[0][0] == "selleros"
    assert specialist_calls[0][1].question == "Why did orders fall?"
    assert any(
        step.tool == "selleros"
        and ("SellerOS:" if seller_available else "unavailable") in step.output_summary
        for step in investigated.result.trail
    )
    assert len(calls) == 1
    child = TaskMetadata.model_validate(calls[0]["metadata"])
    assert child.context_id == metadata.context_id
    assert child.traceparent == metadata.traceparent
    assert child.budget.depth == metadata.budget.depth + 1
    assert child.budget.tokens == metadata.budget.tokens - 12
    assert child.budget.deadline == metadata.budget.deadline

    reporter_url = "http://reporter:8240/"
    model.pop()
    reporter_model = TestModel(
        custom_output_args={
            "title": "Orders fell",
            "summary": "No supported explanation yet.",
        }
    )
    monkeypatch.setattr("eye.agents.reporter.build_model", lambda *_: reporter_model)
    settings.llm.prices["test"] = LlmPrice(input_per_mtok=0.0, output_per_mtok=0.0)
    reporter = create_app("reporter", reporter_url, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reporter)) as http:
        reported = await send_skill(
            http,
            reporter_url,
            "report",
            ReportRequest(investigation=investigated.result.investigation),
            metadata,
            ReportResult,
        )
    assert reported.result == ReportResult(
        title="Orders fell", summary="No supported explanation yet."
    )
    assert not model


@pytest.mark.asyncio
async def test_cartographer_reads_lake_and_returns_validated_proposal(
    metadata: TaskMetadata,
    monkeypatch: pytest.MonkeyPatch,
    warehouse: DataClient,
    tmp_path: Path,
) -> None:
    ts = make_anomaly().ts
    with closing(Store(":memory:")) as store:
        with store.cursor() as con:
            con.execute(
                """INSERT INTO orders_hourly (ts, country, platform, category, seller_tier, channel, units)
                   VALUES (?, 'DE', 'web', 'home', '1P', 'search', 200)""",
                [ts],
            )
            con.execute(
                "INSERT INTO partition_status (ts, table_name, country, expected_rows, loaded_rows) VALUES (?, 'orders_hourly', 'DE', 1, 1)",
                [ts],
            )
        LakeWriter(tmp_path).land(store, ts)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        name = next(
            tool.name for tool in info.output_tools if tool.name.endswith("_Submission")
        )
        return ModelResponse(
            parts=[
                ToolCallPart(name, {"edges": [], "notes": "Validated", "unit": "count"})
            ]
        )

    monkeypatch.setattr(
        "eye.agents.cartographer.build_model",
        lambda *args: FunctionModel(respond, model_name="scripted"),
    )
    monkeypatch.setattr(
        "eye.agents.cartographer.price_table",
        lambda *args: PriceTable(
            prices={"scripted": ModelPrice(input_per_mtok=0, output_per_mtok=0)}
        ),
    )
    url = "http://cartographer:8250/"
    app = create_app("cartographer", url, Settings(), lake=tmp_path, data=warehouse)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http,
    ):
        mapped = await send_skill(
            http,
            url,
            "map",
            MapRequest(
                metric=AddMetricRequest(
                    name="orders_x2",
                    expression="orders + orders",
                    description="Twice orders",
                )
            ),
            metadata,
            AddMetricResult,
        )
    assert mapped.status == "done", mapped
    assert mapped.result is not None
    assert mapped.result.metric.name == "orders_x2"
    assert mapped.result.metric.expression == "orders + orders"
    assert mapped.usage.requests == 1
    assert mapped.usage.total_tokens > 0


@pytest.mark.asyncio
async def test_invalid_and_exhausted_requests_do_not_run_agent(
    metadata: TaskMetadata, warehouse: DataClient, model: deque
) -> None:
    url = "http://sentinel:8210/"
    app = create_app("sentinel", url, Settings(), data=warehouse)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        invalid = await send_skill(
            http,
            url,
            "detect",
            MapRequest(
                metric=AddMetricRequest(
                    name="metric", description="Metric", expression="orders"
                )
            ),
            metadata,
            DetectResult,
        )
        other_tenant = await send_skill(
            http,
            url,
            "detect",
            DetectRequest(ts=make_anomaly().ts),
            metadata.model_copy(update={"tenant_id": "other"}),
            DetectResult,
        )
        exhausted = await send_skill(
            http,
            url,
            "detect",
            DetectRequest(ts=make_anomaly().ts),
            metadata.model_copy(
                update={"budget": metadata.budget.model_copy(update={"tokens": 0})}
            ),
            DetectResult,
        )
    assert invalid.status == other_tenant.status == "failed"
    assert exhausted.status == "gave_up"
    assert not model


@pytest.mark.asyncio
async def test_expired_request_does_not_run_agent(
    metadata: TaskMetadata, warehouse: DataClient, model: deque
) -> None:
    url = "http://sentinel:8210/"
    app = create_app("sentinel", url, Settings(), data=warehouse)

    async def expire(request: httpx.Request) -> None:
        if request.method == "POST":
            content = json.loads(request.content)
            content["params"]["metadata"]["budget"]["deadline"] = (
                datetime.now(UTC) - HOUR
            ).isoformat()
            request.stream = httpx.ByteStream(json.dumps(content).encode())
            request.headers.pop("content-length")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), event_hooks={"request": [expire]}
    ) as http:
        result = await send_skill(
            http,
            url,
            "detect",
            DetectRequest(ts=make_anomaly().ts),
            metadata,
            DetectResult,
        )
    assert result.status == "gave_up"
    assert not model
