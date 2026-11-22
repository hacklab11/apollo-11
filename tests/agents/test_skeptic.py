from collections.abc import Iterator
from contextlib import closing
from unittest.mock import AsyncMock, Mock

import duckdb
import httpx
import pytest
from pydantic_ai import Tool, models
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from test_detective import START, anomaly, seed

from agent_runtime import UnpricedModel
from eye.agents.investigation_tools import _ControlInput, _EventsInput, _MetricInput
from eye.agents.skeptic import (
    Skeptic,
    _PlanInput,
    _prices,
    baseline_compare,
    check_freshness,
    compare_segments,
    list_events,
    planned_effect_check,
)
from eye.config import LlmSettings
from eye.registry.loader import load_registry
from eye.sim.store import Store
from eye.warehouse.client import DataClient


@pytest.fixture(autouse=True)
def no_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)


@pytest.fixture
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    with closing(Store(":memory:")) as store, store.cursor() as con:
        seed(con, "s7")
        yield con


def test_configured_prices() -> None:
    settings = LlmSettings(
        prices={"custom:free": {"input_per_mtok": 0, "output_per_mtok": 0}}
    )
    prices = _prices(settings)
    assert prices.cost("custom:free", RequestUsage(input_tokens=100)) == 0
    with pytest.raises(UnpricedModel):
        prices.cost("skeptic", RequestUsage())
    with pytest.raises(UnpricedModel):
        prices.cost("unconfigured:free", RequestUsage())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_events", {}),
        ("planned_effect_check", {"event_id": "black-friday"}),
        ("check_freshness", {"metric": "gmv"}),
        ("baseline_compare", {"metric": "gmv"}),
        (
            "compare_segments",
            {"metric": "gmv", "dim": "country", "value_a": "DE", "value_b": "FR"},
        ),
    ],
)
async def test_warehouse_failure_is_recoverable(
    monkeypatch: pytest.MonkeyPatch, tool: str, args: dict
) -> None:
    requested = False

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal requested
        if not requested:
            requested = True
            return ModelResponse(parts=[ToolCallPart(tool, args)])
        retries = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.outcome == "retried"
        ]
        assert retries and "Warehouse request failed" in str(retries[-1].content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit",
                    {
                        "verdict": "not_innocent",
                        "explained_share": 0,
                        "reasons": ["Warehouse unavailable"],
                        "evidence": [],
                    },
                )
            ]
        )

    monkeypatch.setattr(
        "eye.agents.skeptic._model",
        lambda _: FunctionModel(respond, model_name="claude-opus-5-5"),
    )
    callback = AsyncMock()
    with closing(
        DataClient(
            "http://warehouse",
            transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        )
    ) as data:
        result = await Skeptic(data=data).review(anomaly("s7"), "inv", callback)
    assert result.reasons == ["Warehouse unavailable"]
    steps = [
        call.args[0] for call in callback.call_args_list if call.args[0].tool == tool
    ]
    assert len(steps) == 1
    assert steps[0].output is None
    assert "failed" in steps[0].output_summary


@pytest.mark.parametrize(
    ("fn", "schema"),
    [
        (list_events, _EventsInput),
        (planned_effect_check, _PlanInput),
        (check_freshness, _MetricInput),
        (baseline_compare, _MetricInput),
        (compare_segments, _ControlInput),
    ],
)
def test_flattened_tool_schema(fn: object, schema: type) -> None:
    generated = Tool(fn).function_schema.json_schema
    assert (
        generated["properties"].keys()
        == schema.model_json_schema()["properties"].keys()
    )
    assert "args" not in generated["properties"]


@pytest.mark.asyncio
async def test_agent_reused_with_fresh_trail_state(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit",
                    {
                        "verdict": "not_innocent",
                        "explained_share": 0,
                        "reasons": [],
                        "evidence": [],
                    },
                )
            ]
        )

    model = FunctionModel(respond, model_name="claude-opus-5-5")
    factory = Mock(return_value=model)
    monkeypatch.setattr("eye.agents.skeptic._model", factory)
    skeptic = Skeptic(world, load_registry(world))
    agent = skeptic._agent
    for run_id in ("first", "second"):
        callback = AsyncMock()
        await skeptic.review(anomaly("s7"), run_id, callback)
        assert skeptic._agent is agent
        steps = [call.args[0] for call in callback.call_args_list]
        assert [step.n for step in steps] == [1, 2]
        assert all(step.investigation_id == run_id for step in steps)
    assert factory.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "plan",
        "seasonal",
        "empty",
        "data_issue",
        "loop",
        "invalid_plan",
        "gave_up",
        "needs_input",
    ],
)
async def test_review(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    calls: list[tuple[str, dict]] = []
    expected = "not_innocent"
    if case == "plan":
        world.execute(
            "UPDATE change_log SET expected_effect=? WHERE id='black-friday'",
            ['{"gmv":"-58%"}'],
        )
        calls = [
            ("list_events", {"planned": False}),
            ("planned_effect_check", {"event_id": "black-friday"}),
        ]
        expected = "innocent"
    elif case == "seasonal":
        world.execute("UPDATE orders_hourly SET gmv=115200 WHERE ts >= ?", [START])
        calls = [("baseline_compare", {"metric": "gmv", "segment": {"country": "DE"}})]
        expected = "innocent"
    elif case == "data_issue":
        world.execute(
            "INSERT INTO partition_status VALUES ('orders_hourly', 'DE', ?, 100, 40, ?)",
            [START, START],
        )
        expected = "data_issue"
    elif case == "loop":
        calls = [("list_events", {})] * 4
    elif case == "invalid_plan":
        calls = [("planned_effect_check", {"event_id": "checkout"})]
    elif case == "gave_up":
        calls = [("GaveUp", {"reason": "No evidence"})]
    elif case == "needs_input":
        calls = [("NeedsInput", {"question": "Which plan?"})]
    calls.append(
        (
            "submit",
            {
                "verdict": "innocent",
                "explained_share": 1,
                "reasons": [],
                "evidence": [],
            },
        )
    )
    turns = iter(calls)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert {tool.name for tool in info.function_tools} == {
            "baseline_compare",
            "check_freshness",
            "compare_segments",
            "list_events",
            "planned_effect_check",
        }
        assert "submit" in {tool.name for tool in info.output_tools}
        name, args = next(turns)
        return ModelResponse(parts=[ToolCallPart(name, args)])

    monkeypatch.setattr(
        "eye.agents.skeptic._model",
        lambda _: FunctionModel(respond, model_name="claude-opus-5-5"),
    )
    callback = AsyncMock()
    result = await Skeptic(world, load_registry(world)).review(
        anomaly("s7"), "inv", callback
    )
    assert result.verdict == expected
    assert (result.explained_share > 0) == (expected == "innocent")
    all_steps = [call.args[0] for call in callback.call_args_list]
    assert [step.n for step in all_steps] == list(range(1, len(all_steps) + 1))
    steps = [step for step in all_steps if step.action == "tool_call"]
    assert steps
    assert all(
        step.investigation_id == "inv" and step.actor == "skeptic" for step in steps
    )
    if case == "plan":
        assert [step.tool for step in steps] == [
            "list_events",
            "planned_effect_check",
            "submit",
        ]
        assert steps[1].input == {"event_id": "black-friday"}
        assert steps[1].output["verdict"] == "matches"
    if case in {"loop", "gave_up", "needs_input"}:
        assert result.reasons
        assert result.evidence == []


def test_skeptic_starts_without_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    Skeptic(data=Mock(), freshness=Mock(), llm=LlmSettings())
