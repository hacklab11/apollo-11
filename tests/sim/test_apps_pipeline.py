from datetime import UTC, datetime, timedelta

import duckdb
import numpy as np
import pytest

from eye.sim.apps import Apps
from eye.sim.changelog import add_change, add_external_status
from eye.sim.pipeline import Pipeline


def test_baseline_errors_partitions_and_noise(
    reader: duckdb.DuckDBPyConnection,
) -> None:
    sessions = reader.execute("SELECT sum(sessions) FROM traffic_hourly").fetchone()[0]
    errors = reader.execute("SELECT sum(errors) FROM client_errors_hourly").fetchone()[
        0
    ]
    assert 0.8 <= errors / (sessions * 0.002) <= 1.2
    assert (
        reader.execute(
            "SELECT count(*) FROM partition_status WHERE expected_rows != loaded_rows OR loaded_at != ts + INTERVAL '1 hour'"
        ).fetchone()[0]
        == 0
    )
    assert reader.execute("SELECT count(*) FROM partition_status").fetchone()[0] > 0
    assert reader.execute("SELECT count(*) FROM change_log").fetchone()[0] == 28


def test_app_rollouts_and_errors() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    apps = Apps()
    apps.release("android", "app-android v2", ts)
    assert apps.adoption("android", ts)["app-android v2"] == 0.2
    assert apps.adoption("android", ts + timedelta(days=4)) == {"app-android v2": 1.0}
    apps.release("checkout-web", "checkout-web v6", ts)
    assert apps.adoption("checkout-web", ts) == {"checkout-web v6": 1.0}
    batches = {}
    apps.errors(ts, np.random.default_rng(42), batches)
    assert all(row[-1] == 0 for row in batches["client_errors_hourly"])
    with pytest.raises(ValueError, match="unknown app"):
        apps.release("unknown", "v2", ts)


def test_partition_and_event_hooks() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    batches = {"traffic_hourly": [(ts, "DE", "web", 100)] * 10}
    pipeline = Pipeline(
        completeness={("traffic_hourly", "DE"): 0.5},
        delay_hours={("traffic_hourly", "DE"): 3},
    )
    pipeline.partitions(ts, batches)
    assert batches["partition_status"][0] == (
        "traffic_hourly",
        "DE",
        ts,
        10,
        5,
        ts + timedelta(hours=3),
    )
    assert batches["partition_status"][1][3:5] == (0, 0)
    add_change(batches, ts, "checkout-web")
    add_external_status(batches, ts, "PSP-1", "incident", "Timeouts")
    with pytest.raises(ValueError, match="invalid external status"):
        add_external_status(batches, ts, "PSP-1", "bad", "")
    pipeline.completeness[("traffic_hourly", "DE")] = 2
    with pytest.raises(ValueError, match="completeness"):
        pipeline.partitions(ts, batches)
