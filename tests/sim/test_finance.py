from datetime import UTC, datetime, timedelta

import duckdb
import numpy as np
import pytest

from eye.sim.finance import TAKE_RATES, Finance
from eye.sim.world import build_world

TS = datetime(2026, 11, 2, tzinfo=UTC)


def order(seller: str = "1P", tier: str = "1P", units: int = 10) -> tuple:
    return (
        TS,
        "DE",
        "web",
        "electronics",
        "E01",
        seller,
        tier,
        "search",
        units,
        100.0 * units,
        None,
        None,
        None,
        None,
    )


def test_finance_empty_and_money() -> None:
    world = build_world(42, joined_ts=TS)
    finance = Finance(world)
    rng = np.random.default_rng(42)
    assert finance.book(TS, rng, []) == {"orders_hourly": [], "refunds_hourly": []}
    rows = finance.book(TS, rng, [order(), order("S-01", "small_3P")])["orders_hourly"]
    sku = world.skus["E01"]
    assert rows[0][10:] == (
        0.0,
        round(10 * sku.ref_price * sku.cogs_ratio, 2),
        0.0,
        50.0,
    )
    assert rows[1][10:] == (80.0, 0.0, 0.0, 0.0)
    finance.platform_discount["E01"] = 2.5
    assert finance.book(TS, rng, [order()])["orders_hourly"][0][12] == 25.0


def test_refund_lag_probability_and_cost() -> None:
    finance = Finance(build_world(42, joined_ts=TS))
    rng = np.random.default_rng(42)
    finance.book(TS, rng, [order("S-99", "small_3P", 10000)])
    total = 0
    for hour in range(100):
        rows = finance.book(TS + timedelta(hours=hour), rng, [])["refunds_hourly"]
        if rows:
            assert 48 <= hour <= 96
        for row in rows:
            total += row[6]
            assert row[8] == round(row[7] * 0.25, 2)
    assert total / 10000 == pytest.approx(0.35, abs=0.02)
    assert not finance.pending


def test_contribution_identity(reader: duckdb.DuckDBPyConnection) -> None:
    rows = reader.execute(
        "SELECT category, seller_tier, units, gmv, fee_revenue, cogs_1p, platform_discount, fulfillment_cost FROM orders_hourly"
    ).fetchall()
    expected = actual = 0.0
    for category, tier, units, gmv, fee, cogs, discount, fulfillment in rows:
        assert discount == 0
        if tier == "1P":
            assert fee == 0 and fulfillment == units * 5
            expected += gmv - cogs - units * 5
        else:
            assert cogs == 0 and fulfillment == 0
            assert fee == round(gmv * TAKE_RATES[category], 2)
            expected += round(gmv * TAKE_RATES[category], 2)
        actual += fee + (gmv - cogs if tier == "1P" else 0) - discount - fulfillment
    refund = reader.execute(
        "SELECT coalesce(sum(refund_cost), 0) FROM refunds_hourly"
    ).fetchone()[0]
    assert refund > 0
    assert actual - refund == pytest.approx(expected - refund, abs=0.01)
    assert reader.execute(
        "SELECT count(*) FROM refunds_hourly WHERE abs(refund_cost - round(refunded_gmv * 0.25, 2)) > 0.001"
    ).fetchone() == (0,)
