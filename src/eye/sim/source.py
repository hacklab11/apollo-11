"""Simulator lifecycle and replay adapter."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

import duckdb

from eye.config import Settings
from eye.contracts import ScenarioId
from eye.orchestrator import HOUR, Scenarios
from eye.sim.engine import Simulator
from eye.sim.store import WORLD_DB, Store


class SimSource:
    def __init__(
        self, sim: Simulator, scenarios: Scenarios | None, titles: dict[ScenarioId, str]
    ) -> None:
        self.sim = sim
        self.scenarios = scenarios
        self.scenario_titles = titles

    @property
    def now(self) -> datetime:
        return self.sim.now

    def step(self) -> None:
        self.sim.step()

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self.sim.store.cursor()

    def record_scenario(self, id: ScenarioId, ts: datetime, active: bool) -> None:
        with self.cursor() as con:
            con.execute(
                "INSERT INTO scenario_commands SELECT coalesce(max(sequence), 0) + 1, ?, ?, ? FROM scenario_commands",
                [ts, id, active],
            )

    def close(self) -> None:
        self.sim.close()


@contextmanager
def open_sim_source(settings: Settings) -> Iterator[SimSource]:
    try:
        scenarios = import_module("eye.sim.scenarios")
    except ModuleNotFoundError as exc:
        if exc.name != "eye.sim.scenarios":
            raise
        scenarios = None
    with _world(settings, scenarios, WORLD_DB) as (sim, manager):
        yield SimSource(sim, manager, scenarios._TITLES if scenarios else {})


@contextmanager
def _world(
    settings: Settings, scenario_module: ModuleType | None, path: Path = WORLD_DB
) -> Iterator[tuple[Simulator, Scenarios | None]]:
    with closing(Store(path)) as stored, stored.cursor() as reader:
        exists = reader.execute("SELECT 1 FROM skus LIMIT 1").fetchone() is not None
        latest = reader.execute("SELECT max(ts) FROM traffic_hourly").fetchone()[0]
        reader.execute(
            "CREATE TABLE IF NOT EXISTS scenario_commands (sequence BIGINT, ts TIMESTAMPTZ, id VARCHAR, active BOOLEAN)"
        )
        commands = reader.execute(
            "SELECT ts, id, active FROM scenario_commands ORDER BY sequence"
        ).fetchall()
        if not exists:
            with closing(Simulator(settings, db_path=path)) as sim:
                sim.backfill()
                yield (
                    sim,
                    scenario_module.ScenarioManager(sim) if scenario_module else None,
                )
            return

        # The simulator opens only fresh databases. Replay into a scratch store to
        # reconstruct its mutable inventory and agent export without rewriting facts.
        with closing(Simulator(settings, db_path=":memory:")) as sim:
            export_path = sim.selleros_export.path
            with TemporaryDirectory(dir=path.parent) as scratch:
                sim.selleros_export.path = Path(scratch) / export_path.name
                start = settings.sim.start_ts
                sim.now = start - HOUR * (24 * settings.sim.history_days)
                while latest is not None and sim.now < start and sim.now <= latest:
                    sim.step()
                if sim.now < start:
                    scratch_store = sim.store
                    sim.store = stored
                    try:
                        while sim.now < start:
                            sim.step()
                    finally:
                        sim.store = scratch_store
                scenarios = (
                    scenario_module.ScenarioManager(sim) if scenario_module else None
                )
                remaining = iter(commands)
                next_command = next(remaining, None)
                while True:
                    while next_command is not None and next_command[0] == sim.now:
                        if scenarios is None:
                            raise RuntimeError(
                                "cannot restore existing scenarios without T04"
                            )
                        _, id, active = next_command
                        if active:
                            scenarios.inject(id)
                        else:
                            scenarios.clear(id)
                        next_command = next(remaining, None)
                    if latest is None or sim.now > latest:
                        break
                    sim.step()
                if next_command is not None:
                    raise RuntimeError(
                        "scenario commands do not align with simulated hours"
                    )
            sim.selleros_export.path = export_path
            sim.selleros_export.write(sim.now)
            scratch_store = sim.store
            sim.store = stored
            try:
                yield sim, scenarios
            finally:
                sim.store = scratch_store
