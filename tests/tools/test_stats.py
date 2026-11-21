from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter

import duckdb
import pytest
import yaml

from eye.config import Settings
from eye.contracts import GraphView
from eye.registry.loader import load_registry
from eye.sim.engine import Simulator
from eye.sim.store import Store
from eye.tools.drill import compare_segments, drill_down, first_change
from eye.tools.models import (
    BaselineCompareInput,
    CompareSegmentsInput,
    DrillDownInput,
    FirstChangeInput,
    ScanAnomaliesInput,
    ScanDivergentSegmentsInput,
)
from eye.tools.stats import (
    baseline_compare,
    expected,
    scan_anomalies,
    scan_divergent_segments,
)

T0 = datetime(2026, 11, 27, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = 24 * HOUR
WEEK = 7 * DAY


@pytest.fixture
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    with closing(Store(":memory:")) as store, store.cursor() as con:
        con.execute(
            """
            CREATE TEMP TABLE fixture_hours AS
            SELECT ts, country, platform,
                   CASE WHEN country = 'DE' THEN 120 ELSE 80 END AS units,
                   CASE WHEN country = 'DE' THEN 1200 ELSE 800 END AS searches,
                   CASE WHEN country = 'DE' THEN 12000 ELSE 8000 END AS sessions
            FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ, INTERVAL 1 HOUR) t(ts),
                 unnest(['DE', 'FR']) c(country), unnest(['web', 'ios']) p(platform);
            """,
            [T0 - 28 * DAY, T0 + 12 * HOUR],
        )
        con.execute(
            """
            INSERT INTO orders_hourly
            SELECT ts, country, platform, 'electronics', 'E01', '1P', '1P', 'search',
                   units, units * 100, units * 10, units * 50, 0, 0 FROM fixture_hours;
            INSERT INTO search_hourly
            SELECT ts, country, platform, 'electronics', searches, searches * 0.45, 0.85
            FROM fixture_hours;
            INSERT INTO traffic_hourly
            SELECT ts, country, platform, sessions, sessions * 0.5, sessions * 0.25, sessions * 0.25
            FROM fixture_hours;
            INSERT INTO checkout_hourly
            SELECT ts, country, platform, 1000, 800, 720 FROM fixture_hours;
            """
        )
        yield con


@pytest.mark.parametrize("name", ["orders", "sessions", "search_ctr"])
def test_baseline_clean_hours(world: duckdb.DuckDBPyConnection, name: str) -> None:
    result = baseline_compare(
        BaselineCompareInput(name=name, start=T0, end=T0 + 12 * HOUR),
        con=world,
        registry=load_registry(world),
    )
    assert len(result.points) == 12
    assert sum(p.z is not None and abs(p.z) < 3 for p in result.points) / 12 >= 0.99
    assert result.actual == pytest.approx(result.expected)
    assert result.delta == pytest.approx(0)


def test_baseline_window_uses_only_hours_with_both_samples(
    world: duckdb.DuckDBPyConnection,
) -> None:
    world.execute("DELETE FROM traffic_hourly WHERE ts < ? AND hour(ts) = 1", [T0])
    result = baseline_compare(
        BaselineCompareInput(name="sessions", start=T0, end=T0 + 2 * HOUR),
        con=world,
        registry=load_registry(world),
    )
    assert result.points[1].actual == result.points[0].actual
    assert result.points[1].expected is None
    assert result.actual == result.expected == 40_000
    assert result.delta == pytest.approx(0)


def test_baseline_fallback_applies_weekday_scaling(
    world: duckdb.DuckDBPyConnection,
) -> None:
    world.execute(
        "UPDATE traffic_hourly SET sessions = sessions * 2 WHERE dayofweek(ts) = 5"
    )
    for i in range(1, 4):
        world.execute(
            "INSERT INTO partition_status VALUES ('traffic_hourly', 'DE', ?, 10, 8, ?)",
            [T0 - i * WEEK, T0 - i * WEEK],
        )
    result = expected(
        "sessions", T0, {"country": "DE"}, con=world, registry=load_registry(world)
    )
    # Excluded Friday observations also do not contribute to the daily scaling totals.
    factor = (2 - 6 / (4 * 24)) / (8 / 7 - 6 / (28 * 24))
    assert result.expected == pytest.approx(24_000 * factor)
    assert result.lower == pytest.approx(result.expected)
    assert result.z == pytest.approx(
        (48_000 - result.expected) / (0.02 * result.expected)
    )


def test_ratio_baseline_is_ratio_of_component_medians(
    world: duckdb.DuckDBPyConnection,
) -> None:
    for i, (num, den) in enumerate(((10, 10), (20, 100), (30, 100), (40, 40)), 1):
        world.execute(
            "UPDATE search_hourly SET result_clicks = ?, searches = ? WHERE ts = ?",
            [num, den, T0 - i * WEEK],
        )
    result = expected("search_ctr", T0, {}, con=world, registry=load_registry(world))
    assert result.expected == pytest.approx(25 / 70)
    assert result.expected != pytest.approx((0.3 + 1) / 2)
    assert result.spread == pytest.approx(0.35 * 1.4826)


def test_s3_drill_and_first_change(world: duckdb.DuckDBPyConnection) -> None:
    start, end = T0 + HOUR, T0 + 4 * HOUR
    world.execute(
        "UPDATE orders_hourly SET units = units / 2 WHERE country = 'DE' AND ts >= ? AND ts < ?",
        [start, end],
    )
    registry = load_registry(world)
    result = drill_down(
        DrillDownInput(metric="search_conversion", dim="country", start=start, end=end),
        con=world,
        registry=registry,
    )
    assert result.rows[0].value == "DE"
    assert result.rows[0].share_of_total_delta >= 0.8
    assert [r.value for r in result.rows if r.focus] == ["DE"]
    change = first_change(
        FirstChangeInput(
            metrics=["search_conversion", "search_ctr"], start=T0, end=end
        ),
        con=world,
        registry=registry,
    )
    assert change.changes == {"search_conversion": start, "search_ctr": None}


def test_s7_compare_web_and_ios(world: duckdb.DuckDBPyConnection) -> None:
    world.execute(
        "UPDATE checkout_hourly SET checkout_starts = 120 WHERE ts >= ? AND platform = 'web'",
        [T0],
    )
    result = compare_segments(
        CompareSegmentsInput(
            metric="cart_to_checkout_rate",
            dim="platform",
            value_a="web",
            value_b="ios",
            start=T0,
            end=T0 + 3 * HOUR,
        ),
        con=world,
        registry=load_registry(world),
    )
    assert result.a.delta_pct == pytest.approx(-0.85)
    assert result.b.delta_pct == pytest.approx(0.0)


def test_scans_rank_segments_and_keep_divergence_with_healthy_total(
    world: duckdb.DuckDBPyConnection,
) -> None:
    world.execute(
        "UPDATE orders_hourly SET units = CASE WHEN country = 'DE' THEN 80 ELSE 120 END WHERE ts >= ?",
        [T0],
    )
    registry = load_registry(world)
    graph = GraphView(nodes=[{"id": "orders", "impact_weight": 0.8}], edges=[])
    start = perf_counter()
    point = scan_anomalies(
        ScanAnomaliesInput(ts=T0), con=world, registry=registry, graph=graph
    )
    assert perf_counter() - start < 3
    assert 0 < len(point.anomalies) <= 30
    assert [a.priority for a in point.anomalies] == sorted(
        (a.priority for a in point.anomalies), reverse=True
    )
    de = next(
        a
        for a in point.anomalies
        if a.metric == "orders" and a.segment == {"country": "DE"}
    )
    assert de.priority == pytest.approx(abs(de.z) * 0.8)
    result = scan_divergent_segments(
        ScanDivergentSegmentsInput(ts=T0 + 5 * HOUR),
        con=world,
        registry=registry,
    )
    de = [
        a
        for a in result.anomalies
        if a.metric == "orders" and a.segment == {"country": "DE"}
    ]
    assert {a.kind for a in de} == {"divergent_segment", "share_shift"}
    assert all(a.total_z == 0 and a.window_hours == 6 for a in de)
    shift = next(a for a in de if a.kind == "share_shift")
    assert shift.share_now == pytest.approx(0.4)
    assert shift.share_expected == pytest.approx(0.6)


def _has_s6() -> bool:
    config = Path("config/scenarios.yaml")
    return config.exists() and "S6" in yaml.safe_load(config.read_text())


@pytest.mark.skipif(not _has_s6(), reason="T05 S6 scenario is not available")
def test_s6_small_seller_divergence(tmp_path: Path) -> None:
    from eye.sim.scenarios import inject

    settings = Settings()
    with closing(Simulator(settings, db_path=tmp_path / "s6.duckdb")) as sim:
        sim.backfill()
        inject(sim, "S6")
        for _ in range(13):
            sim.step()
        with sim.store.cursor() as con:
            result = scan_divergent_segments(
                ScanDivergentSegmentsInput(ts=settings.sim.start_ts + 12 * HOUR),
                con=con,
                registry=load_registry(con),
            )
    assert any(
        a.metric == "small_seller_gmv_share"
        or (a.metric == "buybox_share" and a.segment.get("seller_tier") == "small_3P")
        for a in result.anomalies
    )
