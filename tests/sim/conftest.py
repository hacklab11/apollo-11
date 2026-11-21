from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from time import perf_counter

import duckdb
import pytest

from eye.config import Settings
from eye.sim.engine import Simulator


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def history(
    settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, float]:
    path = tmp_path_factory.mktemp("sim") / "world.duckdb"
    with closing(Simulator(settings, db_path=path)) as simulator:
        start = perf_counter()
        simulator.backfill()
        elapsed = perf_counter() - start
        assert simulator.now == settings.sim.start_ts
        simulator.backfill()  # Repeated backfill must not duplicate facts.
    return path, elapsed


@pytest.fixture
def reader(history: tuple[Path, float]) -> Iterator[duckdb.DuckDBPyConnection]:
    with duckdb.connect(str(history[0]), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        yield con
