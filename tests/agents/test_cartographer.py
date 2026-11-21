import asyncio
import copy
import json
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import duckdb
import pytest
from anthropic.types.beta import BetaMessage

from eye.agents.cartographer import (
    Cartographer,
    SeedDecision,
    SeedSubmission,
    Submission,
)
from eye.agents.sentinel import Sentinel
from eye.config import LlmSettings, Settings
from eye.contracts import AddMetricRequest, Edge, EdgeProposal, MetricDef, TrailStep
from eye.graph.store import GraphStore
from eye.incidents.store import IncidentStore
from eye.llm.client import BudgetExceeded, LLMClient
from eye.orchestrator import Orchestrator
from eye.registry.engine import MetricEngine
from eye.registry.loader import Identity, Registry
from eye.tools.sqlguard import check

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/llm"
SQL = "SELECT ts, country, category, seller_tier, refunds AS num, units AS den FROM orders_hourly"
FEE_SQL = """SELECT ts, country, platform, category, seller_tier, channel,
SUM(fee_revenue) AS num, SUM(units) AS den FROM orders_hourly
GROUP BY ts, country, platform, category, seller_tier, channel"""


def fee_world(cartographer: Cartographer, *, mixed: bool = False) -> None:
    con = cartographer.engine._con
    con.execute("ALTER TABLE orders_hourly ADD COLUMN platform VARCHAR DEFAULT 'web'")
    con.execute("ALTER TABLE orders_hourly ADD COLUMN channel VARCHAR DEFAULT 'search'")
    con.execute("ALTER TABLE orders_hourly ADD COLUMN gmv DOUBLE")
    con.execute("ALTER TABLE orders_hourly ADD COLUMN fee_revenue DOUBLE")
    con.execute(
        "UPDATE orders_hourly SET gmv = units * (50 + 10 * sin(epoch(ts) / 13700) + extract(day FROM ts) + CASE WHEN country = 'DE' THEN 4 ELSE 0 END)"
    )
    con.execute("UPDATE orders_hourly SET fee_revenue = gmv * 0.12")
    if mixed:
        con.execute(
            "UPDATE orders_hourly SET seller_tier = CASE WHEN country = 'FR' THEN '1P' ELSE 'small_3P' END, units = CASE WHEN country = 'FR' THEN 80 + 40 * sin(epoch(ts) / 13700) + extract(day FROM ts) ELSE 100 END"
        )
        con.execute(
            "UPDATE orders_hourly SET gmv = units * CASE WHEN seller_tier = '1P' THEN 100 ELSE 40 END"
        )
        con.execute(
            "UPDATE orders_hourly SET fee_revenue = CASE WHEN seller_tier = '1P' THEN 0 ELSE gmv * 0.08 END"
        )
    dims = ["country", "platform", "category", "seller_tier", "channel"]
    registry = cartographer.engine._registry
    for name, description, unit, value in [
        ("orders", "Total units ordered.", "count", "units"),
        ("gmv", "Total value of merchandise sold, in euros.", "eur", "gmv"),
        (
            "fee_revenue",
            "Total marketplace fees earned from orders, in euros.",
            "eur",
            "fee_revenue",
        ),
        (
            "aov",
            "Merchandise value in euros per unit ordered, calculated as GMV divided by orders.",
            "eur",
            "gmv",
        ),
    ]:
        metric = MetricDef(
            name=name,
            description=description,
            unit=unit,
            domain="orders",
            kind="sql",
            sql=f"SELECT ts, country, platform, category, seller_tier, channel, {value} AS num"
            + (", units AS den" if name == "aov" else "")
            + " FROM orders_hourly",
            dimensions=dims,
        )
        registry.metrics[name] = metric
        registry.source_tables[name] = ("orders_hourly",)
        registry.has_den[name] = name == "aov"
        cartographer.graph.add_metric(metric)
    registry.identities.append(Identity(id="F2", text="gmv = orders * aov"))
    cartographer.engine.invalidate()


