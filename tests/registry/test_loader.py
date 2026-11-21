import logging
import shutil
from pathlib import Path

import duckdb
import pytest
import yaml

from eye.registry.loader import (
    CONFIG_DIR,
    formula_inputs,
    load_registry,
    source_tables,
)

SEED_METRICS = [
    "gmv", "orders", "aov", "fee_revenue", "first_party_margin", "platform_discounts",
    "fulfillment_cost", "ad_revenue", "marketing_cost", "refund_cost", "refunds",
    "contribution_margin", "contribution_per_order", "sessions", "searches", "search_ctr",
    "search_relevance", "search_conversion", "feed_impressions", "feed_ctr",
    "feed_purchase_rate", "feed_unavailable_exposure_share", "carts", "cart_rate",
    "cart_to_checkout_rate", "checkout_to_payment_rate", "payment_success_rate",
    "payment_latency_p95", "checkout_conversion", "client_error_rate", "buybox_share",
    "small_seller_gmv_share", "price_index_vs_1p", "price_change_rate", "active_small_sellers",
    "seller_hhi", "in_stock_rate", "stockout_skus", "po_fill_rate", "po_qty_vs_forecast",
    "supplier_delay_hours", "fba_blocked_units", "ad_impressions", "ad_clicks", "ad_spend",
    "avg_cpc", "ad_ctr", "budget_pace_ratio",
]  # fmt: skip

# Tables T01 fills; the rest are filled by T02/T03, so their metrics stay pending for now.
T01_METRICS = {
    "gmv", "orders", "aov", "sessions", "searches", "search_ctr", "search_relevance",
    "search_conversion", "feed_impressions", "feed_ctr", "feed_purchase_rate",
    "feed_unavailable_exposure_share", "carts", "cart_rate", "cart_to_checkout_rate",
    "checkout_to_payment_rate", "payment_success_rate", "checkout_conversion",
    "buybox_share", "small_seller_gmv_share", "active_small_sellers", "seller_hhi",
}  # fmt: skip


def _config(tmp_path: Path, metrics: list[dict], *, seeds: bool = False) -> Path:
    for name in ("identities.yaml", "causal_seeds.yaml"):
        if seeds:
            shutil.copy(CONFIG_DIR / name, tmp_path / name)
        else:
            (tmp_path / name).write_text("[]")
    (tmp_path / "metrics.yaml").write_text(yaml.safe_dump(metrics))
    return tmp_path


def _seed(name: str) -> dict:
    return next(
        m
        for m in yaml.safe_load((CONFIG_DIR / "metrics.yaml").read_text())
        if m["name"] == name
    )


def test_seed_registry_matches_spec(registry) -> None:
    assert list(registry.metrics) == SEED_METRICS
    assert all(m.added_by == "seed" for m in registry.metrics.values())
    assert [i.id for i in registry.identities] == [f"F{n}" for n in range(1, 9)]
    assert len(registry.causal_seeds) == 24
    assert {m.name for m in registry.active} == T01_METRICS


def test_pending_metrics_warn(con, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="eye.registry.loader"):
        registry = load_registry(con)
    warned = {r.getMessage() for r in caplog.records}
    pending = [m.name for m in registry.metrics.values() if m.status == "pending"]
    assert pending
    for name in pending:
        assert any(f'"metric": "{name}"' in message for message in warned)
    assert registry.metrics["contribution_margin"].status == "pending"


def test_metric_becomes_active_once_its_table_has_data(writable) -> None:
    assert load_registry(writable).metrics["marketing_cost"].status == "pending"
    writable.execute(
        "INSERT INTO marketing_hourly SELECT max(ts), 'DE', 'search_ads', 10.0, 5 FROM orders_hourly"
    )
    assert load_registry(writable).metrics["marketing_cost"].status == "active"


def test_missing_table_marks_metric_pending(writable) -> None:
    writable.execute("DROP TABLE client_errors_hourly")
    registry = load_registry(writable)
    assert registry.metrics["client_error_rate"].status == "pending"
    assert registry.source_tables["client_error_rate"] == (
        "client_errors_hourly",
        "traffic_hourly",
    )


def test_source_tables_from_from_and_join() -> None:
    sql = _seed("po_qty_vs_forecast")["sql"]
    assert source_tables(sql) == (
        "orders_hourly",
        "offers_hourly",
        "purchase_orders",
        "skus",
    )
    assert source_tables(_seed("seller_hhi")["sql"]) == ("orders_hourly",)
    cte = 'WITH t AS (SELECT * FROM "main"."orders_hourly") SELECT ts, 1 AS num FROM t JOIN skus USING (sku)'
    assert source_tables(cte) == ("orders_hourly", "skus")


def test_formula_source_tables_are_the_union_of_inputs(registry) -> None:
    assert set(registry.source_tables["aov"]) == {"orders_hourly"}
    assert set(registry.source_tables["contribution_margin"]) == {
        "orders_hourly", "ads_hourly", "marketing_hourly", "refunds_hourly",
    }  # fmt: skip


def test_formula_inputs_rejects_anything_but_arithmetic() -> None:
    assert formula_inputs("(a_x + b_y) / c_z") == ["a_x", "b_y", "c_z"]
    for bad in ["gmv ** 2", "abs(gmv)", "gmv / 2", "gmv.real", "gmv +"]:
        with pytest.raises(ValueError, match="formula"):
            formula_inputs(bad)


@pytest.mark.parametrize(
    ("name", "update", "error"),
    [
        ("aov", {"name": "gmv"}, "duplicate metric name: gmv"),
        ("aov", {"expression": "gmv / nothing"}, "unknown metrics nothing"),
        ("aov", {"dimensions": ["psp"]}, "not shared by all inputs"),
        ("orders", {"dimensions": ["country", "psp"]}, "SQL returns columns"),
        ("orders", {"sql": "SELECT ts, nope AS num FROM orders_hourly", "dimensions": []}, "invalid SQL"),
        ("orders", {"sql": "SELECT ts, 1 AS num FROM scenario_log", "dimensions": []}, "scenario_log"),
        ("orders", {"sql": 'SELECT ts, 1 AS num FROM main."scenario_log"', "dimensions": []}, "scenario_log"),
    ],
)  # fmt: skip
def test_invalid_registry_is_rejected(con, tmp_path, name, update, error) -> None:
    base = ["gmv"] if name == "orders" else ["gmv", "orders"]
    metrics = [*map(_seed, base), {**_seed(name), **update}]
    with pytest.raises(ValueError, match=error):
        load_registry(con, _config(tmp_path, metrics))


def test_identities_must_use_known_metrics(con, tmp_path) -> None:
    with pytest.raises(ValueError, match="identity F1: unknown metric"):
        load_registry(
            con, _config(tmp_path, [_seed("gmv"), _seed("orders")], seeds=True)
        )


def test_loader_reads_only_through_cursors(world) -> None:
    with duckdb.connect(str(world), read_only=True) as con:
        load_registry(con)
        assert con.execute("SELECT 1").fetchone() == (1,)
