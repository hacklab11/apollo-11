import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from eye.config import Settings
from eye.sim.engine import Simulator
from eye.sim.market import Market, buy_box
from eye.sim.selleros_export import SellerOSExport
from eye.sim.world import build_world

TS = datetime(2026, 11, 2, 6, tzinfo=UTC)


def test_buy_box_rating_ties_and_empty() -> None:
    world = build_world(42, joined_ts=TS)
    offers = world.offers["E01"]
    for offer in offers:
        offer.stock = 0
    assert buy_box(world, "E01") is None
    a, b = offers[:2]
    a.stock = b.stock = 10
    a.price, b.price = 100, 99
    world.sellers[a.seller_id].rating = 0.99
    world.sellers[b.seller_id].rating = 0.85
    assert buy_box(world, "E01") is a
    b.price = 90
    assert buy_box(world, "E01") is b
    a.price = b.price = 100
    world.sellers[b.seller_id].rating = 0.99
    assert buy_box(world, "E01").seller_id == min(a.seller_id, b.seller_id)


@pytest.mark.parametrize(
    "mode,multiplier", [("fixed", 1.0), ("match", 0.8), ("beat", 0.792)]
)
def test_reprice_modes_and_floor(mode: str, multiplier: float) -> None:
    world = build_world(42, joined_ts=TS)
    market = Market(world)
    offers = world.offers["E01"]
    a, b = offers[:2]
    ref = world.skus["E01"].ref_price
    for o in offers:
        o.stock = 0
    a.stock = b.stock = 10
    a.price, b.price = ref, ref * 0.8
    a.repricer_mode = mode
    a.floor = None
    b.repricer_mode = "fixed"
    market.run_repricers()
    assert a.price == pytest.approx(round(ref * multiplier, 2))
    a.repricer_mode = "beat"
    a.price, b.price, a.floor = ref, ref * 0.1, ref * 0.7
    market.run_repricers()
    assert a.price == round(ref * 0.7, 2)


def test_recovery_and_hard_minimum() -> None:
    world = build_world(42, joined_ts=TS)
    market = Market(world)
    offers = world.offers["E01"]
    a, b = offers[:2]
    ref = world.skus["E01"].ref_price
    for o in offers:
        o.stock = 0
    a.stock = b.stock = 10
    a.repricer_mode, b.repricer_mode = "match", "fixed"
    a.price, b.price = ref * 0.7, ref * 1.1
    market.run_repricers()
    assert a.price == ref
    a.price, b.price, a.floor = ref, ref * 0.1, None
    market.run_repricers()
    assert a.price == round(ref * 0.4, 2)


@pytest.mark.parametrize(
    "mode,price_mult,other_mult,other_stock,expected_mult",
    [
        ("match", 0.8, 0.8, 10, 0.84),
        ("beat", 0.8, 0.8, 10, 0.84),
        ("match", 0.95, 0.95, 10, 0.95),
        ("match", 0.9, 0.9, 10, 0.9),
        ("match", 0.8, 0.75, 10, 0.75),
        ("fixed", 0.8, 0.8, 10, 0.8),
        ("match", 0.8, 0.8, 0, 0.8),
    ],
)
def test_deep_discount_drift_up(
    mode: str,
    price_mult: float,
    other_mult: float,
    other_stock: int,
    expected_mult: float,
) -> None:
    world = build_world(42, joined_ts=TS)
    market = Market(world)
    offers = world.offers["E01"]
    for offer in offers:
        offer.stock = 0
    a, b = offers[:2]
    ref = world.skus["E01"].ref_price
    a.stock, b.stock = 10, other_stock
    a.price, b.price = ref * price_mult, ref * other_mult
    a.repricer_mode, b.repricer_mode = mode, "fixed"
    a.floor = None
    market.run_repricers()
    expected = ref * expected_mult
    assert a.price == (
        expected if mode == "fixed" or not other_stock else round(expected, 2)
    )


def test_restock_capacity_and_delivery() -> None:
    world = build_world(42, joined_ts=TS)
    market = Market(world)
    offer = next(o for o in world.offers["E01"] if o.seller_id.startswith("S-"))
    for group in world.offers.values():
        for o in group:
            if o.seller_id == offer.seller_id:
                o.fba_capacity_week = 0
    offer.fba_capacity_week = 5
    offer.stock = 0
    market.before_demand(TS, np.random.default_rng(42))
    assert market.used[offer.seller_id] == 5
    assert market.blocked[offer.seller_id] > 0
    assert sum(q for _, q in offer.inbound) <= 5
    assert market.out_of_stock_hours[offer.seller_id, offer.sku] == 1
    market.before_demand(TS + timedelta(hours=48), np.random.default_rng(42))
    assert offer.stock == 5
    assert market.out_of_stock_hours[offer.seller_id, offer.sku] == 0


