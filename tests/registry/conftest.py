import shutil
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from eye.config import Settings
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry, load_registry
from eye.sim.engine import Simulator


@pytest.fixture(scope="session")
def settings() -> Settings:
    base = Settings()
    # model_copy skips validation, so a short history keeps the suite fast.
    return base.model_copy(
        update={"sim": base.sim.model_copy(update={"history_days": 3})}
    )


@pytest.fixture(scope="session")
def world(settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("registry") / "world.duckdb"
    with closing(Simulator(settings, db_path=path)) as simulator:
        simulator.backfill()
    return path


@pytest.fixture(scope="session")
def day(settings: Settings) -> tuple[datetime, datetime]:
    end = settings.sim.start_ts
    return end - timedelta(hours=24), end


def _connect(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute("SET TimeZone = 'UTC'")
    return con


@pytest.fixture
def con(world: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    with _connect(world, read_only=True) as connection:
        yield connection


@pytest.fixture
def writable(world: Path, tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    path = tmp_path / "world.duckdb"
    shutil.copy(world, path)
    with _connect(path, read_only=False) as connection:
        yield connection


@pytest.fixture
def registry(con: duckdb.DuckDBPyConnection) -> Registry:
    return load_registry(con)


@pytest.fixture
def engine(con: duckdb.DuckDBPyConnection, registry: Registry) -> MetricEngine:
    return MetricEngine(con, registry)
