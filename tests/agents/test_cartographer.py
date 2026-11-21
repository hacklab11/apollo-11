import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import duckdb
import pytest

from eye.agents.cartographer import (
    Cartographer,
    SeedDecision,
    SeedSubmission,
    Submission,
)
from eye.config import LlmSettings
from eye.contracts import AddMetricRequest, Edge, EdgeProposal, MetricDef, TrailStep
from eye.graph.store import GraphStore
from eye.llm.client import LLMClient
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.tools.sqlguard import check

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/llm"
SQL = "SELECT ts, country, category, seller_tier, refunds AS num, units AS den FROM orders_hourly"


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
    else:
        assert result.ambiguous
        assert "duplicate" in result.notes.lower()
    assert cartographer.engine._registry.get(req.name).added_by == "user"
    if not live:
        network.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["refund", "duplicate"])
async def test_replay(
    cartographer: Cartographer, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    await placement(cartographer, monkeypatch, case, False)


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["refund", "duplicate"])
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
    assert cartographer.graph.edges()[0].status == "ambiguous"


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
