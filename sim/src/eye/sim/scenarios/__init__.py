"""Simulator-owned scenario plugins and lifecycle."""

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel

from eye.sim.engine import Hour, Simulator

ScenarioId = Literal["S8"]


class ScenarioState(BaseModel):
    id: ScenarioId
    title: str
    active: bool
    injected_ts: datetime | None
    cleared_ts: datetime | None


class ScenarioPlugin(Protocol):
    id: str
    title: str

    def __init__(self, sim: Simulator, params: dict[str, Any]) -> None: ...

    def start(self, ts: datetime) -> None: ...
    def stop(self) -> None: ...
    def before(self, sim: Simulator, hour: Hour) -> None: ...
    def before_store(self, sim: Simulator, hour: Hour) -> None: ...


def registry() -> dict[str, type[ScenarioPlugin]]:
    from eye.sim.scenarios.s8 import S8

    available = {"s8": S8}
    enabled = {
        value.strip().lower()
        for value in os.getenv("SIM_SCENARIOS", "s8").split(",")
        if value.strip()
    }
    unknown = enabled - available.keys()
    if unknown:
        raise ValueError(f"unknown SIM_SCENARIOS: {', '.join(sorted(unknown))}")
    return {key: available[key] for key in sorted(enabled)}


class ScenarioManager:
    def __init__(
        self, sim: Simulator, *, config_path: str | Path = "config/scenarios.yaml"
    ) -> None:
        self.sim = sim
        self.params: dict[str, dict[str, Any]] = yaml.safe_load(
            Path(config_path).read_text()
        )
        self.plugins = {
            key.upper(): plugin(sim, self.params[key.upper()])
            for key, plugin in registry().items()
        }
        self._states: list[ScenarioState] = []
        sim.pre_demand_hooks.append(self._before)
        sim.pre_store_hooks.append(self._before_store)

    def inject(self, id: str, at_ts: datetime | None = None) -> ScenarioState:
        id = id.upper()
        if id not in self.plugins:
            raise ValueError(f"unsupported scenario: {id}")
        if any(s.id == id and s.cleared_ts is None for s in self._states):
            raise ValueError(f"scenario already injected: {id}")
        ts = self.sim.now if at_ts is None else at_ts
        if (
            ts.tzinfo is None
            or ts < self.sim.now
            or ts.minute
            or ts.second
            or ts.microsecond
        ):
            raise ValueError("injection requires an aware current or future whole hour")
        state = ScenarioState(
            id=id,
            title=self.plugins[id].title,
            active=False,
            injected_ts=ts,
            cleared_ts=None,
        )
        self.plugins[id].start(ts)
        self._states.append(state)
        state.active = ts == self.sim.now
        return state.model_copy(deep=True)

    def clear(self, id: str) -> None:
        id = id.upper()
        state = next(
            (s for s in reversed(self._states) if s.id == id and s.cleared_ts is None),
            None,
        )
        if state is None:
            raise ValueError(f"scenario not injected: {id}")
        self.plugins[id].stop()
        state.active = False
        state.cleared_ts = self.sim.now

    def active(self) -> list[ScenarioState]:
        return [s.model_copy(deep=True) for s in self._states if s.active]

    def timeline(self) -> list[ScenarioState]:
        return [s.model_copy(deep=True) for s in self._states]

    def _before(self, sim: Simulator, hour: Hour) -> None:
        for state in self._states:
            if state.cleared_ts is None and state.injected_ts <= hour.ts:
                state.active = True
        for plugin in self.plugins.values():
            plugin.before(sim, hour)

    def _before_store(self, sim: Simulator, hour: Hour) -> None:
        for plugin in self.plugins.values():
            plugin.before_store(sim, hour)


def inject(sim: Simulator, id: str, at_ts: datetime | None = None) -> ScenarioState:
    for hook in sim.pre_demand_hooks:
        manager = getattr(hook, "__self__", None)
        if isinstance(manager, ScenarioManager):
            return manager.inject(id, at_ts)
    return ScenarioManager(sim).inject(id, at_ts)
