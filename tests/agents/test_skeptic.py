from collections.abc import Iterator
from contextlib import closing
from unittest.mock import AsyncMock

import duckdb
import pytest
from test_detective import START, anomaly, hypothesis, seed

from eye.agents.skeptic import Skeptic, SkepticResult
from eye.llm.runtime import AgentSpec
from eye.registry.loader import load_registry
from eye.sim.store import Store


@pytest.fixture
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    with closing(Store(":memory:")) as store, store.cursor() as con:
        seed(con, "s7")
        yield con


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("planned", "comparison", "verdict"),
    [
        ("-58%", "matches", "innocent"),
        ("+35%", "opposite", "not_innocent"),
        ("-20%", "exceeds", "not_innocent"),
    ],
)
async def test_only_matching_plan_can_establish_innocence(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    planned: str,
    comparison: str,
    verdict: str,
) -> None:
    world.execute(
        "UPDATE change_log SET expected_effect=? WHERE id='black-friday'",
        ['{"gmv":"' + planned + '"}'],
    )

    async def run(spec: AgentSpec, *_: object) -> SkepticResult:
        assert spec.effort == "medium"
        assert spec.max_steps == 6
        assert {t.name for t in spec.tools} == {
            "baseline_compare",
            "check_freshness",
            "compare_segments",
            "list_events",
            "planned_effect_check",
        }
        tools = {t.name: t for t in spec.tools}
        events = tools["list_events"]
        output = events.fn(events.input_model(planned=False))
        assert [event.id for event in output.events] == ["black-friday"]
        plan = tools["planned_effect_check"]
        assert plan.fn(plan.input_model(event_id="black-friday")).verdict == comparison
        with pytest.raises(ValueError, match="outside"):
            plan.fn(plan.input_model(event_id="checkout"))
        return SkepticResult(
            verdict="innocent", explained_share=1, reasons=[], evidence=[]
        )

    monkeypatch.setattr("eye.agents.skeptic.run_agent", run)
    skeptic = Skeptic(world, load_registry(world))
    result = await skeptic.review(
        anomaly("s7"), "inv", AsyncMock(), hypothesis=hypothesis()
    )
    assert result.verdict == verdict
    assert (result.explained_share > 0) == (verdict == "innocent")


@pytest.mark.asyncio
@pytest.mark.parametrize("seasonal", [False, True])
@pytest.mark.parametrize("check_plan", [False, True])
async def test_seasonality_must_be_checked_on_entry(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    seasonal: bool,
    check_plan: bool,
) -> None:
    if seasonal:
        world.execute("UPDATE orders_hourly SET gmv=115200 WHERE ts >= ?", [START])

    async def run(spec: AgentSpec, *_: object) -> SkepticResult:
        baseline = next(t for t in spec.tools if t.name == "baseline_compare")
        observed = baseline.fn(
            baseline.input_model(metric="gmv", segment={"country": "DE"})
        )
        assert observed.points[0].ts == START
        assert len(observed.points) == 2
        if check_plan:
            plan = next(t for t in spec.tools if t.name == "planned_effect_check")
            outcome = plan.fn(plan.input_model(event_id="black-friday"))
            assert outcome.verdict != "matches"
        return SkepticResult(
            verdict="innocent", explained_share=1, reasons=[], evidence=[]
        )

    monkeypatch.setattr("eye.agents.skeptic.run_agent", run)
    skeptic = Skeptic(world, load_registry(world))
    result = await skeptic.review(anomaly("s7"), "inv", AsyncMock())
    assert result.verdict == ("innocent" if seasonal else "not_innocent")
