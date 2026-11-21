import os
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import duckdb
import httpx
import pytest
from anthropic.types.beta import BetaMessage
from fastapi import FastAPI

from eye.a2a_client import A2AClient
from eye.agents.detective import Detective, _Submission
from eye.api.data_api import create_router
from eye.config import A2aSettings, LlmSettings, Settings
from eye.contracts import (
    Anomaly,
    Contribution,
    Edge,
    Evidence,
    Hypothesis,
    TrailStep,
    Verdict,
)
from eye.graph.store import GraphStore
from eye.llm.client import BudgetExceeded, LLMClient
from eye.llm.runtime import AgentSpec
from eye.registry.loader import Registry, load_registry
from eye.sim.store import Store
from eye.specialists.app import create_app
from eye.specialists.tools import DataClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/llm"
START = datetime(2026, 11, 27, 3, tzinfo=UTC)
HOUR = timedelta(hours=1)
END = START + 2 * HOUR


@pytest.fixture
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    with closing(Store(":memory:")) as store, store.cursor() as con:
        yield con


def seed(con: duckdb.DuckDBPyConnection, case: str) -> None:
    # The scenario injector is independent of these tests; seed its observable signatures.
    con.execute(
        """
        CREATE TEMP TABLE hours AS SELECT ts, ts >= ? AS broken
        FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ, INTERVAL 1 HOUR) AS t(ts)
        """,
        [START, START - timedelta(weeks=4), END + HOUR],
    )
    if case == "s3":
        con.execute("""
            INSERT INTO search_hourly
            SELECT ts, country, platform, 'electronics', 1024,
                   CASE WHEN broken AND country = 'DE' THEN 256 ELSE 512 END,
                   CASE WHEN broken AND country = 'DE' THEN 0.4375 ELSE 0.875 END
            FROM hours, unnest(['DE','FR']) c(country),
                 unnest(['android','ios','web']) p(platform);
            INSERT INTO orders_hourly
            SELECT ts, country, platform, category, 'E01', 'S01', '1P', 'search',
                   CASE WHEN relevance < 0.5 THEN 64 ELSE 128 END,
                   CASE WHEN relevance < 0.5 THEN 1280 ELSE 2560 END, 0, 0, 0, 0
            FROM search_hourly;
            INSERT INTO change_log VALUES
              ('ranker', '2026-11-27T03:00:00Z', 'search-ranker', 'release', false,
               '{"country":"DE"}', 'search-ranker v7.2', NULL),
              ('fr-ui', '2026-11-27T03:00:00Z', 'search-ui', 'release', false,
               '{"country":"FR"}', 'search-ui v3.0', NULL),
              ('android', '2026-11-27T01:00:00Z', 'app-android', 'release', false,
               '{}', 'Android app release', NULL);
            """)
    else:
        con.execute("""
            INSERT INTO checkout_hourly
            SELECT ts, 'DE', platform, 8192,
                   CASE WHEN broken AND platform <> 'ios' THEN 512 ELSE 4096 END,
                   CASE WHEN broken AND platform <> 'ios' THEN 480 ELSE 3840 END
            FROM hours, unnest(['web','android','ios']) p(platform);
            INSERT INTO payments_hourly
            SELECT ts, country, platform, 'PSP-2', payment_attempts,
                   payment_attempts * 0.9375, CASE WHEN ts >= '2026-11-27T02:00:00Z' THEN 820 ELSE 200 END
            FROM checkout_hourly;
            INSERT INTO client_errors_hourly
            SELECT ts, platform,
                   CASE WHEN broken THEN 'v5.8' ELSE 'v5.7' END,
                   'checkout', 'TypeError', CASE WHEN broken THEN 3584 ELSE 0 END
            FROM hours, unnest(['web','android']) p(platform);
            INSERT INTO app_components VALUES
              ('web','checkout','checkout-web','v5.8'),
              ('android','checkout','checkout-web','v5.8'),
              ('ios','checkout','native-ios','v3.2');
            INSERT INTO orders_hourly
            SELECT ts, country, platform, 'electronics', 'E01', 'S01', '1P', 'search',
                   successes, successes * 32, 0, 0, 0, 0 FROM payments_hourly;
            INSERT INTO change_log VALUES
              ('black-friday', '2026-11-27T00:00:00Z', 'marketing', 'campaign', true,
                '{}', 'Black Friday campaign', '{"gmv":"+35%","orders":"+35%"}'),
              ('checkout', '2026-11-27T03:00:00Z', 'checkout-web', 'release', false,
                '{"platform":["web","android"]}', 'checkout-web v5.8', NULL),
              ('android', '2026-11-27T01:00:00Z', 'app-android', 'release', false,
                '{"platform":"android"}', 'v9.14 rollout (20%)', NULL);
            INSERT INTO external_status VALUES
              ('psp2','2026-11-27T02:00:00Z','PSP-2','degraded','Elevated latency');
            """)


