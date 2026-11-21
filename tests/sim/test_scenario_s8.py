import json
from collections.abc import Iterator
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from eye.config import Settings
from eye.sim.cli import _play_schedule
from eye.sim.engine import Simulator
from eye.sim.scenarios import ScenarioManager


@contextmanager
def fork(base: Simulator, path: Path) -> Iterator[Simulator]:
    with closing(Simulator(base.settings, db_path=":memory:")) as sim:
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
                base.world,
                base.ads,
                base.apps,
                base.pipeline,
                base.market,
                base.finance,
                base.selleros_export,
            )
        )
        sim.selleros_export.path = path
        with base.store.cursor() as con:
            sim.store.append(
                {
                    table: con.execute(
                        f"SELECT * FROM {table} WHERE ts >= ?",
                        [base.now - timedelta(days=7)],
                    ).fetchall()
                    for table in ("orders_hourly", "offers_hourly")
                }
            )
        yield sim


def metrics(
    sim: Simulator, start: datetime, end: datetime, fee_segment: str
) -> tuple[float, float]:
    with sim.store.cursor() as con:
        return con.execute(
            "SELECT sum(gmv) / sum(units), "
            f"sum(fee_revenue) FILTER (WHERE {fee_segment}) / "
            f"sum(units) FILTER (WHERE {fee_segment}) "
            "FROM orders_hourly WHERE category = 'electronics' AND ts >= ? AND ts < ?",
            [start, end],
        ).fetchone()


