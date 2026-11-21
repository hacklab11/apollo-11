from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import duckdb
import pytest

from eye.config import Settings
from eye.graph.bootstrap import bootstrap
from eye.graph.store import GraphStore
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.sim.engine import Simulator


@pytest.fixture(scope="session")
def graph_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = Settings()
    # model_copy skips validation, so a short history keeps the suite fast.
    settings = base.model_copy(
        update={"sim": base.sim.model_copy(update={"history_days": 3})}
    )
    path = tmp_path_factory.mktemp("graph") / "world.duckdb"
    with closing(Simulator(settings, db_path=path)) as simulator:
        simulator.backfill()
    return path


@pytest.fixture(scope="session")
def graph_engine(graph_world: Path) -> Iterator[MetricEngine]:
    with duckdb.connect(str(graph_world), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        yield MetricEngine(con, load_registry(con))


@pytest.fixture(scope="session")
def graph_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("graph_json") / "graph.json"


@pytest.fixture(scope="session")
def graph(graph_engine: MetricEngine, graph_path: Path) -> GraphStore:
    return bootstrap(graph_engine, graph_path)
