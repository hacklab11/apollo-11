from collections.abc import Iterator
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from eye.config import Settings
from eye.sim.engine import Simulator
from eye.sim.scenarios import ScenarioManager, inject


@pytest.fixture(scope="module")
def baseline(settings: Settings) -> Iterator[Simulator]:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        sim.backfill()
        yield sim


@contextmanager
def fork(baseline: Simulator, *, warmup: int = 12) -> Iterator[Simulator]:
    with closing(Simulator(baseline.settings, db_path=":memory:")) as sim:
        (
            sim.world,
            sim.ads,
            sim.apps,
            sim.pipeline,
            sim.market,
            sim.finance,
            sim.selleros_export,
        ) = deepcopy(
            (
                baseline.world,
                baseline.ads,
                baseline.apps,
                baseline.pipeline,
                baseline.market,
                baseline.finance,
                baseline.selleros_export,
            )
        )
        with baseline.store.cursor() as con:
            sim.store.append(
                {"purchase_orders": con.execute("FROM purchase_orders").fetchall()}
            )
        for _ in range(warmup):
            sim.step()
        yield sim


def number(sim: Simulator, sql: str, start: datetime) -> float:
    with sim.store.cursor() as con:
        return float(con.execute(sql, [start]).fetchone()[0])


def snapshot(sim: Simulator, start: datetime) -> dict[str, float]:
    queries = {
        "sessions": "SELECT sum(sessions) FROM traffic_hourly WHERE ts >= ?",
        "orders": "SELECT sum(units) FROM orders_hourly WHERE ts >= ?",
        "gmv": "SELECT sum(gmv) FROM orders_hourly WHERE ts >= ?",
        "feed_ctr": "SELECT sum(clicks) / sum(impressions) FROM feed_hourly WHERE ts >= ?",
        "feed_clicks": "SELECT sum(clicks) FROM feed_hourly WHERE ts >= ?",
        "feed_orders": "SELECT sum(units) FROM orders_hourly WHERE channel = 'feed' AND ts >= ?",
        "unavailable": "SELECT sum(impressions) FILTER (NOT in_stock) / sum(impressions) FROM feed_hourly WHERE ts >= ?",
        "stock_home": "SELECT sum(CASE WHEN in_stock THEN popularity ELSE 0 END) / sum(popularity) FROM offers_hourly JOIN skus USING (sku, category) WHERE category = 'home' AND ts >= ?",
        "marketing": "SELECT sum(spend) FROM marketing_hourly WHERE ts >= ?",
        "discounts": "SELECT sum(platform_discount) FROM orders_hourly WHERE ts >= ?",
        "trading": "SELECT sum(fee_revenue + CASE WHEN seller_tier = '1P' THEN gmv - cogs_1p ELSE 0 END - fulfillment_cost) FROM orders_hourly WHERE ts >= ?",
        "ad_revenue": "SELECT sum(spend) FROM ads_hourly WHERE ts >= ?",
        "refund_cost": "SELECT coalesce(sum(refund_cost), 0) FROM refunds_hourly WHERE ts >= ?",
        "psp_success": "SELECT sum(successes) / sum(attempts) FROM payments_hourly WHERE psp = 'PSP-2' AND ts >= ?",
    }
    for country in ("DE", "FR"):
        queries[f"searches_{country}"] = (
            f"SELECT sum(searches) FROM search_hourly WHERE country = '{country}' AND ts >= ?"
        )
        queries[f"search_orders_{country}"] = (
            f"SELECT sum(units) FROM orders_hourly WHERE country = '{country}' AND channel = 'search' AND ts >= ?"
        )
    for platform in ("web", "android", "ios"):
        queries[f"checkout_{platform}"] = (
            f"SELECT sum(checkout_starts) / sum(carts) FROM checkout_hourly WHERE platform = '{platform}' AND ts >= ?"
        )
    result = {key: number(sim, query, start) for key, query in queries.items()}
    result["contribution"] = (
        result["trading"]
        + result["ad_revenue"]
        - result["marketing"]
        - result["discounts"]
        - result["refund_cost"]
    )
    result["feed_purchase"] = result["feed_orders"] / result["feed_clicks"]
    for country in ("DE", "FR"):
        result[f"search_conversion_{country}"] = (
            result[f"search_orders_{country}"] / result[f"searches_{country}"]
        )
    return result


def play(sim: Simulator, hours: int = 6) -> None:
    for _ in range(hours):
        sim.step()


