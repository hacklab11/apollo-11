import asyncio
import os
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from eye.config import Settings
from eye.schema import SCHEMA
from eye.sim.engine import Simulator
from eye.sim.lake import LakeWriter
from eye.sim.scenarios import ScenarioManager, registry
from eye.sim.service import ServiceSettings, create_app


def tiny_settings() -> Settings:
    settings = Settings()
    settings.sim = settings.sim.model_copy(update={"history_days": 0})
    return settings


def test_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIM_SCENARIOS", raising=False)
    assert list(registry()) == ["s8"]
    monkeypatch.setenv("SIM_SCENARIOS", " S8, s8 ")
    assert list(registry()) == ["s8"]
    monkeypatch.setenv("SIM_SCENARIOS", "")
    assert registry() == {}
    monkeypatch.setenv("SIM_SCENARIOS", "s1")
    with pytest.raises(ValueError, match="unknown SIM_SCENARIOS"):
        registry()


def test_coupon_then_typo_and_clear(tmp_path: Path) -> None:
    with closing(Simulator(tiny_settings(), db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        manager = ScenarioManager(sim)
        manager.params["S8"]["typo_delay_hours"] = 2
        offers = manager.plugins["S8"].offers()
        prices = [offer.price for offer in offers]
        manager.inject("s8")
        with sim.store.cursor() as con:
            assert con.execute(
                "SELECT planned, description FROM change_log WHERE id = 'coupon-day-electronics'"
            ).fetchone() == (True, "coupon-day-electronics")
        assert all(
            sim.finance.platform_discount[s.sku] == s.ref_price * 0.1
            for s in sim.world.skus.values()
            if s.category == "electronics"
        )
        assert [o.price for o in offers] == prices
        sim.step()
        sim.step()
        assert not manager.plugins["S8"].original
        before = [o.price for o in offers]
        sim.step()
        assert [o.price for o in offers] == [round(p * 0.1, 2) for p in before]
        manager.clear("s8")
        assert [o.price for o in offers] == before
        assert not sim.finance.platform_discount
        with sim.store.cursor() as con:
            assert con.execute(
                "SELECT action FROM scenario_log ORDER BY ts, action"
            ).fetchall() == [("inject",), ("typo",), ("clear",)]


def test_coupon_expires(tmp_path: Path) -> None:
    with closing(Simulator(tiny_settings(), db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        manager = ScenarioManager(sim)
        manager.params["S8"]["typo_delay_hours"] = 100
        manager.inject("S8")
        sim.now += timedelta(hours=24)
        sim.step()
        assert not sim.finance.platform_discount


def test_lake_public_tables_and_atomic_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Simulator(tiny_settings(), db_path=":memory:")) as sim:
        sim.selleros_export.path = tmp_path / "sellers.json"
        hour = sim.now
        sim.step()
        writer = LakeWriter(tmp_path / "lake")
        writer.land(sim.store, hour)
        assert {p.name for p in writer.root.iterdir()} == set(SCHEMA)
        for table in SCHEMA:
            path = writer.root / table / f"hour={hour:%Y-%m-%dT%H}" / "data.parquet"
            with duckdb.connect() as con:
                actual = con.execute(
                    "SELECT count(*) FROM read_parquet(?)", [str(path)]
                ).fetchone()[0]
            with sim.store.cursor() as con:
                expected = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert actual == expected
        manifest = (
            writer.root
            / "partition_status"
            / f"hour={hour:%Y-%m-%dT%H}"
            / "data.parquet"
        )
        original_manifest = manifest.read_bytes()
        replace = os.replace
        replacements: list[Path] = []

        def observe(source: Path, destination: Path) -> None:
            assert manifest.exists()
            assert manifest.read_bytes() == original_manifest
            replacements.append(destination)
            replace(source, destination)

        monkeypatch.setattr("eye.sim.lake.os.replace", observe)
        writer.land(sim.store, hour)
        assert len(replacements) == len(SCHEMA)
        assert replacements[-1] == manifest

        def fail(*args: object) -> None:
            assert manifest.exists()
            raise OSError("disk full")

        monkeypatch.setattr("eye.sim.lake.os.replace", fail)
        with pytest.raises(OSError, match="disk full"):
            writer.land(sim.store, hour)
        assert manifest.read_bytes() == original_manifest
        assert not list(writer.root.rglob("*.tmp"))


def test_service_endpoints_restart_and_background_step(tmp_path: Path) -> None:
    settings = tiny_settings()
    service = ServiceSettings(
        world_db=tmp_path / "world.duckdb",
        lake_dir=tmp_path / "lake",
        seconds_per_sim_hour=0.01,
    )
    app = create_app(settings, service)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/scenarios").json() == [
            {"id": "s8", "title": "Price typo: the €9 headphones"}
        ]
        assert client.post("/scenarios/missing").status_code == 404
        assert client.delete("/scenarios/missing").status_code == 404
        assert client.delete("/scenarios/s8").status_code == 409
        assert client.post("/scenarios/s8").status_code == 200
        assert client.post("/scenarios/s8").status_code == 409

        async def wait_for_landing() -> None:
            async with asyncio.timeout(10):
                while app.state.writer.last_hour is None:
                    await asyncio.sleep(0.01)

        client.portal.call(wait_for_landing)
        assert client.get("/status").json()["last_landed_hour"] is not None
    clock = app.state.source.now
    restart = create_app(
        settings, service.model_copy(update={"seconds_per_sim_hour": 1000})
    )
    with TestClient(restart) as client:
        assert restart.state.source.now == clock
        assert client.get("/status").json()["active_scenarios"][0]["id"] == "S8"
        assert client.delete("/scenarios/s8").status_code == 200
        assert client.get("/status").json()["active_scenarios"] == []


def test_disabled_scenario_and_history_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIM_SCENARIOS", "")
    settings = tiny_settings()
    settings.sim = settings.sim.model_copy(update={"history_days": 1})
    service = ServiceSettings(
        world_db=tmp_path / "world.duckdb",
        lake_dir=tmp_path / "lake",
        seconds_per_sim_hour=1000,
    )
    with TestClient(create_app(settings, service)) as client:
        assert client.get("/scenarios").json() == []
        assert client.post("/scenarios/s8").status_code == 404
        assert client.delete("/scenarios/s8").status_code == 404
        assert (
            len(list((service.lake_dir / "partition_status").rglob("data.parquet")))
            == 24
        )


def test_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_PORT", "8123")
    monkeypatch.setenv("SIM_LAKE_DIR", "other/lake")
    monkeypatch.setenv("SIM_SECONDS_PER_SIM_HOUR", "0.02")
    settings = ServiceSettings()
    assert settings.port == 8123
    assert settings.lake_dir == Path("other/lake")
    assert settings.seconds_per_sim_hour == 0.02
