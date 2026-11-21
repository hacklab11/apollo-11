from datetime import timedelta

import pytest

from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry, load_registry


def _by_ts(series) -> dict:
    return {p.ts: p.value for p in series.points}


# One row per purchase-order event or per day, so most hours have no value.
SPARSE_METRICS = {
    "po_fill_rate", "po_qty_vs_forecast", "supplier_delay_hours", "fba_blocked_units",
}  # fmt: skip


def test_every_metric_runs_at_total_and_each_dimension(con, registry, day) -> None:
    # Pending metrics read empty tables today; run them anyway so their SQL is exercised.
    active = Registry(
        {
            n: m.model_copy(update={"status": "active"})
            for n, m in registry.metrics.items()
        },
        registry.source_tables,
        registry.has_den,
        registry.identities,
        registry.causal_seeds,
    )
    engine = MetricEngine(con, active)
    for metric in registry.metrics.values():
        total = engine.query_metric(metric.name, *day)
        if metric.status == "active":
            assert len(total) == 1, metric.name
            if metric.name not in SPARSE_METRICS:
                assert len(total[0].points) == 24, metric.name
            assert any(p.value is not None for p in total[0].points), metric.name
        for dimension in metric.dimensions:
            for series in engine.query_metric(metric.name, *day, group_by=[dimension]):
                assert set(series.segment) == {dimension}


def test_orders_total_equals_sum_over_countries(engine, day) -> None:
    total = _by_ts(engine.query_metric("orders", *day)[0])
    by_country = engine.query_metric("orders", *day, group_by=["country"])
    assert {s.segment["country"] for s in by_country} == {"DE", "FR"}
    summed = {ts: sum(_by_ts(s).get(ts, 0) for s in by_country) for ts in total}
    assert summed == total


def test_search_ctr_total_is_pooled_not_mean_of_segments(con, engine, day) -> None:
    pooled = dict(
        con.execute(
            "SELECT ts, SUM(result_clicks) / SUM(searches) FROM search_hourly "
            "WHERE ts >= ? AND ts < ? GROUP BY ts",
            list(day),
        ).fetchall()
    )
    total = _by_ts(engine.query_metric("search_ctr", *day)[0])
    assert total == pytest.approx(pooled)

    by_country = engine.query_metric("search_ctr", *day, group_by=["country"])
    means = {
        ts: sum(_by_ts(s)[ts] for s in by_country) / len(by_country) for ts in total
    }
    assert max(abs(total[ts] - means[ts]) for ts in total) > 1e-6


def test_aov_is_gmv_over_orders_per_ts(engine, day) -> None:
    for segment in ({}, {"country": "FR"}):
        gmv = _by_ts(engine.query_metric("gmv", *day, segment)[0])
        orders = _by_ts(engine.query_metric("orders", *day, segment)[0])
        aov = _by_ts(engine.query_metric("aov", *day, segment)[0])
        assert aov == pytest.approx({ts: gmv[ts] / orders[ts] for ts in gmv})


def test_segment_filter_matches_group_by(engine, day) -> None:
    grouped = engine.query_metric("cart_rate", *day, group_by=["platform"])
    ios = next(s for s in grouped if s.segment == {"platform": "ios"})
    filtered = engine.query_metric("cart_rate", *day, {"platform": "ios"})
    assert filtered == [ios]

    nested = engine.query_metric("orders", *day, {"country": "DE"}, ["channel"])
    assert {s.segment["country"] for s in nested} == {"DE"}
    assert {s.segment["channel"] for s in nested} >= {"search", "feed"}


@pytest.mark.parametrize(
    ("segment", "group_by"), [({"psp": "adyen"}, []), ({}, ["seller_tier"])]
)
def test_unknown_dimension_lists_allowed(engine, day, segment, group_by) -> None:
    with pytest.raises(ValueError, match=r"allowed: \['country', 'platform'\]"):
        engine.query_metric("sessions", *day, segment, group_by)
    with pytest.raises(ValueError, match="unknown metric"):
        engine.query_metric("nope", *day)