def test_s1_budget_spent_by_ten(baseline: Simulator) -> None:
    with fork(baseline, warmup=0) as sim:
        manager = ScenarioManager(sim)
        manager.inject("S1")
        play(sim, 11)
        with sim.store.cursor() as con:
            ratios = dict(
                con.execute("""
                SELECT advertiser_id, max(spent_today / daily_budget)
                FROM ads_hourly WHERE hour(ts) = 10 GROUP BY advertiser_id
            """).fetchall()
            )
        assert ratios["ADV-3"] >= 0.95, ratios
        assert all(v <= 0.6 for k, v in ratios.items() if k != "ADV-3"), ratios


def test_s2_home_stock_and_supplier_decoy(baseline: Simulator) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        start = sim.now
        ScenarioManager(sim).inject("S2")
        play(control)
        play(sim)
        normal, actual = snapshot(control, start), snapshot(sim, start)
        with sim.store.cursor() as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM purchase_orders WHERE sku = 'T05' AND supplier_id = ? AND delivered_ts > eta_ts",
                    [sim.world.skus["T05"].supplier_id],
                ).fetchone()[0]
                > 0
            )
            assert (
                con.execute(
                    "SELECT count(*) FROM purchase_orders WHERE sku LIKE 'H%' AND system_version = 'purchasing v2.3'"
                ).fetchone()[0]
                > 0
            )
        assert actual["stock_home"] / normal["stock_home"] <= 0.7, (normal, actual)
        assert actual["searches_DE"] == normal["searches_DE"]
        assert actual["searches_FR"] == normal["searches_FR"]


def test_s3_german_relevance_and_french_control(baseline: Simulator) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        start = sim.now
        ScenarioManager(sim).inject("S3")
        play(control)
        play(sim)
        normal, actual = snapshot(control, start), snapshot(sim, start)
        assert actual["search_conversion_DE"] / normal["search_conversion_DE"] <= 0.6, (
            normal,
            actual,
        )
        assert actual["search_conversion_FR"] / normal[
            "search_conversion_FR"
        ] == pytest.approx(1, abs=0.05), (normal, actual)
        assert actual["searches_DE"] == normal["searches_DE"]
        assert actual["searches_FR"] == normal["searches_FR"]


def test_s4_feed_engagement_and_unavailable_exposure(baseline: Simulator) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        start = sim.now
        ScenarioManager(sim).inject("S4")
        play(control)
        play(sim)
        normal, actual = snapshot(control, start), snapshot(sim, start)
        assert actual["feed_ctr"] / normal["feed_ctr"] >= 1.1, (normal, actual)
        assert actual["feed_purchase"] / normal["feed_purchase"] <= 0.75, (
            normal,
            actual,
        )
        assert actual["unavailable"] >= 0.3, (normal, actual)


def test_s5_orders_and_contribution_split(baseline: Simulator) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        start = sim.now
        ScenarioManager(sim).inject("S5")
        play(control)
        play(sim)
        normal, actual = snapshot(control, start), snapshot(sim, start)
        drop = normal["contribution"] - actual["contribution"]
        marketing = actual["marketing"] - normal["marketing"]
        discounts = actual["discounts"] - normal["discounts"]
        assert actual["orders"] > normal["orders"], (normal, actual)
        assert drop > 0, (normal, actual)
        assert (marketing + discounts) / drop >= 0.85, (normal, actual)


def test_s7_with_i1_checkout_outage(baseline: Simulator) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        for world in (control, sim):
            inject(world, "I1")
            play(world, 3)
        start = sim.now
        inject(sim, "S7")
        play(control)
        play(sim)
        normal, actual = snapshot(control, start), snapshot(sim, start)
        for platform in ("web", "android"):
            assert (
                actual[f"checkout_{platform}"] / normal[f"checkout_{platform}"] <= 0.2
            ), (normal, actual)
        assert actual["checkout_ios"] / normal["checkout_ios"] == pytest.approx(
            1, abs=0.05
        ), (normal, actual)
        assert actual["psp_success"] / normal["psp_success"] == pytest.approx(
            1, abs=0.05
        ), (normal, actual)
        assert actual["gmv"] / normal["gmv"] <= 0.7, (normal, actual)
        with sim.store.cursor() as con:
            assert (
                con.execute(
                    "SELECT sum(errors) FROM client_errors_hourly WHERE ts >= ? AND platform = 'web' AND app_version = 'checkout-web v5.8' AND page = 'checkout' AND error_type = 'TypeError'",
                    [start],
                ).fetchone()[0]
                > 0
            )
            assert (
                con.execute(
                    "SELECT count(*) FROM external_status WHERE provider = 'PSP-2' AND status = 'degraded'"
                ).fetchone()[0]
                == 1
            )