@pytest.fixture
def cartographer() -> Cartographer:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders_hourly AS SELECT ts, country, 'electronics' AS category, 'small_3P' AS seller_tier, 100.0::DOUBLE AS units, 2.0::DOUBLE AS refunds FROM range(TIMESTAMPTZ '2026-01-01', TIMESTAMPTZ '2026-01-29', INTERVAL 1 HOUR) t(ts), unnest(['DE', 'FR']) c(country)"
    )
    con.execute(
        "CREATE TABLE partition_status(table_name VARCHAR, country VARCHAR, ts TIMESTAMPTZ, loaded_rows INTEGER, expected_rows INTEGER)"
    )
    metrics = {
        name: MetricDef(
            name=name,
            description=description,
            unit="count",
            domain="orders",
            kind="sql",
            sql=f"SELECT ts, country, category, seller_tier, {column} AS num FROM orders_hourly",
            dimensions=["country", "category", "seller_tier"],
        )
        for name, column, description in [
            ("orders", "units", "Total units ordered"),
            ("refunds", "refunds", "Total number of refunds"),
        ]
    }
    registry = Registry(
        metrics,
        {n: ("orders_hourly",) for n in metrics},
        {n: False for n in metrics},
        [],
        [],
    )
    graph = GraphStore(None)
    for metric in metrics.values():
        graph.add_metric(metric)
    yield Cartographer(MetricEngine(con, registry), graph)
    con.close()


def install(monkeypatch: pytest.MonkeyPatch, case: str, live: bool) -> Mock:
    sdk = Mock()
    sdk.beta.messages.create = AsyncMock(
        side_effect=AssertionError("Replay tried to go live")
    )
    settings = LlmSettings(
        mode="record" if live else "replay",
        cache_path=FIXTURES / f"cartographer_{case}.jsonl",
    )
    monkeypatch.setattr(
        "eye.llm.client._client",
        LLMClient(settings) if live else LLMClient(settings, sdk),
    )
    return sdk.beta.messages.create


async def placement(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch, case: str, live: bool
) -> None:
    network = install(monkeypatch, case, live)
    if case in {"fee_per_order", "fee_per_order_mix"}:
        fee_world(cartographer, mixed=case == "fee_per_order_mix")
        req = AddMetricRequest(
            name="fee_per_order",
            description="Marketplace fee revenue in euros divided by units ordered.",
            sql=FEE_SQL,
        )
    else:
        req = AddMetricRequest(
            name="refund_rate" if case == "refund" else "order_copy",
            description="Number of refunds divided by orders"
            if case == "refund"
            else "Duplicate of total orders",
            sql=SQL
            if case == "refund"
            else "SELECT ts, country, category, seller_tier, units AS num FROM orders_hourly",
        )
    steps: list[TrailStep] = []

    async def trail(step: TrailStep) -> None:
        steps.append(step)

    start = time.monotonic()
    result = await cartographer.add_metric(req, on_step=trail)
    assert time.monotonic() - start < 60
    assert len([s for s in steps if s.action == "note"]) == 7
    if case == "refund":
        assert any(
            e.identity == "refund_rate = refunds / orders" and e.status == "validated"
            for e in result.accepted
        )
        assert {e.dimension for e in result.accepted if e.type == "breakdown"} == {
            "country",
            "category",
            "seller_tier",
        }
    elif case in {"fee_per_order", "fee_per_order_mix"}:
        formula = [
            e
            for e in result.accepted
            if e.identity == "fee_revenue = orders * fee_per_order"
        ]
        assert {e.src for e in formula if e.status == "validated"} == {
            "orders",
            "fee_per_order",
        }
        assert {
            e.dimension
            for e in result.accepted
            if e.type == "breakdown" and e.status == "validated"
        } == {"country", "platform", "category", "seller_tier", "channel"}
        assert any(
            e.src == "aov"
            and e.dst == "fee_per_order"
            and e.type == "causal_hypothesis"
            and e.status == "hypothesis"
            and e.lag_hours == 0
            for e in result.accepted
        )
        if case == "fee_per_order_mix":
            edge = next(
                e
                for e in result.accepted
                if e.src == "aov" and e.type == "causal_hypothesis"
            )
            assert edge.evidence["corr"] < -0.3
            assert edge.evidence["n"] > 0
    else:
        assert result.ambiguous
        assert "duplicate" in result.notes.lower()
    assert cartographer.engine._registry.get(req.name).added_by == "user"
    if not live:
        network.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["refund", "duplicate", "fee_per_order", "fee_per_order_mix"]
)
async def test_replay(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    await placement(cartographer, monkeypatch, case, False)


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["refund", "duplicate", "fee_per_order", "fee_per_order_mix"]
)
async def test_record(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    await placement(cartographer, monkeypatch, case, True)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE orders_hourly",
        "SELECT 1; SELECT 2",
        "SELECT * FROM scenario_log",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM orders_hourly, scenario_log",
        "ATTACH 'foo'",
        "COPY orders_hourly TO 'foo'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT * FROM other",
    ],
)
def test_guard_rejects(sql: str) -> None:
    with pytest.raises(ValueError):
        check(sql, {"orders_hourly", "scenario_log"})


