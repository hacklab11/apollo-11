from collections.abc import Iterator
from contextlib import closing
from unittest.mock import AsyncMock

import duckdb
import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from test_detective import START, anomaly, seed

from agent_runtime import UnpricedModel
from eye.agents.skeptic import Skeptic, _prices
from eye.config import LlmSettings
from eye.registry.loader import load_registry
from eye.sim.store import Store


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
