import json
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from eye.config import Settings
from eye.sim import cli
from eye.sim.demand import _HOUR_PROFILE, _WEEKDAY_PROFILE
from eye.sim.engine import Hour, Simulator
from eye.sim.market import buy_box
from eye.sim.store import TABLES


def test_history_speed_and_range(
    reader: duckdb.DuckDBPyConnection, history: tuple[Path, float], settings: Settings
) -> None:
    assert history[1] < 60
    ts = settings.sim.start_ts
    assert reader.execute(
        "SELECT min(ts), max(ts), count(DISTINCT ts), count(*) FROM traffic_hourly"
    ).fetchone() == (ts - timedelta(days=28), ts - timedelta(hours=1), 672, 672 * 6)


def test_baseline_orders_and_traffic_seasonality(
    reader: duckdb.DuckDBPyConnection,
) -> None:
    (peak_to_trough,) = reader.execute("""
        SELECT sum(sessions) FILTER (hour(ts) = 20) / sum(sessions) FILTER (hour(ts) = 4)
        FROM traffic_hourly
    """).fetchone()
    assert 8 <= peak_to_trough <= 16
    hours = reader.execute("""
        SELECT ts, sum(units) FROM orders_hourly GROUP BY ts ORDER BY ts LIMIT 168
    """).fetchall()
    assert len(hours) == 168
    units = np.array([count for _, count in hours])
    season = np.array(
        [_HOUR_PROFILE[ts.hour] * _WEEKDAY_PROFILE[ts.weekday()] for ts, _ in hours]
    )
    expected = season * np.median(units / season)
    # Compare count noise on the same scale at both peak and off-peak hours.
    residuals = (units - expected) / np.sqrt(expected)
    centered = residuals - np.median(residuals)
    robust_z = centered / (1.4826 * np.median(np.abs(centered)))
    assert np.mean(np.abs(robust_z) <= 3) >= 0.99


def test_checkout_and_orders_reconcile(reader: duckdb.DuckDBPyConnection) -> None:
    assert reader.execute("""
        WITH orders AS (
            SELECT ts, country, platform, sum(units) AS units
            FROM orders_hourly GROUP BY ALL
        ), payments AS (
            SELECT ts, country, platform, sum(attempts) AS attempts, sum(successes) AS successes
            FROM payments_hourly GROUP BY ALL
        )
        SELECT count(*)
        FROM checkout_hourly c
        FULL JOIN payments p USING (ts, country, platform)
        FULL JOIN orders o USING (ts, country, platform)
        WHERE c.carts IS NULL OR p.attempts IS NULL
           OR c.carts < c.checkout_starts OR c.checkout_starts < c.payment_attempts
           OR c.payment_attempts != p.attempts OR p.attempts < p.successes
           OR coalesce(o.units, 0) != p.successes
    """).fetchone() == (0,)
    assert reader.execute("""
        SELECT count(*) FROM orders_hourly
        WHERE sku IN ('E11', 'F09', 'T07') OR seller_id = 'S-99' OR units <= 0 OR gmv <= 0
    """).fetchone() == (0,)
    assert reader.execute("""
        SELECT count(*) FROM traffic_hourly
        WHERE sessions != search_sessions + feed_sessions + other_sessions
    """).fetchone() == (0,)
    assert reader.execute("""
        SELECT count(*) FROM (
            SELECT ts, country, platform, sum(impressions) AS impressions FROM feed_hourly GROUP BY ALL
        ) f JOIN traffic_hourly t USING (ts, country, platform)
        WHERE impressions != 12 * feed_sessions
    """).fetchone() == (0,)
    split = reader.execute("""
        SELECT psp, sum(attempts) / sum(sum(attempts)) OVER (),
            sum(successes) / sum(attempts)
        FROM payments_hourly GROUP BY psp ORDER BY psp
    """).fetchall()
    assert [r[0] for r in split] == ["PSP-1", "PSP-2"]
    assert [r[1] for r in split] == pytest.approx([0.60, 0.40], abs=0.01)
    assert [r[2] for r in split] == pytest.approx([0.97, 0.96], abs=0.01)


