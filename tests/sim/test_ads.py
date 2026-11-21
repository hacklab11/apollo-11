from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from eye.config import Settings
from eye.sim.ads import Ads, marketing
from eye.sim.engine import Simulator
from eye.sim.world import HYPED_SKUS, build_world


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


def test_noon_pacing(reader: duckdb.DuckDBPyConnection) -> None:
    # Bid or eligibility changes can switch slots/CPC after the budget is fixed.
    ratios = reader.execute(
        """
        WITH eligibility AS (
            SELECT a.advertiser_id, a.category, a.ts,
                bool_or(o.in_stock) AS eligible
            FROM ads_hourly a JOIN offers_hourly o
                ON o.ts = a.ts AND o.seller_id = a.seller_id
                AND o.category = a.category
            WHERE hour(a.ts) <= 12
            GROUP BY a.advertiser_id, a.category, a.ts
        ), changed AS (
            SELECT category, ts::DATE AS day, advertiser_id
            FROM ads_hourly WHERE hour(ts) <= 12
            GROUP BY category, day, advertiser_id HAVING min(bid) != max(bid)
            UNION ALL
            SELECT category, ts::DATE AS day, advertiser_id
            FROM eligibility GROUP BY category, day, advertiser_id
            HAVING bool_and(eligible) != bool_or(eligible)
        )
        SELECT DISTINCT advertiser_id, ts::DATE AS day,
            spent_today / (daily_budget * planned_cum_share) AS ratio
        FROM ads_hourly a
        WHERE hour(ts) = 12 AND EXISTS (
            SELECT 1 FROM eligibility e
            WHERE e.advertiser_id = a.advertiser_id
                AND e.ts = a.ts AND e.eligible
        ) AND NOT EXISTS (
            SELECT 1 FROM changed c WHERE c.category = a.category AND c.day = a.ts::DATE
        )
        """
    ).fetchall()
    counts = Counter(aid for aid, _, _ in ratios)
    assert set(counts) == {f"ADV-{index}" for index in range(1, 9)}
    assert all(count >= 5 for count in counts.values())
    assert all(0.8 <= ratio <= 1.2 for _, _, ratio in ratios)


def test_second_seed_pacing(tmp_path: Path) -> None:
    settings = Settings()
    settings.sim.seed = 7
    with closing(Simulator(settings, db_path=tmp_path / "seed7.duckdb")) as sim:
        sim.backfill()
        with sim.store.cursor() as reader:
            test_noon_pacing(reader)
            test_budget_cap_and_daily_reset(reader)


@pytest.mark.parametrize("seed", [42, 7])
def test_balanced_advertiser_categories(seed: int) -> None:
    world = build_world(seed, joined_ts=datetime(2026, 1, 1, tzinfo=UTC))
    assert Counter(a.category for a in world.advertisers.values()) == dict.fromkeys(
        ("electronics", "home", "fashion", "toys"), 2
    )
    for adv in world.advertisers.values():
        assert any(
            sku.category == adv.category
            and sku.sku not in HYPED_SKUS
            and any(o.seller_id == adv.seller_id for o in world.offers[sku.sku])
            for sku in world.skus.values()
        )


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


def test_auction_requires_advertisers_own_in_stock_offer() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    world = build_world(42, joined_ts=ts)
    adv = world.advertisers["ADV-1"]
    for offers in world.offers.values():
        for offer in offers:
            if offer.seller_id == adv.seller_id:
                offer.stock = 0
    batches = {}
    landed = Ads().auction(
        ts, np.random.default_rng(2), world, np.full((6, 4), 10000), batches
    )
    assert all(
        row[5:8] == (0, 0, 0.0)
        for row in batches["ads_hourly"]
        if row[1] == adv.advertiser_id
    )
    assert landed
    for (_, sku), clicks in landed.items():
        if clicks:
            assert any(
                offer.stock > 0
                and any(
                    a.seller_id == offer.seller_id
                    and a.category == world.skus[sku].category
                    for a in world.advertisers.values()
                )
                for offer in world.offers[sku]
            )


def test_auction_with_no_in_stock_advertisers() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    world = build_world(42, joined_ts=ts)
    for offers in world.offers.values():
        for offer in offers:
            offer.stock = 0
    batches = {}
    assert (
        Ads().auction(
            ts, np.random.default_rng(2), world, np.full((6, 4), 10000), batches
        )
        == {}
    )
    assert all(row[5:8] == (0, 0, 0.0) for row in batches["ads_hourly"])


def test_budget_calibration_on_fresh_ads() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    world = build_world(42, joined_ts=ts)
    ads = Ads()
    ads.calibrate_budgets(ts, world)
    assert ads.bids == {
        adv.advertiser_id: adv.base_bid for adv in world.advertisers.values()
    }
    assert all(adv.daily_budget > 0 for adv in world.advertisers.values())
    ads.bids["ADV-1"] *= 2
    current_bid = ads.bids["ADV-1"]
    ads.calibrate_budgets(ts, world)
    assert ads.bids["ADV-1"] == current_bid


def test_budget_calibration_uses_eligible_competitors() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    world = build_world(42, joined_ts=ts)
    ads = Ads()
    ads.bids = {a.advertiser_id: a.base_bid for a in world.advertisers.values()}
    category = world.advertisers["ADV-1"].category
    high, low = sorted(
        (a for a in world.advertisers.values() if a.category == category),
        key=lambda a: -a.base_bid,
    )
    ads.calibrate_budgets(ts, world)
    high_budget = world.advertisers[high.advertiser_id].daily_budget
    low_budget = world.advertisers[low.advertiser_id].daily_budget
    for offers in world.offers.values():
        for offer in offers:
            if offer.seller_id == low.seller_id:
                offer.stock = 0
    ads.calibrate_budgets(ts, world)
    assert world.advertisers[high.advertiser_id].daily_budget == low_budget
    assert 0 < world.advertisers[low.advertiser_id].daily_budget == low_budget
    assert high_budget > low_budget
    for offers in world.offers.values():
        for offer in offers:
            if offer.seller_id == high.seller_id:
                offer.stock = 0
    ads.calibrate_budgets(ts, world)
    assert world.advertisers[high.advertiser_id].daily_budget == low_budget


@pytest.mark.parametrize("seed", [42, 7])
def test_fixed_ctr_accumulates_fractional_clicks(seed: int) -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    world = build_world(42, joined_ts=ts)
    ads = Ads()
    totals = Counter()
    for hour in range(13):
        batches = {}
        landed = ads.auction(
            ts + timedelta(hours=hour),
            np.random.default_rng([seed, hour]),
            world,
            np.full((6, 4), 50),
            batches,
        )
        assert sum(landed.values()) == sum(r[6] for r in batches["ads_hourly"])
        for row in batches["ads_hourly"]:
            totals[row[1], row[2]] += row[6]
    assert len(totals) == 16
    for key, clicks in totals.items():
        assert clicks == round(ads.served_impressions[key] * 0.02)