def test_purchase_orders_arrive_once() -> None:
    world = build_world(42, joined_ts=TS)
    market = Market(world)
    offer = next(o for o in world.offers["E01"] if o.seller_id == "1P")
    offer.stock = 0
    ts = TS.replace(hour=2)
    rows = market.before_demand(ts, np.random.default_rng(42))["purchase_orders"]
    po = next(row for row in rows if row[1] == "E01")
    assert po[4] > 0 and po[8] == "purchasing v2.2"
    assert po[6:8] == (None, 0)
    po = next(row for row in market.pos if row[1] == "E01")
    assert abs((po[6] - po[5]).total_seconds()) <= 6 * 3600
    market.before_demand(po[6], np.random.default_rng(42))
    assert offer.stock == po[7]
    market.before_demand(po[6] + timedelta(hours=1), np.random.default_rng(42))
    assert offer.stock == po[7]


def test_po_store_updates_atomically(settings: Settings, tmp_path: Path) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        row = (
            "PO-test",
            "E01",
            "SUP-1",
            TS,
            10,
            TS + timedelta(hours=48),
            None,
            0,
            "purchasing v2.2",
        )
        sim.store.append({"purchase_orders": [row]})
        delivered = (*row[:6], TS + timedelta(hours=48), 10, row[8])
        sim.store.append({"purchase_orders": [delivered]})
        with sim.store.cursor() as con:
            assert con.execute(
                "SELECT count(*), max(delivered_qty) FROM purchase_orders"
            ).fetchone() == (1, 10)


def test_baseline_prices_private_data_and_dormant(
    reader: duckdb.DuckDBPyConnection,
) -> None:
    prices = reader.execute("""
        SELECT ts, avg(price / ref_price) FROM offers_hourly JOIN skus USING (sku)
        GROUP BY ts ORDER BY ts LIMIT 168
    """).fetchall()
    assert len(prices) == 168
    assert abs(prices[-1][1] / prices[0][1] - 1) < 0.01
    assert reader.execute(
        "SELECT count(*) FROM offers_hourly WHERE seller_id = 'S-99'"
    ).fetchone() == (0,)
    columns = {
        r[0]
        for r in reader.execute(
            "SELECT column_name FROM information_schema.columns"
        ).fetchall()
    }
    assert not columns.intersection(
        {"cost", "unit_cost", "floor", "repricer_mode", "cogs_ratio"}
    )
    assert reader.execute(
        "SELECT count(*) FROM offers_hourly WHERE stock < 0 OR (sku IN ('E11','F09','T07') AND in_stock)"
    ).fetchone() == (0,)


def test_export_atomic_history_and_error(tmp_path: Path) -> None:
    world = build_world(42, joined_ts=TS)
    path = tmp_path / "sellers.json"
    exporter = SellerOSExport(world, 42, path)
    exporter.write(TS)
    data = json.loads(path.read_text())
    assert len(data["sellers"]) == 24
    assert {s["seller_id"] for s in data["sellers"]} == {
        f"S-{i:02d}" for i in range(1, 25)
    }
    assert data["generated_for_seed"] == 42
    exporter.write(TS + timedelta(hours=1))
    assert json.loads(path.read_text()) == data
    offer = next(
        o for group in world.offers.values() for o in group if o.seller_id == "S-01"
    )
    offer.price -= 1
    exporter.write(TS + timedelta(hours=2))
    assert (
        len(json.loads(path.read_text())["sellers"][0]["price_history"])
        == len(data["sellers"][0]["price_history"]) + 1
    )
    exporter.path = tmp_path
    with pytest.raises(OSError):
        exporter.write(TS)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sellers.json"]


def test_step_decrements_stock(settings: Settings, tmp_path: Path) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        offer = buy_box(sim.world, "E01")
        before = offer.stock
        sim.step()
        with sim.store.cursor() as con:
            sold = con.execute(
                "SELECT coalesce(sum(units), 0) FROM orders_hourly WHERE sku = 'E01' AND seller_id = ?",
                [offer.seller_id],
            ).fetchone()[0]
        assert offer.stock == before - sold
        assert sim.selleros_export.path.exists()