def anomaly(case: str) -> Anomaly:
    value, expected = (0.0625, 0.125) if case == "s3" else (288000, 691200)
    return Anomaly(
        id=case,
        ts=END - HOUR,
        metric="search_conversion" if case == "s3" else "gmv",
        segment={"country": "DE"},
        kind="divergent_segment",
        actual=value,
        expected=expected,
        delta=value - expected,
        delta_pct=value / expected - 1,
        z=-10,
        window_hours=2,
        impact_eur_per_hour=0 if case == "s3" else 201600,
        completeness=1,
        priority=10,
    )


def make_graph(registry: Registry, case: str) -> GraphStore:
    graph = GraphStore(None)
    for metric in registry.metrics.values():
        graph.add_metric(metric)
    pairs = (
        [("search_relevance", "search_conversion"), ("search_ctr", "search_conversion")]
        if case == "s3"
        else [
            ("orders", "gmv"),
            ("cart_to_checkout_rate", "orders"),
            ("payment_success_rate", "orders"),
            ("client_error_rate", "cart_to_checkout_rate"),
        ]
    )
    for src, dst in pairs:
        graph.add_edge(
            Edge(
                id=f"{src}:{dst}",
                src=src,
                dst=dst,
                type="causal_hypothesis",
                status="hypothesis",
                confidence=0.5,
                evidence={},
                created_by="seed",
            )
        )
    return graph