def test_guard_cte() -> None:
    check("WITH x AS (SELECT * FROM orders_hourly) SELECT * FROM x", {"orders_hourly"})


@pytest.mark.asyncio
async def test_guard_does_not_mutate(cartographer: Cartographer) -> None:
    before = cartographer.graph.view()
    with pytest.raises(ValueError):
        await cartographer.add_metric(
            AddMetricRequest(
                name="bad_metric", description="bad", sql="DROP TABLE orders_hourly"
            )
        )
    assert cartographer.graph.view() == before
    assert (
        cartographer.engine._con.execute(
            "SELECT count(*) FROM orders_hourly"
        ).fetchone()[0]
        == 1344
    )


def test_cte_names_do_not_escape_subqueries() -> None:
    with pytest.raises(ValueError, match="Table is not allowed: private_table"):
        check(
            "SELECT * FROM (WITH private_table AS (SELECT * FROM orders_hourly) SELECT * FROM private_table) x, private_table",
            {"orders_hourly"},
        )


@pytest.mark.asyncio
async def test_empty_and_invalid_columns(cartographer: Cartographer) -> None:
    with pytest.raises(ValueError, match="ts and num"):
        await cartographer.add_metric(
            AddMetricRequest(
                name="bad_metric", description="bad", sql="SELECT ts FROM orders_hourly"
            )
        )
    with pytest.raises(ValueError, match="history"):
        await cartographer.add_metric(
            AddMetricRequest(
                name="empty_metric",
                description="empty",
                sql="SELECT ts, units AS num FROM orders_hourly WHERE false",
            )
        )
    assert len(cartographer.graph.metrics()) == 2


@pytest.mark.asyncio
async def test_expression_and_failed_agent_are_atomic(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: object) -> None:
        raise RuntimeError("offline")

    monkeypatch.setattr("eye.agents.cartographer.run_agent", fail)
    with pytest.raises(RuntimeError, match="offline"):
        await cartographer.add_metric(
            AddMetricRequest(
                name="refund_rate", description="ratio", expression="refunds / orders"
            )
        )
    assert "refund_rate" not in cartographer.engine._registry.metrics


