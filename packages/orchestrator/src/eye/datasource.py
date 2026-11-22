"""Read sources for the monitoring core, independent of synthetic generation."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol

import duckdb

from eye.config import Settings
from eye.contracts import ScenarioId
from eye.orchestrator import HOUR, Scenarios
from eye.schema import validate_schema
from eye.warehouse import operations as ops
from eye.warehouse.client import DataClient, DataUnavailable


class DataSource(Protocol):
    now: datetime
    history_start: datetime
    live_start: datetime
    scenarios: Scenarios | None
    scenario_titles: dict[ScenarioId, str]

    def step(self) -> None: ...
    def cursor(self) -> duckdb.DuckDBPyConnection: ...
    def record_scenario(self, id: ScenarioId, ts: datetime, active: bool) -> None: ...
    def close(self) -> None: ...


class DuckDBSource:
    scenarios: Scenarios | None = None

    def __init__(self, path: Path) -> None:
        self.path = path
        self.scenario_titles: dict[ScenarioId, str] = {}
        self._con = duckdb.connect(str(path), read_only=True)
        try:
            validate_schema(self._con)
            earliest, latest = self._con.execute(
                "SELECT min(ts), max(ts) FROM (SELECT ts FROM partition_status GROUP BY ts "
                "HAVING bool_and(coalesce(loaded_rows >= expected_rows, false)))"
            ).fetchone()
            if latest is None:
                raise ValueError(
                    "Warehouse has no fully loaded hour in partition_status"
                )
            self.now = latest + HOUR
            self.history_start = earliest
            self.live_start = self.now
        except BaseException:
            self.close()
            raise

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def step(self) -> None:
        # Keep issued reader cursors alive; refresh the clock on a fresh connection.
        with duckdb.connect(str(self.path), read_only=True) as con:
            loaded = con.execute(
                "SELECT count(*) > 0 AND bool_and(coalesce(loaded_rows >= expected_rows, false)) "
                "FROM partition_status WHERE ts = ?",
                [self.now],
            ).fetchone()[0]
        if loaded:
            self.now += HOUR

    def record_scenario(self, id: ScenarioId, ts: datetime, active: bool) -> None:
        raise RuntimeError("Scenarios are not supported by the DuckDB data source")

    def close(self) -> None:
        self._con.close()


class ServiceClock:
    """Consume only completed hours; this clock never advances the producer."""

    def __init__(self, client: DataClient) -> None:
        self.client = client
        window = client.partitions()
        if window.earliest is None or window.latest is None:
            raise DataUnavailable("Warehouse has no fully loaded hour")
        self.now = self.live_start = window.latest + HOUR
        self.history_start = window.earliest

    def step(self) -> None:
        if self.client.read(ops.COMPLETE_HOUR, ops.CompleteHourInput(ts=self.now)):
            self.now += HOUR


@contextmanager
def open_source(settings: Settings) -> Iterator[DataSource]:
    if settings.data.source == "sim":
        module = import_module(".sim.source", package=__package__)
        with module.open_sim_source(settings) as source:
            yield source
    else:
        with closing(DuckDBSource(settings.data.path)) as source:
            yield source