async def scenario(
    case: str,
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    live: bool,
    cache_dir: Path = FIXTURES,
) -> None:
    seed(world, case)
    registry = load_registry(world)
    mode = "record" if live else "replay"
    sdk = None if live else Mock()
    if sdk is not None:
        sdk.beta.messages.create = AsyncMock(
            side_effect=AssertionError("Replay tried to go live")
        )
    clients = {
        agent: LLMClient(
            LlmSettings(mode=mode, cache_path=cache_dir / f"{agent}_{case}.jsonl"), sdk
        )
        for agent in ("detective", "skeptic")
    }

    class Transport:
        async def create(self, request: dict) -> BetaMessage:
            prompt = request["system"][0]["text"]
            return await clients[
                "skeptic" if prompt.startswith("You are the Skeptic") else "detective"
            ].create(request)

    monkeypatch.setattr("eye.llm.client._client", Transport())
    core = FastAPI()
    core.include_router(create_router(world))
    settings = Settings(
        a2a=A2aSettings(
            growth_url="http://specialists/growth",
            marketplace_url="http://specialists/marketplace",
            discovery_url="http://specialists/discovery",
            selleros_url="http://specialists/selleros",
        )
    )
    steps: list[TrailStep] = []

    async def trail(step: TrailStep) -> None:
        steps.append(step)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core)) as data:
        app = create_app(settings, DataClient(data, "http://core/data"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            detective = Detective(
                world,
                registry,
                make_graph(registry, case),
                a2a=A2AClient(settings.a2a, http_client=http),
            )
            result = await detective.investigate(
                anomaly(case), trail, investigation_id=f"inv-{case}"
            )

    root = result.root_cause
    print(
        f"{case.upper()}: {result.verdict}, root={root.root_node if root else None}; {result.summary}"
    )
    print("Trail:", ", ".join(step.tool or step.action for step in steps))
    assert result.verdict == "CONFIRMED", (result.summary, result.open_questions)
    assert root is not None and root.cause_type == "release"
    assert root.root_node == (
        "search_relevance" if case == "s3" else "cart_to_checkout_rate"
    )
    assert len({e.group for e in root.evidence_for}) >= 2
    assert sum(row.share for row in result.contributions) == pytest.approx(1, abs=0.02)
    assert [step.n for step in steps] == list(range(1, len(steps) + 1))
    assert any(step.actor == "skeptic" for step in steps)
    if case == "s7":
        assert any(
            step.actor == "skeptic"
            and step.tool == "planned_effect_check"
            and step.output is not None
            and step.output["verdict"] == "opposite"
            for step in steps
        )
    assert any(
        step.action == "a2a_call" and step.output and step.output["answered"]
        for step in steps
    )
    if sdk is not None:
        sdk.beta.messages.create.assert_not_awaited()


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["s3", "s7"])
async def test_record_fixture(
    case: str,
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_API_KEY is required to record live fixtures")
    await scenario(case, world, monkeypatch, live=True, cache_dir=tmp_path)
    for agent in ("detective", "skeptic"):
        filename = f"{agent}_{case}.jsonl"
        (FIXTURES / filename).write_bytes((tmp_path / filename).read_bytes())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["s3", "s7"])
async def test_scenario_replay(
    case: str,
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await scenario(case, world, monkeypatch, live=False)
    assert "replay_miss" not in capsys.readouterr().out


def hypothesis() -> Hypothesis:
    return Hypothesis(
        id="H1",
        statement="ranker release",
        cause_type="release",
        root_node="search_relevance",
        segment={"country": "DE"},
        first_change_ts=START,
        explained_share=0.9,
        status="supported",
        evidence_for=[
            Evidence(
                group="data",
                source="baseline_compare",
                step_id="baseline_compare",
                summary="Conversion fell by half.",
            ),
            Evidence(
                group="data",
                source="compare_segments",
                step_id="compare_segments",
                summary="DE fell while FR was steady.",
            ),
        ],
    )


def submission(root: Hypothesis, verdict: Verdict = "CONFIRMED") -> _Submission:
    return _Submission(
        verdict=verdict,
        root_cause=root,
        hypotheses=[root],
        summary="Ranker release.",
        contributions=[
            Contribution(factor="search_relevance", kind="rate", delta=-0.05, share=0.8)
        ],
        open_questions=[],
        suggested_actions=[],
    )


@pytest.mark.asyncio
async def test_unavailable_specialist_and_weak_confirmation(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    steps: list[TrailStep] = []

    async def trail(step: TrailStep) -> None:
        steps.append(step)

    async def run(spec: AgentSpec, *_: object) -> _Submission:
        ask = next(t for t in spec.tools if t.name == "ask_specialist")
        response = await ask.fn(
            ask.input_model(
                agent="discovery",
                question="What changed?",
                metric="search_relevance",
                segment={"country": "DE"},
            )
        )
        assert response.answered is False
        return submission(hypothesis())

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no connection", request=request)

    monkeypatch.setattr("eye.agents.detective.run_agent", run)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        detective = Detective(
            world, registry, make_graph(registry, "s3"), a2a=A2AClient(http_client=http)
        )
        result = await detective.investigate(anomaly("s3"), trail)
    assert result.verdict == "HYPOTHESIS"
    assert "found 1" in " ".join(result.open_questions)
    assert result.contributions[-1].kind == "residual"
    assert result.contributions[-1].delta == pytest.approx(-0.0125)
    assert any(
        s.action == "note" and "agent unavailable" in s.output_summary for s in steps
    )
    assert steps[-1].action == "decision"


@pytest.mark.asyncio
async def test_budget_preserves_current_board(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    root = hypothesis()

    async def run(spec: AgentSpec, *_: object) -> _Submission:
        update = next(t for t in spec.tools if t.name == "update_hypotheses")
        update.fn(update.input_model(hypotheses=[root]))
        raise BudgetExceeded("agent turn budget exhausted")

    monkeypatch.setattr("eye.agents.detective.run_agent", run)
    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert result.verdict == "HYPOTHESIS"
    assert result.stop_reason == "budget"
    assert result.hypotheses == [root]


@pytest.mark.asyncio
async def test_loop_fills_missing_fields_from_measured_change_points(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    world.execute(
        "UPDATE search_hourly SET relevance = 0.4375 WHERE ts = ? AND country = 'DE'",
        [START - HOUR],
    )
    registry = load_registry(world)
    graph = make_graph(registry, "s3")
    graph.add_edge(
        Edge(
            id="feedback",
            src="search_conversion",
            dst="search_relevance",
            type="causal_hypothesis",
            status="hypothesis",
            confidence=0.5,
            evidence={},
            created_by="seed",
        )
    )
    monkeypatch.setattr(
        "eye.agents.detective.run_agent",
        AsyncMock(return_value=submission(hypothesis(), "HYPOTHESIS")),
    )
    result = await Detective(world, registry, graph).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert result.loop is not None and set(result.loop) == {
        "search_conversion",
        "search_relevance",
    }
    assert result.trigger == "H1"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["DATA_ISSUE", "INNOCENT", "MIXED"])
async def test_other_unsubstantiated_verdicts_are_downgraded(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    verdict: Verdict,
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    monkeypatch.setattr(
        "eye.agents.detective.run_agent",
        AsyncMock(return_value=submission(hypothesis(), verdict)),
    )
    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert result.verdict == "HYPOTHESIS"
    assert result.open_questions


@pytest.mark.asyncio
async def test_data_issue_requires_stale_entry_window(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(world, "s3")
    world.execute(
        "INSERT INTO partition_status VALUES ('search_hourly', 'DE', ?, 100, 40, ?)",
        [START, START + HOUR],
    )
    registry = load_registry(world)
    monkeypatch.setattr(
        "eye.agents.detective.run_agent",
        AsyncMock(return_value=submission(hypothesis(), "DATA_ISSUE")),
    )
    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert result.verdict == "DATA_ISSUE"
    assert result.stop_reason == "data_issue"