@pytest.mark.asyncio
async def test_refinement_is_cached(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    cartographer.graph.add_edge(
        Edge(
            id="seed",
            src="orders",
            dst="refunds",
            type="causal_hypothesis",
            status="hypothesis",
            confidence=0.5,
            evidence={},
            created_by="seed",
        )
    )
    run = AsyncMock(
        return_value=SeedSubmission(
            seeds=[
                SeedDecision(
                    src="orders",
                    dst="refunds",
                    status="ambiguous",
                    confidence=0.2,
                    rationale="0 usable correlations",
                )
            ],
            edges=[],
            notes="uncertain",
        )
    )
    monkeypatch.setattr("eye.agents.cartographer.run_agent", run)
    await cartographer.refine_causal_seeds()
    await cartographer.refine_causal_seeds()
    assert run.call_count == 1
    spec, payload, *_ = run.call_args.args
    prompt = Path(spec.system_prompt_file).read_text()
    assert "assess all seeds" in prompt
    assert "Do not call get_graph once per seed" in prompt
    assert json.loads(payload)["mode"] == "refine_seeds"
    assert "numbers" in json.loads(payload)["seeds"][0]
    assert cartographer.graph.edges()[0].status == "ambiguous"


@pytest.mark.asyncio
async def test_refinement_respects_main_cache_marker(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    cartographer.graph.add_edge(
        Edge(
            id="main_refined",
            src="orders",
            dst="refunds",
            type="causal_hypothesis",
            status="hypothesis",
            confidence=0.5,
            evidence={"llm_refined": True},
            created_by="seed",
        )
    )
    run = AsyncMock(side_effect=AssertionError("cached seed must not be refined"))
    monkeypatch.setattr("eye.agents.cartographer.run_agent", run)
    await cartographer.refine_causal_seeds([], run)
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [True, False])
async def test_refinement_incomplete_submit_can_be_repaired(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch, repair: bool
) -> None:
    cartographer.graph.add_edge(
        Edge(
            id="seed",
            src="orders",
            dst="refunds",
            type="causal_hypothesis",
            status="hypothesis",
            confidence=0.5,
            evidence={},
            created_by="seed",
        )
    )
    requests = []

    async def create(request: dict) -> BetaMessage:
        requests.append(copy.deepcopy(request))
        decisions = []
        if repair and len(requests) == 2:
            decisions = [
                {
                    "src": "orders",
                    "dst": "refunds",
                    "status": "ambiguous",
                    "confidence": 0.2,
                    "rationale": "0 usable correlations",
                }
            ]
        return BetaMessage.model_validate(
            {
                "id": f"msg_{len(requests)}",
                "type": "message",
                "role": "assistant",
                "model": request["model"],
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"submit_{len(requests)}",
                        "name": "submit",
                        "input": {
                            "seeds": decisions,
                            "edges": [],
                            "notes": "uncertain",
                        },
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        )

    monkeypatch.setattr(
        "eye.llm.runtime.get_client", lambda: SimpleNamespace(create=create)
    )
    if repair:
        await cartographer.refine_causal_seeds()
        assert len(requests) == 2
        assert cartographer.graph.edges()[0].evidence["refined"] is True
        assert cartographer.graph.edges()[0].status == "ambiguous"
    else:
        with pytest.raises(BudgetExceeded, match="agent turn budget exhausted"):
            await cartographer.refine_causal_seeds()
        assert len(requests) == 8
        assert not cartographer.graph.edges()[0].evidence.get("refined")
        assert (
            "final allowed turn" in requests[-1]["messages"][-1]["content"][-1]["text"]
        )
    error = requests[1]["messages"][-1]["content"][0]
    assert error["is_error"] is True
    assert "Missing seed decisions: orders -> refunds" in error["content"]


@pytest.mark.asyncio
async def test_deterministic_acceptance_overrides_proposal(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = EdgeProposal(
        type="formula",
        src="orders",
        dst="refund_rate",
        identity="refund_rate = orders / refunds",
        rationale="incorrect",
    )
    monkeypatch.setattr(
        "eye.agents.cartographer.run_agent",
        AsyncMock(return_value=Submission(edges=[proposal], notes="", unit="ratio")),
    )
    result = await cartographer.add_metric(
        AddMetricRequest(name="refund_rate", description="ratio", sql=SQL)
    )
    assert len(result.rejected) == 2


@pytest.mark.asyncio
async def test_sql_comments_cannot_expand_table_permissions(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = cartographer.engine._con
    con.execute("CREATE TABLE private_table AS SELECT * FROM orders_hourly")
    monkeypatch.setattr(
        "eye.agents.cartographer.run_agent",
        AsyncMock(return_value=Submission(edges=[], notes="", unit="count")),
    )
    await cartographer.add_metric(
        AddMetricRequest(
            name="comment_metric",
            description="orders",
            sql="SELECT ts, units AS num FROM orders_hourly /* FROM private_table */",
        )
    )
    assert cartographer.engine._registry.source_tables["comment_metric"] == (
        "orders_hourly",
    )
    with pytest.raises(ValueError, match="Table is not allowed"):
        await cartographer.add_metric(
            AddMetricRequest(
                name="private_metric",
                description="not allowed",
                sql="SELECT ts, units AS num FROM private_table",
            )
        )


@pytest.mark.asyncio
async def test_outgoing_causal_proposal_for_new_metric(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = EdgeProposal(
        type="causal_hypothesis",
        src="new_orders",
        dst="refunds",
        lag_hours=0,
        rationale="More orders can cause refunds",
    )
    monkeypatch.setattr(
        "eye.agents.cartographer.run_agent",
        AsyncMock(return_value=Submission(edges=[proposal], notes="", unit="count")),
    )
    result = await cartographer.add_metric(
        AddMetricRequest(
            name="new_orders",
            description="new orders",
            sql="SELECT ts, units AS num FROM orders_hourly",
        )
    )
    assert any(e.src == "new_orders" and e.dst == "refunds" for e in result.ambiguous)
    assert "unknown metric" not in result.notes


@pytest.mark.asyncio
async def test_concurrent_same_name_cannot_overwrite(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = asyncio.Event()
    entered = 0

    async def submit(*args: object) -> Submission:
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await ready.wait()
        return Submission(edges=[], notes="", unit="count")

    monkeypatch.setattr("eye.agents.cartographer.run_agent", submit)
    req = AddMetricRequest(
        name="same_metric",
        description="orders",
        sql="SELECT ts, units AS num FROM orders_hourly",
    )
    results = await asyncio.gather(
        cartographer.add_metric(req),
        cartographer.add_metric(req.model_copy(update={"description": "other"})),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert sum(not isinstance(r, Exception) for r in results) == 1


@pytest.mark.asyncio
async def test_additive_edges_have_mean_shares(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = EdgeProposal(
        type="formula",
        src="refunds",
        dst="contribution_margin",
        identity="contribution_margin = orders + refunds",
        rationale="Sum",
    )
    monkeypatch.setattr(
        "eye.agents.cartographer.run_agent",
        AsyncMock(return_value=Submission(edges=[proposal], notes="", unit="count")),
    )
    result = await cartographer.add_metric(
        AddMetricRequest(
            name="contribution_margin",
            description="total",
            sql="SELECT ts, units + refunds AS num FROM orders_hourly",
        )
    )
    edges = {e.src: e for e in result.accepted}
    assert edges["refunds"].evidence["mean_share"] == pytest.approx(2 / 102)
    assert edges["orders"].evidence["mean_share"] == pytest.approx(100 / 102)
    assert cartographer.graph.impact_weight("refunds") == pytest.approx(
        2 / 102, abs=1e-6
    )


@pytest.mark.asyncio
async def test_pending_seed_does_not_abort_other_refinement(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = cartographer.engine._registry
    pending = registry.get("refunds").model_copy(
        update={"name": "pending_refunds", "status": "pending"}
    )
    registry.metrics[pending.name] = pending
    registry.source_tables[pending.name] = ("orders_hourly",)
    registry.has_den[pending.name] = False
    cartographer.graph.add_metric(pending)
    for name in ("refunds", "pending_refunds"):
        cartographer.graph.add_edge(
            Edge(
                id=name,
                src="orders",
                dst=name,
                type="causal_hypothesis",
                status="hypothesis",
                confidence=0.5,
                evidence={},
                created_by="seed",
            )
        )
    original = cartographer._correlation

    def correlate(engine: MetricEngine, src: str, dst: str, lag: int = 96):
        assert dst != "pending_refunds", "pending seed must not be queried"
        return original(engine, src, dst, lag)

    async def submit(spec: object, content: str, *args: object) -> SeedSubmission:
        payload = json.loads(content)["seeds"]
        missing = next(s for s in payload if s["edge"]["dst"] == "pending_refunds")
        assert missing["numbers"] is None
        assert missing["pending_metrics"] == ["pending_refunds"]
        return SeedSubmission(
            seeds=[
                SeedDecision(
                    src="orders",
                    dst=name,
                    status="ambiguous",
                    confidence=0.1,
                    rationale="insufficient evidence",
                )
                for name in ("refunds", "pending_refunds")
            ],
            edges=[],
            notes="pending",
        )

    monkeypatch.setattr(cartographer, "_correlation", correlate)
    monkeypatch.setattr("eye.agents.cartographer.run_agent", submit)
    await cartographer.refine_causal_seeds()
    assert all(e.evidence["refined"] for e in cartographer.graph.edges())


@pytest.mark.asyncio
async def test_t18_callbacks_use_shared_engine_and_clean_hours(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = cartographer.engine
    end = engine._con.execute(
        "SELECT max(ts) + INTERVAL 1 HOUR FROM orders_hourly"
    ).fetchone()[0]
    settings = Settings(sim={"start_ts": end, "autostart": False})
    store = IncidentStore(":memory:")
    orchestrator = Orchestrator(
        settings,
        SimpleNamespace(
            now=end, live_start=end, history_start=end - timedelta(days=28)
        ),
        Sentinel(engine._con, engine._registry, graph=cartographer.graph),
        store,
        LLMClient(settings.llm),
        lambda *_: (0.0, 1.0),
        add_metric=cartographer.add_metric,
        refine=cartographer.refine_causal_seeds,
        invalidate=engine.invalidate,
    )
    monkeypatch.setattr(
        "eye.agents.cartographer.run_agent",
        AsyncMock(return_value=Submission(edges=[], notes="", unit="ratio")),
    )
    try:
        clean = orchestrator.clean_hours()
        monkeypatch.setattr(orchestrator, "clean_hours", lambda: clean[:-1])
        await orchestrator.refine(orchestrator.clean_hours(), orchestrator.record_step)
        result = await orchestrator.register_metric(
            AddMetricRequest(
                name="new_ratio", description="ratio", expression="refunds / orders"
            )
        )
        assert result.metric.name == "new_ratio"
        assert clean[-1] in cartographer.scenario_hours
        assert engine.metric_window(
            "new_ratio", end.replace(day=1), end
        ) == pytest.approx(0.02)
        assert cartographer.graph.metric("new_ratio").added_by == "user"
    finally:
        await orchestrator.close()
        store.close()
