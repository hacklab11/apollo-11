from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from eye.config import Settings
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.sim import engine as sim_engine
from eye.sim.apps import Apps
from eye.sim.changelog import add_change, add_external_status
from eye.sim.engine import Hour, Simulator
from eye.sim.pipeline import Pipeline
from eye.sim.store import TABLES, Batches


def test_baseline_payment_latency_becomes_active(tmp_path: Path) -> None:
    settings = Settings()
    with closing(Simulator(settings, db_path=tmp_path / "latency.duckdb")) as sim:
        with sim.store.cursor() as reader:
            registry = load_registry(reader)
            assert registry.get("payment_latency_p95").status == "pending"
            with pytest.raises(ValueError, match="payment_latency_p95 is pending"):
                MetricEngine(reader, registry).metric_value(
                    "payment_latency_p95", sim.now
                )
        sim.step()
        sim.step()
        with sim.store.cursor() as reader:
            registry = load_registry(reader)
            assert registry.get("payment_latency_p95").status == "active"
            series = MetricEngine(reader, registry).query_metric(
                "payment_latency_p95",
                settings.sim.start_ts,
                sim.now,
                group_by=["psp"],
            )
            assert {s.segment["psp"] for s in series} == {"PSP-1", "PSP-2"}
            for s in series:
                assert len(s.points) == 2
                assert all(150 < p.value < 250 for p in s.points)
                assert all(p.completeness == 1.0 for p in s.points)
            assert len({p.value for s in series for p in s.points}) == 4


@pytest.mark.parametrize("latency", [0.0, 900.0])
def test_payment_latency_override_and_recovery(tmp_path: Path, latency: float) -> None:
    def override(sim: Simulator, hour: Hour) -> None:
        hour.demand.psp_latency["PSP-2"] = latency

    settings = Settings()
    with closing(Simulator(settings, db_path=tmp_path / "latency.duckdb")) as sim:
        sim.pre_demand_hooks.append(override)
        sim.step()
        sim.pre_demand_hooks.clear()
        sim.step()
        with sim.store.cursor() as reader:
            engine = MetricEngine(reader, load_registry(reader))
            for offset in range(2):
                ts = settings.sim.start_ts + timedelta(hours=offset)
                value = engine.metric_value("payment_latency_p95", ts, {"psp": "PSP-2"})
                if offset == 0:
                    assert value == latency
                else:
                    assert 150 < value < 250
                assert (
                    150
                    < engine.metric_value("payment_latency_p95", ts, {"psp": "PSP-1"})
                    < 250
                )


def test_payment_latency_distribution(reader: duckdb.DuckDBPyConnection) -> None:
    rows = reader.execute(
        "SELECT psp, avg(latency_p95_ms), stddev_pop(latency_p95_ms), "
        "min(latency_p95_ms), count(DISTINCT latency_p95_ms) "
        "FROM payments_hourly GROUP BY psp"
    ).fetchall()
    assert len(rows) == 2
    for _, average, sd, minimum, distinct in rows:
        assert average == pytest.approx(200.0, rel=0.02)
        assert 0.035 < sd / average < 0.065
        assert minimum > 0
        assert distinct == 672


def test_payment_latency_rng_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def play() -> dict[str, list[tuple]]:
        with closing(Simulator(Settings(), db_path=":memory:")) as sim:
            sim.selleros_export.path = tmp_path / "selleros.json"
            for _ in range(4):
                sim.step()
            with sim.store.cursor() as reader:
                return {
                    table: reader.execute(
                        f"SELECT * FROM {table} ORDER BY ALL"
                    ).fetchall()
                    for table in TABLES
                    if table != "scenario_log"
                }

    def shifted_latency_rng(seed: int | list[int]) -> np.random.Generator:
        rng = np.random.default_rng(seed)
        if isinstance(seed, list) and len(seed) == 4 and seed[2] == 2:
            rng.random(100)
        return rng

    baseline = play()
    assert play() == baseline
    monkeypatch.setattr(sim_engine, "default_rng", shifted_latency_rng)
    shifted = play()
    assert shifted["payments_hourly"] != baseline["payments_hourly"]
    for table, rows in baseline.items():
        if table == "payments_hourly":
            assert [r[:-1] for r in rows] == [r[:-1] for r in shifted[table]]
        else:
            assert rows == shifted[table], table


def test_payment_latency_without_attempts(tmp_path: Path) -> None:
    def stop_checkout(sim: Simulator, hour: Hour) -> None:
        hour.demand.checkout_ok.update(dict.fromkeys(("web", "android", "ios"), 0.0))

    settings = Settings()
    with closing(Simulator(settings, db_path=tmp_path / "latency.duckdb")) as sim:
        sim.pre_demand_hooks.append(stop_checkout)
        sim.step()
        with sim.store.cursor() as reader:
            assert reader.execute(
                "SELECT sum(attempts), count(latency_p95_ms) FROM payments_hourly"
            ).fetchone() == (0, 12)
            registry = load_registry(reader)
            assert registry.get("payment_latency_p95").status == "active"
            assert (
                MetricEngine(reader, registry).metric_value(
                    "payment_latency_p95", settings.sim.start_ts
                )
                is None
            )


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


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_overlapping_native_releases(platform: str) -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    apps = Apps()
    original = apps.versions[platform]
    apps.release(platform, "v2", ts)
    second_release = ts + timedelta(days=1)
    apps.release(platform, "v3", second_release)
    for days, expected in (
        (0, {original: 0.48, "v2": 0.32, "v3": 0.2}),
        (1, {original: 0.36, "v2": 0.24, "v3": 0.4}),
        (4, {"v3": 1.0}),
    ):
        now = second_release + timedelta(days=days)
        mix = apps.adoption(platform, now)
        assert mix == pytest.approx(expected)
        assert sum(mix.values()) == pytest.approx(1.0)
        batches: Batches = {"traffic_hourly": [(now, "DE", platform, 1_000_000)]}
        apps.errors(now, np.random.default_rng(42), batches)
        errors = sum(row[-1] for row in batches["client_errors_hourly"])
        assert errors == pytest.approx(1_000_000 * 0.002, rel=0.2)


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_rereleasing_current_native_version(platform: str) -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    apps = Apps()
    version = apps.versions[platform]
    apps.release(platform, version, ts)
    for days in (0, 1, 4):
        now = ts + timedelta(days=days)
        mix = apps.adoption(platform, now)
        assert mix == {version: 1.0}
        assert sum(mix.values()) == pytest.approx(1.0)
        batches: Batches = {"traffic_hourly": [(now, "DE", platform, 1_000_000)]}
        apps.errors(now, np.random.default_rng(42), batches)
        errors = sum(row[-1] for row in batches["client_errors_hourly"])
        assert errors == pytest.approx(1_000_000 * 0.002, rel=0.2)
