import json
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import duckdb
import pytest
from factories import make_incident_s7

from eye.agents.sentinel import Sentinel, SentinelSubmission
from eye.config import LlmSettings
from eye.contracts import (
    Anomaly,
    Edge,
    GraphView,
    Incident,
    SentinelDecision,
    TrailStep,
)
from eye.llm.client import LLMClient
from eye.llm.runtime import AgentSpec
from eye.registry.loader import load_registry
from eye.sim.store import Store

TICK = datetime(2026, 11, 27, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/llm"


@contextmanager
def simulation() -> Iterator[duckdb.DuckDBPyConnection]:
    with closing(Store(":memory:")) as store, store.cursor() as con:
        con.execute(
            """
            INSERT INTO orders_hourly
            SELECT ts, country, 'web', 'electronics', 'E01', 'S01', '1P', 'search',
                   250, 5000, 0, 0, 0, 0
            FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ, INTERVAL 1 HOUR) t(ts),
                 unnest(['DE', 'FR']) c(country)
            """,
            [TICK - 28 * 24 * HOUR, TICK + 4 * HOUR],
        )
        con.execute(
            """
            INSERT INTO search_hourly
            SELECT ts, country, platform, category, 1000, 500, 0.85
            FROM orders_hourly
            """
        )
        yield con


@pytest.fixture
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    with simulation() as con:
        yield con


class SearchGraph:
    def view(self, node: str | None = None, depth: int | None = None) -> GraphView:
        assert node is None or (node, depth) == ("search_conversion", 1)
        return GraphView(
            nodes=[{"id": "search_conversion", "impact_weight": 1}, {"id": "orders"}],
            edges=[
                Edge(
                    id="search-orders",
                    src="search_conversion",
                    dst="orders",
                    type="causal_hypothesis",
                    status="hypothesis",
                    confidence=0.8,
                    evidence={},
                    created_by="seed",
                )
            ],
        )


def make_sentinel(con: duckdb.DuckDBPyConnection, case: str = "clean") -> Sentinel:
    if case == "s3":
        con.execute(
            "UPDATE orders_hourly SET units = 125 WHERE country = 'DE' AND ts = ?",
            [TICK],
        )
        con.execute(
            "INSERT INTO change_log VALUES "
            "('ranker', ?, 'search-ranker', 'release', false, '{\"country\":\"DE\"}', 'v7.2', NULL),"
            "('older', ?, 'app-android', 'release', false, '{}', 'older release', NULL)",
            [TICK, TICK - 24 * HOUR],
        )
    elif case == "i2":
        con.execute(
            "UPDATE orders_hourly SET units = 100 WHERE country = 'FR' AND ts = ?",
            [TICK],
        )
        con.execute(
            "INSERT INTO partition_status VALUES ('orders_hourly', 'FR', ?, 100, 40, ?)",
            [TICK, TICK + 6 * HOUR],
        )
    registry = load_registry(con)
    names = ("search_conversion", "orders") if case == "s3" else ("orders",)
    registry = replace(registry, metrics={name: registry.get(name) for name in names})
    return Sentinel(con, registry, graph=SearchGraph() if case == "s3" else None)


def incident_context() -> list[Incident]:
    incident = make_incident_s7()
    return [
        incident,
        incident.model_copy(update={"id": "closed", "status": "resolved"}),
    ]


@pytest.fixture
def replay(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[[str], None], AsyncMock]:
    sdk = Mock()
    sdk.beta.messages.create = AsyncMock(
        side_effect=AssertionError("Replay tried to go live")
    )

    def install(case: str) -> None:
        client = LLMClient(
            LlmSettings(mode="replay", cache_path=FIXTURES / f"sentinel_{case}.jsonl"),
            sdk,
        )
        monkeypatch.setattr("eye.llm.client._client", client)

    return install, sdk.beta.messages.create


@pytest.mark.asyncio
async def test_s3_replay_prefers_de_search_to_downstream_orders(
    world: duckdb.DuckDBPyConnection,
    replay: tuple[Callable[[str], None], AsyncMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    install, network = replay
    install("s3")
    steps: list[TrailStep] = []

    async def trail(step: TrailStep) -> None:
        steps.append(step)

    result = await make_sentinel(world, "s3").run(
        TICK, open_incidents=incident_context(), on_step=trail
    )
    by_metric = {
        item.anomaly.metric: item.action
        for item in result
        if item.anomaly.segment == {"country": "DE"}
    }
    assert by_metric["search_conversion"] == "investigate"
    assert by_metric["orders"] in {"attach", "ignore"}
    tool_steps = {step.tool: step for step in steps if step.action == "tool_call"}
    assert set(tool_steps) == {
        "baseline_compare",
        "check_freshness",
        "get_graph",
        "list_events",
        "submit",
    }
    assert tool_steps["get_graph"].output_summary == "Graph: 2 nodes and 1 edges."
    events = tool_steps["list_events"].output
    assert events is not None
    assert [event["id"] for event in events["events"]] == ["ranker"]
    assert "replay_miss" not in capsys.readouterr().out
    network.assert_not_awaited()


@pytest.mark.asyncio
async def test_i2_replay_forces_defer(
    world: duckdb.DuckDBPyConnection, replay: tuple[Callable[[str], None], AsyncMock]
) -> None:
    install, network = replay
    install("i2")
    steps: list[TrailStep] = []

    async def trail(step: TrailStep) -> None:
        steps.append(step)

    result = await make_sentinel(world, "i2").run(TICK, on_step=trail)
    submitted = next(step for step in steps if step.tool == "submit")
    assert submitted.output is not None
    assert submitted.output["decisions"][0]["action"] == "investigate"
    fr = next(item for item in result if item.anomaly.segment == {"country": "FR"})
    assert fr.action == "defer"
    assert "40.00%" in fr.reason
    assert fr.attach_to_incident is None
    network.assert_not_awaited()


@pytest.mark.asyncio
async def test_quiet_tick_does_not_call_the_llm(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = AsyncMock(side_effect=AssertionError("LLM called on a quiet tick"))
    monkeypatch.setattr("eye.agents.sentinel.run_agent", run)
    assert await make_sentinel(world).run(TICK) == []
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("second_hour", "action"), [(1, "investigate"), (2, "watch")])
async def test_watch_only_escalates_on_consecutive_hours(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    second_hour: int,
    action: str,
) -> None:
    world.execute(
        "UPDATE orders_hourly SET units = 225 WHERE country = 'DE' AND ts IN (?, ?)",
        [TICK, TICK + second_hour * HOUR],
    )
    seen: list[bool] = []

    async def watch(spec: AgentSpec, content: str, *_: object) -> SentinelSubmission:
        assert spec.effort == "low"
        assert spec.max_steps == 4
        rows = json.loads(content)["candidates"]
        de = next(row for row in rows if row["anomaly"]["segment"] == {"country": "DE"})
        seen.append(de["watched_last_tick"])
        anomaly = Anomaly.model_validate(de["anomaly"])
        return SentinelSubmission(
            decisions=[
                SentinelDecision(
                    anomaly=anomaly, action="watch", reason="Single-hour drop"
                )
            ]
        )

    monkeypatch.setattr("eye.agents.sentinel.run_agent", watch)
    sentinel = make_sentinel(world)
    assert [item.action for item in await sentinel.run(TICK)] == ["watch"]
    assert [item.action for item in await sentinel.run(TICK + second_hour * HOUR)] == [
        action
    ]
    assert seen == [False, second_hour == 1]


@pytest.mark.asyncio
async def test_trim_low_priority_candidates_and_discard_unknown_decisions(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = make_sentinel(world, "s3")
    full: list[Anomaly] = []

    def candidates(content: str) -> list[Anomaly]:
        payload = json.loads(content)
        return [Anomaly.model_validate(row["anomaly"]) for row in payload["candidates"]]

    async def collect(spec: AgentSpec, content: str, *_: object) -> SentinelSubmission:
        full.extend(candidates(content))
        return SentinelSubmission(decisions=[])

    monkeypatch.setattr("eye.agents.sentinel.run_agent", collect)
    await sentinel.run(TICK)
    assert [item.priority for item in full] == sorted(
        (item.priority for item in full), reverse=True
    )
    monkeypatch.setattr("eye.agents.sentinel._MAX_CONTEXT_CHARS", 1300)

    async def submit(spec: AgentSpec, content: str, *_: object) -> SentinelSubmission:
        assert len(content) <= 1300
        sent = candidates(content)
        assert 0 < len(sent) < len(full)
        assert sent == full[: len(sent)]
        changed = sent[0].model_copy(update={"actual": 999})
        return SentinelSubmission(
            decisions=[
                SentinelDecision(anomaly=item, action="investigate", reason="Test")
                for item in (full[-1], changed, changed)
            ]
        )

    monkeypatch.setattr("eye.agents.sentinel.run_agent", submit)
    result = await sentinel.run(TICK)
    assert [item.anomaly for item in result] == full[:1]