@pytest.fixture(
    scope="module",
    params=[(seed, "seller_tier != '1P'") for seed in (42, 43, 47)],
    ids=lambda param: f"seed-{param[0]}-3P-electronics",
)
def run(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    seed, fee_segment = request.param
    directory = tmp_path_factory.mktemp(f"s8-{seed}")
    settings = Settings()
    settings.sim.seed = seed
    with closing(Simulator(settings, db_path=":memory:")) as baseline:
        baseline.selleros_export.path = directory / "baseline.json"
        baseline.backfill()
        with (
            fork(baseline, directory / "control.json") as control,
            fork(baseline, directory / "sellers.json") as sim,
        ):
            normal, manager = ScenarioManager(control), ScenarioManager(sim)
            schedule = _play_schedule("I3@0,S8@10")
            start = sim.now + timedelta(hours=10)
            original = {}
            for hour in range(11):
                for scenario in schedule.get(hour, []):
                    if scenario == "S8":
                        original = {
                            (o.seller_id, o.sku): (o.price, o.repricer_mode)
                            for offers in sim.world.offers.values()
                            for o in offers
                        }
                        with sim.store.cursor() as con:
                            before_changes = con.execute(
                                "SELECT * FROM change_log ORDER BY ALL"
                            ).fetchall()
                    manager.inject(scenario)
                    if scenario == "S8":
                        with sim.store.cursor() as con:
                            changes_match = (
                                before_changes
                                == con.execute(
                                    "SELECT * FROM change_log ORDER BY ALL"
                                ).fetchall()
                            )
                    if scenario != "S8":
                        normal.inject(scenario)
                control.step()
                sim.step()
            seller = manager.params["S8"]["seller_id"]
            affected = [
                o
                for sku, offers in sim.world.offers.items()
                if sku in {f"E{i:02d}" for i in range(1, 11)}
                for o in offers
                if o.seller_id == seller
            ]
            result: dict[str, Any] = {
                "seed": seed,
                "fee_segment": fee_segment,
                "seller": seller,
                "start": start.isoformat(),
                "changes_match": changes_match,
                "offers": [
                    {
                        "sku": o.sku,
                        "old_price": original[seller, o.sku][0],
                        "price": o.price,
                        "cost": o.cost,
                    }
                    for o in affected
                ],
            }
            with sim.store.cursor() as con:
                result["partitions"] = con.execute(
                    "SELECT country, expected_rows, loaded_rows FROM partition_status "
                    "WHERE table_name = 'orders_hourly' AND ts = ?",
                    [start],
                ).fetchall()
                result["loaded_counts"] = dict(
                    con.execute(
                        "SELECT country, count(*) FROM orders_hourly WHERE ts = ? GROUP BY country",
                        [start],
                    ).fetchall()
                )
                result["offer_history"] = con.execute(
                    "SELECT ts, sku, seller_id, price FROM offers_hourly "
                    "WHERE ts >= ? AND category = 'electronics' ORDER BY ts",
                    [settings.sim.start_ts],
                ).fetchall()
            result["private"] = json.loads((directory / "sellers.json").read_text())
            soldout: dict[str, float] = {}
            for hour in range(1, 13):
                for offer in affected:
                    if offer.stock == 0:
                        soldout.setdefault(offer.sku, float(hour))
                if hour == 3:
                    result["control"] = metrics(control, start, sim.now, fee_segment)
                    result["actual"] = metrics(sim, start, sim.now, fee_segment)
                if len(soldout) == len(affected) and hour >= 3:
                    break
                if hour == 1:
                    control.step()
                    sim.step()
                    with sim.store.cursor() as con:
                        result["backfilled"] = con.execute(
                            "SELECT country, expected_rows, loaded_rows FROM partition_status "
                            "WHERE table_name = 'orders_hourly' AND ts = ?",
                            [start],
                        ).fetchall()
                else:
                    control.step()
                    sim.step()
            result["sellout_hours"] = soldout
            before_clear = {o.sku: o.stock for o in affected}
            manager.clear("S8")
            result["restored"] = all(
                (o.price, o.repricer_mode) == original[seller, o.sku]
                and o.stock == before_clear[o.sku]
                for o in affected
            )
            result["recovery"] = []
            for _ in range(3):
                control.step()
                sim.step()
                result["recovery"].append(
                    {
                        "actual": metrics(
                            sim, sim.now - timedelta(hours=1), sim.now, fee_segment
                        ),
                        "control": metrics(
                            control, sim.now - timedelta(hours=1), sim.now, fee_segment
                        ),
                    }
                )
            result["recovery_control"] = metrics(
                control, sim.now - timedelta(hours=1), sim.now, fee_segment
            )
            result["recovery_actual"] = metrics(
                sim, sim.now - timedelta(hours=1), sim.now, fee_segment
            )
            result["recovery_prices"] = [
                {
                    "sku": o.sku,
                    "seller": o.seller_id,
                    "price": o.price,
                    "floor": o.floor,
                    "stock": o.stock,
                }
                for offer in affected
                for o in sim.world.offers[offer.sku]
            ]
            (directory / "measurements.json").write_text(
                json.dumps(result, default=str, indent=2)
            )
            return result


def test_s8_selection_and_tuning(run: dict[str, Any]) -> None:
    assert 2 <= len(run["offers"]) <= 4
    assert run["seller"] not in ("S-03", "S-07", "S-12", "S-99")
    if run["seed"] == 42:
        assert run["seller"] == "S-24"
        assert {o["sku"] for o in run["offers"]} == {"E01", "E03"}
    assert run["actual"][0] / run["control"][0] <= 0.7
    assert run["actual"][1] / run["control"][1] <= 0.7
    assert set(run["sellout_hours"]) == {o["sku"] for o in run["offers"]}
    assert all(4 <= hours <= 8 for hours in run["sellout_hours"].values())


def test_s8_late_partitions_and_no_change_log(run: dict[str, Any]) -> None:
    assert {r[0] for r in run["partitions"]} == {"DE", "FR"}
    for country, expected, loaded in run["partitions"]:
        assert loaded == round(expected * 0.5)
        assert loaded / expected == pytest.approx(0.5, abs=1 / expected)
        assert run["loaded_counts"][country] == loaded
    assert all(expected == loaded for _, expected, loaded in run["backfilled"])
    assert run["changes_match"]


def test_s8_price_ordering_and_manual_evidence(run: dict[str, Any]) -> None:
    start = datetime.fromisoformat(run["start"])
    assert start.hour == 10
    customers = {s["seller_id"]: s for s in run["private"]["sellers"]}
    for offer in run["offers"]:
        assert offer["price"] == round(offer["old_price"] * 0.1, 2)
        history = [r for r in run["offer_history"] if r[1] == offer["sku"]]
        before = {r[2]: r[3] for r in history if r[0] < start}
        changes = [r for r in history if r[3] != before[r[2]]]
        assert next(r[0] for r in changes if r[2] == run["seller"]) == start
        assert all(r[0] >= start for r in changes if r[2] != run["seller"])
        entries = customers[run["seller"]]["price_history"]
        manual = [
            e for e in entries if e["ts"] == run["start"] and e["sku"] == offer["sku"]
        ]
        assert len(manual) == 1
        assert manual[0]["source"] == "manual"
        assert manual[0]["price"] == offer["price"] < offer["cost"]
    assert all(
        e.get("source", "repricer") == "repricer"
        for seller in customers.values()
        if seller["seller_id"] != run["seller"]
        for e in seller["price_history"]
    )


def test_s8_clear_and_recovery(run: dict[str, Any]) -> None:
    assert run["restored"]
    assert any(
        all(
            actual / control == pytest.approx(1, abs=0.1)
            for actual, control in zip(tick["actual"], tick["control"], strict=True)
        )
        for tick in run["recovery"]
    ), run["recovery"]


def test_s8_scheduled_clear_and_lifecycle_errors(
    settings: Settings, tmp_path: Path
) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        manager = ScenarioManager(sim)
        assert manager.active() == []
        with pytest.raises(ValueError, match="not injected"):
            manager.clear("S8")
        with pytest.raises(ValueError, match="aware current or future whole hour"):
            manager.inject("S8", sim.now - timedelta(hours=1))
        prices = [
            (o.price, o.stock, o.repricer_mode)
            for g in sim.world.offers.values()
            for o in g
        ]
        manager.inject("S8", sim.now + timedelta(hours=2))
        with pytest.raises(ValueError, match="already injected"):
            manager.inject("S8")
        manager.clear("S8")
        assert prices == [
            (o.price, o.stock, o.repricer_mode)
            for g in sim.world.offers.values()
            for o in g
        ]
        assert not manager.active()
        due = sim.now + timedelta(hours=2)
        manager.inject("S8", due)
        sim.step()
        sim.step()
        assert not manager.active()
        sim.step()
        assert manager.active()[0].injected_ts == due
        offers = manager._s8_original.copy()
        stock = [o.stock for o, _, _ in offers]
        assert all(o.repricer_mode == "fixed" for o, _, _ in offers)
        manager.clear("S8")
        assert [(o.price, o.repricer_mode) for o, _, _ in offers] == [
            (price, mode) for _, price, mode in offers
        ]
        assert [o.stock for o, _, _ in offers] == stock


def test_s8_graph_seed() -> None:
    edges = yaml.safe_load(Path("config/causal_seeds.yaml").read_text())
    assert any(
        e["src"] == "price_change_rate" and e["dst"] == "aov" and e["lag_hours"] == 0
        for e in edges
    )


def test_s8_empty_catalog_error(settings: Settings) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        sim.world.offers.clear()
        manager = ScenarioManager(sim)
        with pytest.raises(ValueError, match="eligible electronics offer"):
            manager.inject("S8")
        assert manager.timeline() == []
