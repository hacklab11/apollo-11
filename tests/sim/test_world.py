from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from itertools import chain

import duckdb
import pytest

from eye.sim.store import TABLES, Store
from eye.sim.world import build_world


@pytest.mark.parametrize("seed", [0, 42, 8675309])
def test_seeded_catalog(seed: int) -> None:
    joined_ts = datetime(2026, 10, 30, tzinfo=UTC)
    world = build_world(seed, joined_ts=joined_ts)
    assert world == build_world(seed, joined_ts=joined_ts)
    assert world != build_world(seed + 1, joined_ts=joined_ts)
    assert Counter(sku.category for sku in world.skus.values()) == {
        "electronics": 12,
        "home": 10,
        "fashion": 10,
        "toys": 8,
    }
    assert len(world.suppliers) == 6
    assert all(s.supplier_id in world.suppliers for s in world.skus.values())
    assert len(world.sellers) == 31
    assert world.sellers["S-99"].joined_ts is None
    assert len(world.advertisers) == 8

    unavailable = {
        sku for sku, offers in world.offers.items() if not any(o.stock for o in offers)
    }
    assert unavailable == {"E11", "F09", "T07"}
    assert all(1.4 <= world.skus[sku].attractiveness <= 1.5 for sku in unavailable)
    for offers in world.offers.values():
        assert 3 <= len({offer.seller_id for offer in offers}) == len(offers) <= 5
    for rank in range(1, 11):
        sellers = {o.seller_id for o in world.offers[f"E{rank:02d}"]}
        assert "1P" in sellers
        assert sum(seller.startswith("S-") for seller in sellers) >= 2
    catalog_sizes = Counter(
        o.seller_id for o in chain.from_iterable(world.offers.values())
    )
    assert catalog_sizes["1P"] == 20
    assert catalog_sizes["S-99"] == 0
    for seller in world.sellers.values():
        if seller.tier == "large_3P":
            assert 6 <= catalog_sizes[seller.seller_id] <= 10
        if seller.tier == "small_3P" and seller.seller_id != "S-99":
            assert 2 <= catalog_sizes[seller.seller_id] <= 4


def test_static_schema(reader: duckdb.DuckDBPyConnection) -> None:
    assert {row[0] for row in reader.execute("SHOW TABLES").fetchall()} == set(TABLES)
    assert reader.execute(
        "SELECT platform, source FROM app_components WHERE component = 'checkout' ORDER BY platform"
    ).fetchall() == [
        ("android", "checkout-web"),
        ("ios", "native"),
        ("web", "checkout-web"),
    ]
    assert reader.execute("SELECT count(*) FROM scenario_log").fetchone() == (0,)
    columns = reader.execute(
        "SELECT column_name FROM information_schema.columns"
    ).fetchall()
    assert not {"floor", "cost", "repricer_mode", "cogs_ratio"} & {
        c[0] for c in columns
    }


def test_append_rolls_back_on_failure() -> None:
    with closing(Store(":memory:")) as store, store.cursor() as reader:
        seller = ("test", "small_3P", 0.9, None)
        with pytest.raises(duckdb.Error):
            store.append({"sellers": [seller], "skus": [("bad row",)]})
        assert reader.execute("SELECT count(*) FROM sellers").fetchone() == (0,)
        store.append({"sellers": [seller]})
        assert reader.execute("SELECT seller_id FROM sellers").fetchall() == [("test",)]