def test_pending_metric_is_not_queried(writable, day) -> None:
    writable.execute("DELETE FROM ads_hourly")
    engine = MetricEngine(writable, load_registry(writable))
    with pytest.raises(ValueError, match="ad_spend is pending"):
        engine.query_metric("ad_spend", *day)


def test_metric_value_and_window(con, engine, day) -> None:
    start, end = day
    hourly = engine.query_metric("orders", start, end)[0].points
    assert engine.metric_value("orders", start) == hourly[0].value
    assert engine.metric_value("orders", end) is None
    assert engine.metric_window("orders", start, end) == sum(p.value for p in hourly)

    clicks, searches = con.execute(
        "SELECT SUM(result_clicks), SUM(searches) FROM search_hourly "
        "WHERE ts >= ? AND ts < ? AND country = 'FR'",
        [start, end],
    ).fetchone()
    assert engine.metric_window(
        "search_ctr", start, end, {"country": "FR"}
    ) == pytest.approx(clicks / searches)
    window_aov = engine.metric_window("aov", start, end)
    assert window_aov == pytest.approx(
        engine.metric_window("gmv", start, end)
        / engine.metric_window("orders", start, end)
    )
    assert engine.metric_window("orders", end, end + timedelta(hours=3)) is None


def test_results_are_cached_until_invalidate(writable, day) -> None:
    engine = MetricEngine(writable, load_registry(writable))
    start, end = day
    first = engine.query_metric("sessions", start, end, {"country": "DE"})
    writable.execute(
        "UPDATE traffic_hourly SET sessions = sessions * 2 WHERE country = 'DE'"
    )
    assert engine.query_metric("sessions", start, end, {"country": "DE"}) == first

    engine.invalidate()
    doubled = engine.query_metric("sessions", start, end, {"country": "DE"})
    assert _by_ts(doubled[0]) == {ts: 2 * v for ts, v in _by_ts(first[0]).items()}


def test_incomplete_hours_keep_value_and_lower_completeness(writable, day) -> None:
    start, end = day
    late = start + timedelta(hours=5)
    writable.execute(
        "DELETE FROM partition_status WHERE table_name = 'orders_hourly' AND ts = ?",
        [late],
    )
    writable.execute(
        "INSERT INTO partition_status VALUES "
        "('orders_hourly', 'DE', ?, 100, 40, ?), ('orders_hourly', 'FR', ?, 100, 100, ?)",
        [late, late, late, late],
    )
    engine = MetricEngine(writable, load_registry(writable))
    by_country = {
        s.segment["country"]: s.points
        for s in engine.query_metric("orders", start, end, group_by=["country"])
    }
    de = {p.ts: p for p in by_country["DE"]}
    assert de[late].completeness == pytest.approx(0.4)
    assert de[late].value is not None
    assert all(p.completeness == 1.0 for ts, p in de.items() if ts != late)
    assert {p.ts: p.completeness for p in by_country["FR"]}[late] == 1.0

    total = {
        p.ts: p.completeness
        for p in engine.query_metric("orders", start, end)[0].points
    }
    assert total[late] == pytest.approx(0.7)
    aov = engine.query_metric("aov", start, end, {"country": "DE"})[0].points
    assert {p.ts: p.completeness for p in aov}[late] == pytest.approx(0.4)
    assert (
        engine.query_metric("sessions", start, end, {"country": "DE"})[0]
        .points[5]
        .completeness
        == 1.0
    )


def test_null_denominators_give_no_value(writable, day) -> None:
    start, end = day
    writable.execute("UPDATE search_hourly SET searches = NULL WHERE country = 'DE'")
    engine = MetricEngine(writable, load_registry(writable))
    points = engine.query_metric("search_ctr", start, end, {"country": "DE"})[0].points
    assert points and all(p.value is None for p in points)
    assert engine.metric_window("search_ctr", start, end, {"country": "DE"}) is None