def test_replay_checksum_with_timed_hook(settings: Settings) -> None:
    def play(seed: int, injected_hour: int) -> str:
        config = settings.model_copy(
            update={"sim": settings.sim.model_copy(update={"seed": seed})}
        )
        with closing(Simulator(config, db_path=":memory:")) as simulator:
            injected_ts = simulator.now + timedelta(hours=injected_hour)

            def change_checkout(sim: Simulator, hour: Hour) -> None:
                assert sim.now == hour.ts
                if hour.ts == injected_ts:
                    hour.demand.checkout_ok["android"] = 0.0

            def verify_orders(sim: Simulator, hour: Hour) -> None:
                if hour.ts == injected_ts:
                    assert hour.batches["orders_hourly"]
                    assert all(
                        row[2] != "android" for row in hour.batches["orders_hourly"]
                    )

            simulator.pre_demand_hooks.append(change_checkout)
            simulator.post_demand_hooks.append(verify_orders)
            for _ in range(4):
                simulator.step()
            assert simulator.now == settings.sim.start_ts + timedelta(hours=4)
            with simulator.store.cursor() as con:
                checksum = con.execute("""
                    SELECT sha256(string_agg(to_json(o), '' ORDER BY ts, country, platform, sku, seller_id, channel))
                    FROM orders_hourly o
                """).fetchone()[0]
            return checksum

    assert play(42, 1) == play(42, 1)
    assert play(42, 1) != play(42, 2)
    assert play(42, 1) != play(43, 1)


def test_orders_use_buy_box_offer(settings: Settings) -> None:
    with closing(Simulator(settings, db_path=":memory:")) as sim:
        offers = sim.world.offers["E01"]
        offers[0].stock = 0
        sim.market.run_repricers()
        winner = buy_box(sim.world, "E01")
        sim.step()
        with sim.store.cursor() as con:
            assert con.execute("""
                SELECT DISTINCT seller_id, seller_tier
                FROM orders_hourly WHERE sku = 'E01'
            """).fetchall() == [
                (winner.seller_id, sim.world.sellers[winner.seller_id].tier)
            ]
            assert con.execute(
                """
                SELECT count(*) FROM orders_hourly
                WHERE sku = 'E01' AND gmv != round(units * ?, 2)
            """,
                [winner.price],
            ).fetchone() == (0,)


def test_cli_stats_and_exports(
    history: tuple[Path, float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "WORLD_DB", history[0])
    monkeypatch.setattr(sys, "argv", ["eye-sim", "stats"])
    cli.main()
    stats = json.loads(capsys.readouterr().out)
    assert stats["rows"]["traffic_hourly"] == 4032
    assert stats["first_hour_utc"] == "2026-10-30T00:00:00+00:00"
    for fmt in ("csv", "parquet"):
        out = tmp_path / "market's export"
        monkeypatch.setattr(
            sys, "argv", ["eye-sim", "export", "--format", fmt, "--out", str(out)]
        )
        cli.main()
        assert {p.stem for p in out.glob(f"*.{fmt}")} == set(TABLES)
    with duckdb.connect() as con:
        assert con.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(out / "skus.parquet")]
        ).fetchone() == (40,)
    assert (
        (out / "skus.csv")
        .read_text()
        .splitlines()[0]
        .startswith("sku,category,ref_price,")
    )


def test_cli_play_schedule_and_missing_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._play_schedule("I1@0,S7@3,S6@3") == {0: ["I1"], 3: ["S7", "S6"]}
    path = tmp_path / "world.duckdb"
    monkeypatch.setattr(cli, "WORLD_DB", path)
    monkeypatch.setitem(sys.modules, "eye.sim.scenarios", None)
    monkeypatch.setattr(sys, "argv", ["eye-sim", "generate", "--play", "I1@0"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "scenarios not implemented" in capsys.readouterr().err
    assert not path.exists()


def test_cli_play_injects_at_offsets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    played = []

    def inject(sim: Simulator, scenario: str) -> None:
        played.append((sim.now, scenario))

    monkeypatch.setitem(
        sys.modules, "eye.sim.scenarios", SimpleNamespace(inject=inject)
    )
    path = tmp_path / "world.duckdb"
    monkeypatch.setattr(cli, "WORLD_DB", path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(Simulator, "backfill", lambda self: None)
    monkeypatch.setattr(sys, "argv", ["eye-sim", "generate", "--play", "S7@3,I1@0"])
    cli.main()
    assert played == [
        (settings.sim.start_ts, "I1"),
        (settings.sim.start_ts + timedelta(hours=3), "S7"),
    ]
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE consumer_state AS SELECT 42 AS value")
    cli.main()
    with duckdb.connect(str(path), read_only=True) as con:
        assert con.execute(
            "SELECT count(DISTINCT ts) FROM orders_hourly"
        ).fetchone() == (4,)
        assert con.execute("SELECT count(*) FROM traffic_hourly").fetchone() == (24,)
        assert con.execute("SELECT value FROM consumer_state").fetchone() == (42,)
