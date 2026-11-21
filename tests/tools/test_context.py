from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from eye.registry.loader import load_registry
from eye.sim.store import Store
from eye.tools.context import check_freshness, list_events, planned_effect_check
from eye.tools.models import (
    CheckFreshnessInput,
    ListEventsInput,
    PlannedEffectCheckInput,
)

T0 = datetime(2026, 11, 27, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    with closing(Store(":memory:")) as store, store.cursor() as con:
        con.execute(
            """
            INSERT INTO orders_hourly
            SELECT ts, country, 'web', 'electronics', 'E01', '1P', '1P', 'search',
                   100, 5000, 0, 0, 0, 0
            FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ, INTERVAL 1 HOUR) t(ts),
                 unnest(['DE', 'FR']) c(country)
            """,
            [T0 - 28 * 24 * HOUR, T0 + 3 * HOUR],
        )
        con.execute(
            """
            INSERT INTO change_log VALUES
            ('i1', ?, 'growth', 'campaign', true, '{}', 'Black Friday', '{"orders":"+35%","gmv":10500}'),
            ('s3', ?, 'search-ranker', 'release', false, '{"country":"DE"}', 'v7.2', NULL)
            """,
            [T0, T0],
        )
        con.execute(
            "INSERT INTO external_status VALUES ('psp', ?, 'PSP-2', 'degraded', 'Latency')",
            [T0],
        )
        con.execute(
            "INSERT INTO scenario_log VALUES ('hidden', ?, 'I1', 'inject', '{}')", [T0]
        )
        yield con


def test_planned_effect_i1_alone_and_with_s7(world: duckdb.DuckDBPyConnection) -> None:
    registry = load_registry(world)
    query = PlannedEffectCheckInput(
        event_id="i1", metric="orders", start=T0, end=T0 + 3 * HOUR
    )
    world.execute(
        "UPDATE orders_hourly SET units = 135, gmv = 6750 WHERE ts >= ?", [T0]
    )
    result = planned_effect_check(query, con=world, registry=registry)
    assert result.verdict == "matches"
    assert result.observed_effect == pytest.approx(0.35)
    assert result.explained_share == pytest.approx(1)
    numeric = planned_effect_check(
        query.model_copy(update={"metric": "gmv"}), con=world, registry=registry
    )
    assert numeric.verdict == "matches"
    assert numeric.observed_effect == 10500
    world.execute("UPDATE orders_hourly SET units = 90 WHERE ts >= ?", [T0])
    result = planned_effect_check(query, con=world, registry=registry)
    assert result.verdict == "opposite"
    assert result.observed_effect == pytest.approx(-0.10)
    assert result.explained_share == 0


def test_i2_freshness_before_and_after_backfill(
    world: duckdb.DuckDBPyConnection,
) -> None:
    for ts in (T0, T0 + HOUR, T0 + 2 * HOUR):
        world.execute(
            "INSERT INTO partition_status VALUES "
            "('orders_hourly', 'FR', ?, 100, 40, ?), ('orders_hourly', 'DE', ?, 100, 100, ?)",
            [ts, ts + 6 * HOUR, ts, ts],
        )
    registry = load_registry(world)
    query = CheckFreshnessInput(
        metric="orders",
        start=T0,
        end=T0 + 3 * HOUR,
        segment={"country": "FR"},
    )
    result = check_freshness(query, con=world, registry=registry)
    assert result.verdict == "incomplete"
    assert [p.completeness for p in result.points] == [0.4, 0.4, 0.4]
    assert len(result.late_hours) == 3

    world.execute(
        "UPDATE partition_status SET loaded_rows = expected_rows WHERE country = 'FR'"
    )
    assert check_freshness(query, con=world, registry=registry).verdict == "late"
    world.execute("UPDATE partition_status SET loaded_at = ts WHERE country = 'FR'")
    assert check_freshness(query, con=world, registry=registry).verdict == "ok"


def test_list_events_filters_and_excludes_ground_truth(
    world: duckdb.DuckDBPyConnection,
) -> None:
    query = ListEventsInput(start=T0, end=T0 + HOUR)
    assert {e.id for e in list_events(query, con=world).events} == {"i1", "s3", "psp"}
    assert {
        e.id
        for e in list_events(
            query.model_copy(update={"country": "FR"}), con=world
        ).events
    } == {
        "i1",
        "psp",
    }
    external = list_events(
        query.model_copy(
            update={"system": "PSP-2", "kind": "external_status", "planned": False}
        ),
        con=world,
    )
    assert [e.status for e in external.events] == ["degraded"]
    assert [
        e.id
        for e in list_events(
            query.model_copy(update={"planned": True}), con=world
        ).events
    ] == ["i1"]
    with pytest.raises(ValueError, match="not a tool data source"):
        check_freshness(
            CheckFreshnessInput(tables=["scenario_log"], start=T0, end=T0 + HOUR),
            con=world,
            registry=load_registry(world),
        )
