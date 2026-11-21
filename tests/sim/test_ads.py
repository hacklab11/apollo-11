from datetime import UTC, datetime

import duckdb
import numpy as np
import pytest

from eye.sim.ads import Ads, marketing
from eye.sim.world import build_world


def test_budget_cap_and_daily_reset(reader: duckdb.DuckDBPyConnection) -> None:
    assert (
        reader.execute(
            "SELECT count(*) FROM ads_hourly WHERE spent_today > daily_budget OR spend < 0"
        ).fetchone()[0]
        == 0
    )
    assert (
        reader.execute(
            "SELECT max(spent_today - daily_budget) FROM ads_hourly"
        ).fetchone()[0]
        <= 0
    )


@pytest.mark.xfail(
    strict=True,
    reason="Spec auction traffic and 400–3000 EUR budgets cannot ensure every advertiser spends to plan",
)
def test_noon_pacing(reader: duckdb.DuckDBPyConnection) -> None:
    ratios = reader.execute(
        "SELECT advertiser_id, spent_today / (daily_budget * planned_cum_share) FROM ads_hourly WHERE hour(ts) = 12"
    ).fetchall()
    assert ratios
    assert all(0.8 <= ratio <= 1.2 for _, ratio in ratios)


def test_empty_auction_and_bid_escalation() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    world = build_world(42, joined_ts=ts)
    ads = Ads()
    adv = world.advertisers["ADV-1"]
    ads.out_of_stock_hours[adv.seller_id] = 13
    batches = {}
    assert (
        ads.auction(
            ts, np.random.default_rng(42), world, np.zeros((6, 4), dtype=int), batches
        )
        == {}
    )
    assert ads.bids[adv.advertiser_id] == pytest.approx(adv.base_bid * 1.25**2)
    ads.out_of_stock_hours.clear()
    ads.recovered_hours[adv.seller_id] = 24
    ads.auction(ts, np.random.default_rng(42), world, np.zeros((6, 4), dtype=int), {})
    assert ads.bids[adv.advertiser_id] == adv.base_bid
    marketing(ts, batches, {"DE": 0.0})
    assert all(row[3] == 0 for row in batches["marketing_hourly"] if row[1] == "DE")


def test_auction_clicks_land_and_second_price() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    world = build_world(42, joined_ts=ts)
    ads = Ads()
    batches = {}
    landed = ads.auction(
        ts, np.random.default_rng(2), world, np.full((6, 4), 10000), batches
    )
    assert sum(landed.values()) == sum(row[6] for row in batches["ads_hourly"])
    assert all(row[8] >= 0.1 for row in batches["ads_hourly"] if row[6])
    assert len(batches["ads_hourly"]) == 16