def test_i1_is_planned_uplift(baseline: Simulator) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        start = sim.now
        ScenarioManager(sim).inject("I1")
        play(control)
        play(sim)
        normal, actual = snapshot(control, start), snapshot(sim, start)
        assert 1.35 <= actual["sessions"] / normal["sessions"] <= 1.45
        assert actual["orders"] / normal["orders"] == pytest.approx(1.35, rel=0.25), (
            normal,
            actual,
        )
        for key in (
            "search_conversion_DE",
            "search_conversion_FR",
            "feed_ctr",
            "feed_purchase",
            "psp_success",
            "checkout_web",
            "checkout_android",
            "checkout_ios",
        ):
            assert actual[key] / normal[key] == pytest.approx(1, abs=0.05), (
                key,
                normal,
                actual,
            )


def test_i2_late_rows_are_backfilled_even_after_clear(baseline: Simulator) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        start = sim.now
        manager = ScenarioManager(sim)
        manager.inject("I2")
        play(control, 9)
        play(sim, 3)
        with sim.store.cursor() as con:
            rows = con.execute(
                "SELECT expected_rows, loaded_rows, loaded_at - ts FROM partition_status WHERE table_name = 'orders_hourly' AND country = 'FR' AND ts >= ? ORDER BY ts",
                [start],
            ).fetchall()
            assert len(rows) == 3
            for expected, loaded, delay in rows:
                assert loaded == round(expected * 0.4)
                assert delay == timedelta(hours=6)
        manager.clear("I2")
        play(sim, 6)
        with sim.store.cursor() as con, control.store.cursor() as reference:
            query = "SELECT * FROM orders_hourly WHERE ts >= ? ORDER BY ALL"
            assert (
                con.execute(query, [start]).fetchall()
                == reference.execute(query, [start]).fetchall()
            )
            assert con.execute(
                "SELECT count(*) FROM partition_status WHERE table_name = 'orders_hourly' AND country = 'FR' AND loaded_rows != expected_rows"
            ).fetchone() == (0,)


@pytest.mark.parametrize("id", ["S1", "S3", "S4", "S5", "S7", "I1"])
def test_clear_returns_to_baseline_within_three_hours(
    baseline: Simulator, id: str
) -> None:
    with fork(baseline) as control, fork(baseline) as sim:
        manager = ScenarioManager(sim)
        manager.inject(id)
        play(control)
        play(sim)
        manager.clear(id)
        start = sim.now
        play(control, 3)
        play(sim, 3)
        normal, actual = snapshot(control, start), snapshot(sim, start)
        for key in (
            "sessions",
            "feed_ctr",
            "feed_purchase",
            "checkout_web",
            "checkout_android",
            "checkout_ios",
            "search_conversion_DE",
            "search_conversion_FR",
        ):
            assert actual[key] / normal[key] == pytest.approx(1, abs=0.05), (
                id,
                key,
                normal,
                actual,
            )
        assert not manager.active()
        assert manager.timeline()[0].cleared_ts == start


def test_lifecycle_empty_scheduled_and_errors(settings: Settings) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        manager = ScenarioManager(sim)
        assert manager.active() == manager.timeline() == []
        with pytest.raises(ValueError, match="unsupported"):
            manager.inject("S6")
        with pytest.raises(ValueError, match="not injected"):
            manager.clear("S3")
        with pytest.raises(ValueError, match="aware current or future whole hour"):
            manager.inject("S3", sim.now - timedelta(hours=1))
        due = sim.now + timedelta(hours=2)
        manager.inject("S3", due)
        with pytest.raises(ValueError, match="already injected"):
            manager.inject("S3")
        assert manager.active() == []
        play(sim, 2)
        assert manager.active() == []
        play(sim, 1)
        assert manager.active()[0].injected_ts == due
        manager.clear("S3")
        assert manager.active() == []
        copy = manager.timeline()
        copy[0].active = True
        assert not manager.timeline()[0].active
        with sim.store.cursor() as con:
            assert con.execute(
                "SELECT action, ts FROM scenario_log ORDER BY ts"
            ).fetchall() == [("inject", due), ("clear", due + timedelta(hours=1))]
