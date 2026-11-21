import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from eye.config import Settings
from eye.sim.engine import Hour, Simulator
from eye.sim.scenarios import ScenarioManager


@pytest.fixture(params=[42, 43, 47], ids=lambda seed: f"seed-{seed}")
def coupon_day(request: pytest.FixtureRequest, tmp_path: Path) -> dict[str, Any]:
    settings = Settings()
    settings.sim.seed = request.param
    with (
        closing(Simulator(settings, db_path=":memory:")) as control,
        closing(Simulator(settings, db_path=":memory:")) as sim,
    ):
        control.selleros_export.path = tmp_path / "control.json"
        sim.selleros_export.path = tmp_path / "sellers.json"
        manager = ScenarioManager(sim)
        start = sim.now
        manager.inject("I3")
        observed: list[tuple[float, float, dict[str, float]]] = []

        def observe(world: Simulator, hour: Hour) -> None:
            observed.append(
                (
                    hour.demand.campaign_mult,
                    hour.demand.coupled_volume_mult,
                    world.finance.platform_discount.copy(),
                )
            )

        sim.pre_demand_hooks.append(observe)
        for _ in range(26):
            control.step()
            sim.step()
        with sim.store.cursor() as con, control.store.cursor() as reference:
            change = con.execute(
                "SELECT id, ts, kind, planned, scope, description, expected_effect "
                "FROM change_log WHERE kind = 'campaign'"
            ).fetchall()
            unchanged = {}
            for table in (
                "traffic_hourly",
                "search_hourly",
                "feed_hourly",
                "checkout_hourly",
                "payments_hourly",
                "offers_hourly",
            ):
                unchanged[table] = (
                    con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
                    == reference.execute(
                        f"SELECT * FROM {table} ORDER BY ALL"
                    ).fetchall()
                )
            query = (
                "SELECT * EXCLUDE (platform_discount) FROM orders_hourly ORDER BY ALL"
            )
            orders_unchanged = (
                con.execute(query).fetchall() == reference.execute(query).fetchall()
            )
            discounts = con.execute(
                "SELECT ts, orders_hourly.category, units, ref_price, platform_discount "
                "FROM orders_hourly JOIN skus USING (sku)"
            ).fetchall()
            bad_discounts = sum(
                discount
                != (
                    round(units * (ref_price * 0.1), 2)
                    if category == "electronics" and ts < start + timedelta(hours=24)
                    else 0
                )
                for ts, category, units, ref_price, discount in discounts
            )
            discount_total = con.execute(
                "SELECT sum(platform_discount) FROM orders_hourly"
            ).fetchone()[0]
            incomplete = con.execute(
                "SELECT count(*) FROM partition_status WHERE loaded_rows != expected_rows"
            ).fetchone()[0]
        assert manager.active() == []
        state = manager.timeline()[0]
        with pytest.raises(ValueError, match="not injected"):
            manager.clear("I3")
        manager.inject("I3")
        sim.step()
        with sim.store.cursor() as con:
            campaign_count = con.execute(
                "SELECT count(*), count(DISTINCT id) FROM change_log WHERE kind = 'campaign'"
            ).fetchone()
        return {
            "start": start,
            "change": change,
            "unchanged": unchanged,
            "orders_unchanged": orders_unchanged,
            "bad_discounts": bad_discounts,
            "discount_total": discount_total,
            "incomplete": incomplete,
            "observed": observed,
            "state": state,
            "campaign_count": campaign_count,
        }


def test_i3_coupon_only_innocent_controls(coupon_day: dict[str, Any]) -> None:
    assert all(coupon_day["unchanged"].values())
    assert coupon_day["orders_unchanged"]
    assert coupon_day["incomplete"] == 0
    assert coupon_day["discount_total"] > 0
    assert coupon_day["bad_discounts"] == 0
    assert all(
        campaign == coupled == 1.0 for campaign, coupled, _ in coupon_day["observed"]
    )
    assert len(coupon_day["change"]) == 1
    identifier, ts, kind, planned, scope, description, effect = coupon_day["change"][0]
    assert identifier == description == "coupon-day-electronics"
    assert ts == coupon_day["start"]
    assert kind == "campaign" and planned
    assert json.loads(scope) == {"category": "electronics"}
    assert json.loads(effect) == {
        "platform_discounts": "10% of electronics reference-price GMV",
        "duration_hours": 24,
    }


def test_i3_expiry_and_reinjection(coupon_day: dict[str, Any]) -> None:
    observed = coupon_day["observed"]
    assert all(discounts for _, _, discounts in observed[:24])
    assert all(not discounts for _, _, discounts in observed[24:26])
    assert observed[26][2]
    assert coupon_day["state"].cleared_ts == coupon_day["start"] + timedelta(hours=24)
    assert not coupon_day["state"].active
    assert coupon_day["campaign_count"] == (2, 2)


def test_i3_scheduled_and_early_clear(settings: Settings, tmp_path: Path) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        sim.finance.platform_discount = {"E01": 2.0}
        manager = ScenarioManager(sim)
        with pytest.raises(ValueError, match="not injected"):
            manager.clear("I3")
        due = sim.now + timedelta(hours=2)
        manager.inject("I3", due)
        with pytest.raises(ValueError, match="already injected"):
            manager.inject("I3")
        sim.step()
        sim.step()
        assert manager.active() == []
        assert sim.finance.platform_discount == {"E01": 2.0}
        sim.step()
        assert manager.active()[0].injected_ts == due
        assert sim.finance.platform_discount["E01"] != 2.0
        manager.clear("I3")
        assert sim.finance.platform_discount == {"E01": 2.0}
        sim.step()
        assert sim.finance.platform_discount == {"E01": 2.0}
        assert manager.active() == []


def test_i3_empty_orders_and_cancel_before_start(
    settings: Settings, tmp_path: Path
) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        for offers in sim.world.offers.values():
            for offer in offers:
                offer.stock = 0
                offer.inbound.clear()
        manager = ScenarioManager(sim)
        manager.inject("I3", sim.now + timedelta(hours=1))
        manager.clear("I3")
        sim.step()
        sim.step()
        assert not manager.active()
        assert sim.finance.platform_discount == {}
        manager.inject("I3")
        sim.step()
        with sim.store.cursor() as con:
            assert con.execute("SELECT count(*) FROM orders_hourly").fetchone() == (0,)
