"""Core clock, agent dispatch, and incident lifecycle."""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel

from eye.a2a_client import A2AClient
from eye.agents.reporter import Reporter
from eye.api.events import EventBus
from eye.config import Settings
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Anomaly,
    Event,
    Incident,
    InvestigationResult,
    ScenarioId,
    ScenarioState,
    SentinelDecision,
    TrailStep,
)
from eye.incidents.store import IncidentStore
from eye.llm.client import LLMClient, LLMUnavailable

HOUR = timedelta(hours=1)
log = logging.getLogger(__name__)
type StepSink = Callable[[TrailStep], Awaitable[None]]
type Investigate = Callable[[Anomaly, StepSink], Awaitable[InvestigationResult]]
type AddMetric = Callable[
    [AddMetricRequest, Sequence[datetime], StepSink], Awaitable[AddMetricResult]
]
type Refine = Callable[[Sequence[datetime], StepSink], Awaitable[None]]
type EventType = Literal[
    "tick",
    "anomaly",
    "sentinel_decision",
    "investigation_started",
    "trail_step",
    "investigation_finished",
    "incident_upserted",
    "graph_changed",
    "scenario_changed",
    "agent_status",
]


class ClockSource(Protocol):
    now: datetime
    history_start: datetime
    live_start: datetime

    def step(self) -> None: ...


class SentinelSource(Protocol):
    async def run(
        self, ts: datetime, *, open_incidents: Sequence[Incident], on_step: StepSink
    ) -> list[SentinelDecision]: ...


class Scenarios(Protocol):
    def inject(self, id: ScenarioId) -> ScenarioState: ...

    def clear(self, id: ScenarioId) -> None: ...

    def timeline(self) -> list[ScenarioState]: ...


class AgentHealth(BaseModel):
    ok: bool
    url: str


class Status(BaseModel):
    sim_ts: datetime
    tick: int
    running: bool
    paused: bool
    llm_mode: Literal["live", "record", "replay"]
    llm_calls: int
    tokens_in: int
    tokens_out: int
    agents: dict[str, AgentHealth]
    active_scenarios: list[ScenarioId]
    investigations_running: int


def _log_error(event: str, exc: Exception, incident_id: str | None = None) -> None:
    log.warning(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "component": "orchestrator",
                "event": event,
                "incident_id": incident_id,
                "error": type(exc).__name__,
                **(
                    {"detail": str(exc)[:500]}
                    if isinstance(exc, LLMUnavailable)
                    else {}
                ),
            }
        )
    )


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        sim: ClockSource,
        sentinel: SentinelSource,
        incidents: IncidentStore,
        llm: LLMClient,
        z_lookup: Callable[
            [str, dict[str, str], datetime], float | tuple[float, float]
        ],
        *,
        scenarios: Scenarios | None = None,
        scenario_titles: dict[ScenarioId, str] | None = None,
        a2a: A2AClient | None = None,
        investigate: Investigate | None = None,
        add_metric: AddMetric | None = None,
        refine: Refine | None = None,
        invalidate: Callable[[], None] = lambda: None,
        record_scenario: Callable[[ScenarioId, datetime, bool], None] = lambda *_: None,
    ) -> None:
        self.settings = settings
        self.sim = sim
        self.sentinel = sentinel
        self.incidents = incidents
        self.llm = llm
        self.z_lookup = z_lookup
        self.scenarios = scenarios
        self.scenario_titles = scenario_titles or {}
        self.a2a = a2a
        self.investigate = investigate
        self.add_metric = add_metric
        self.refine = refine
        self.invalidate = invalidate
        self.record_scenario = record_scenario
        self.events = EventBus()
        self.decisions: deque[SentinelDecision] = deque(maxlen=2_000)
        self.tick = max(0, (sim.now - sim.live_start) // HOUR)
        self.sim_ts = sim.now - HOUR if self.tick else sim.now
        self.paused = not settings.sim.autostart
        self._agents = {
            name: AgentHealth(ok=False, url=getattr(settings.a2a, f"{name}_url"))
            for name in ("growth", "marketplace", "discovery", "selleros")
        }
        self._clock: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._metric_lock = asyncio.Lock()
        self._upsert_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending: deque[Incident] = deque()
        self._closing = False
        self._stop = asyncio.Event()

    def emit(
        self, kind: EventType, payload: BaseModel | dict, ts: datetime | None = None
    ) -> None:
        body = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload
        )
        self.events.publish(Event(type=kind, sim_ts=ts or self.sim_ts, payload=body))

    async def record_step(self, step: TrailStep, ts: datetime | None = None) -> None:
        self.incidents.add_step(step)
        self.emit("trail_step", step, ts)

    async def refresh_agents(self) -> None:
        if self.a2a is None:
            return
        for name, card in (await self.a2a.fetch_cards()).items():
            health = AgentHealth(ok=card.healthy, url=card.url)
            if health != self._agents[name]:
                self._agents[name] = health
                self.emit("agent_status", {"name": name, **health.model_dump()})

    def agent_unavailable(self, name: str, _reason: str) -> None:
        health = self._agents[name]
        if health.ok:
            self._agents[name] = health.model_copy(update={"ok": False})
            self.emit("agent_status", {"name": name, **self._agents[name].model_dump()})

    async def start(self) -> None:
        if self.refine is not None:
            await self.refine(self.clean_hours(), self.record_step)
        if self.investigate is not None:
            self._pending.extend(
                item
                for item in self._open_incidents()
                if item.status == "investigating"
            )
        await self.refresh_agents()
        if self.settings.sim.autostart:
            self.resume()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        if self._clock is None:
            self._clock = asyncio.create_task(self._run_clock())

    async def close(self) -> None:
        self._closing = True
        self._stop.set()
        self.pause()
        if self._clock is not None:
            await self._clock
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.a2a is not None:
            await self.a2a.aclose()

    async def _run_clock(self) -> None:
        interval = self.settings.sim.seconds_per_sim_hour
        while not self._closing:
            try:
                await asyncio.wait_for(self._stop.wait(), interval)
                return
            except TimeoutError:
                pass
            if self._closing or self.paused:
                continue
            try:
                if not all(agent.ok for agent in self._agents.values()):
                    await self.refresh_agents()
                await self._step(manual=False)
            except Exception as exc:  # noqa: BLE001 -- preserve the clock after a failed tick
                _log_error("tick_failed", exc)

    def status(self) -> Status:
        usage = self.llm.status()
        running = bool(self._clock and not self._clock.done())
        return Status(
            sim_ts=self.sim_ts,
            tick=self.tick,
            running=running
            and not self.paused
            and not (
                self.settings.orchestrator.pause_sim_during_investigation
                and self._tasks
            ),
            paused=self.paused,
            llm_mode=self.settings.llm.mode,
            llm_calls=int(usage["calls_per_hour"]),
            tokens_in=int(usage["tokens_in"]),
            tokens_out=int(usage["tokens_out"]),
            agents=self._agents,
            active_scenarios=[
                state.id for state in self.scenario_states() if state.active
            ],
            investigations_running=len(self._tasks),
        )

    def scenario_states(self) -> list[ScenarioState]:
        latest = (
            {state.id: state for state in self.scenarios.timeline()}
            if self.scenarios
            else {}
        )
        return [
            latest.get(scenario)
            or ScenarioState(
                id=scenario,
                title=title,
                active=False,
                injected_ts=None,
                cleared_ts=None,
            )
            for scenario, title in self.scenario_titles.items()
        ]

    async def set_scenario(
        self, scenario: ScenarioId, *, active: bool
    ) -> ScenarioState:
        async with self._tick_lock:
            if self.scenarios is None or scenario not in self.scenario_titles:
                raise LookupError(scenario)
            if active:
                state = self.scenarios.inject(scenario)
            else:
                self.scenarios.clear(scenario)
                state = next(
                    item for item in self.scenario_states() if item.id == scenario
                )
            self.record_scenario(scenario, self.sim.now, active)
            self.emit("scenario_changed", state, self.sim.now)
            return state

    def clean_hours(self) -> list[datetime]:
        start = self.sim.history_start
        excluded = self.incidents.excluded_hours(self.sim.now)
        hours = (start + i * HOUR for i in range((self.sim.now - start) // HOUR))
        return [hour for hour in hours if hour not in excluded]

    async def register_metric(self, request: AddMetricRequest) -> AddMetricResult:
        if self.add_metric is None:
            raise NotImplementedError
        async with self._metric_lock:
            return await self.add_metric(request, self.clean_hours(), self.record_step)

    async def step_once(self) -> Status:
        """Advance one hour without starting the timer; drain that tick's work."""
        return await self._step(manual=True)

    async def _step(self, *, manual: bool) -> Status:
        async with self._tick_lock:
            if not manual and (
                self.paused
                or (
                    self.settings.orchestrator.pause_sim_during_investigation
                    and self._tasks
                )
            ):
                return self.status()
            before = self.scenario_states()
            ts = self.sim.now
            await asyncio.to_thread(self.sim.step)
            if self.sim.now == ts:
                return self.status()
            self._dispatch()
            if manual:
                while self._tasks:
                    await asyncio.gather(*self._tasks, return_exceptions=True)
            self.invalidate()
            self.sim_ts = ts
            self.tick += 1
            self.emit("tick", {"tick": self.tick})
            for previous, current in zip(before, self.scenario_states(), strict=True):
                if previous != current:
                    self.emit("scenario_changed", current)
            try:
                decisions = await self.sentinel.run(
                    ts, open_incidents=self._open_incidents(), on_step=self.record_step
                )
            except Exception as exc:  # noqa: BLE001 -- lifecycle must still run
                _log_error("sentinel_failed", exc)
                decisions = []

            for decision in decisions:
                self.decisions.append(decision)
                self.emit("anomaly", decision.anomaly)
                self.emit("sentinel_decision", decision)
                self._match(decision)
            self._dispatch()
            if manual:
                while self._tasks:
                    await asyncio.gather(*self._tasks, return_exceptions=True)
            for incident in self.incidents.lifecycle(ts, self.z_lookup):
                self.emit("incident_upserted", incident)
            return self.status()

    def _open_incidents(self) -> list[Incident]:
        return [
            item
            for item in self.incidents.all()
            if item.merged_into is None
            and item.status in {"investigating", "open", "persisting"}
        ]

    def _match(self, decision: SentinelDecision) -> None:
        if decision.action == "attach" and decision.attach_to_incident:
            item = self.incidents.get(decision.attach_to_incident)
            if (
                item
                and item.merged_into is None
                and item.status in {"investigating", "open", "persisting"}
            ):
                item.occurrences += 1
                item.last_seen_ts = item.updated_ts = decision.anomaly.ts
                self.incidents._save(item)
                self.emit("incident_upserted", item)
        elif (
            decision.action == "investigate"
            and self.investigate is not None
            and not self.llm.status()["budget_paused"]
        ):
            item = self.incidents.match(decision)
            if item is not None:
                self.emit("incident_upserted", item)
                if (
                    item.status == "investigating"
                    and item.entry.id == decision.anomaly.id
                    and item.occurrences == 1
                ):
                    self._pending.append(item)

    def _dispatch(self) -> None:
        limit = self.settings.orchestrator.max_concurrent_investigations
        while (
            self._pending
            and len(self._tasks) < limit
            and not self._closing
            and not self.llm.status()["budget_paused"]
        ):
            item = self._pending.popleft()
            task = asyncio.create_task(self._run_investigation(item))
            self._tasks.add(task)

    async def _run_investigation(self, item: Incident) -> None:
        try:
            await self._investigate(item)
        except Exception as exc:  # noqa: BLE001 -- remaining investigations must still run
            _log_error("incident_upsert_failed", exc, item.id)
        finally:
            task = asyncio.current_task()
            assert task is not None
            self._tasks.remove(task)
            self._dispatch()

    async def _investigate(self, item: Incident) -> None:
        assert self.investigate is not None
        entry = item.entry
        current = self.incidents.get(item.id)
        assert current is not None
        if current.investigation_ids:
            investigation_id = current.investigation_ids[-1]
        else:
            investigation_id = f"inv_{uuid4().hex}"
            current.investigation_ids.append(investigation_id)
            self.incidents._save(current)
        step_offset = max(
            (step.n for step in self.incidents.trail(investigation_id)), default=0
        )
        self.emit(
            "investigation_started",
            {
                "incident_id": item.id,
                "investigation_id": investigation_id,
                "anomaly": entry.model_dump(mode="json"),
            },
            entry.ts,
        )

        async def on_step(step: TrailStep) -> None:
            await self.record_step(
                step.model_copy(
                    update={
                        "investigation_id": investigation_id,
                        "n": step_offset + step.n,
                    }
                ),
                entry.ts,
            )

        failure = None
        try:
            result = await self.investigate(entry, on_step)
        except Exception as exc:  # noqa: BLE001 -- preserve and expose the investigation trail
            _log_error("investigation_failed", exc, item.id)
            failure = f"investigation failed: {type(exc).__name__}"
            result = InvestigationResult(
                investigation_id=investigation_id,
                entry=entry,
                verdict="HYPOTHESIS",
                summary=failure,
                root_cause=None,
                hypotheses=[],
                contributions=[],
                open_questions=[],
                suggested_actions=[],
                steps_used=len(self.incidents.trail(investigation_id)),
                stop_reason="error",
            )
        result = result.model_copy(
            update={"investigation_id": investigation_id, "entry": entry}
        )
        self.emit("investigation_finished", result, entry.ts)
        async with self._upsert_lock:
            reporter = self.incidents.reporter
            narrative = await (reporter or Reporter().write)(result)

            async def prepared_narrative(_: InvestigationResult) -> tuple[str, str]:
                return narrative

            # Upsert must read its snapshot after the asynchronous narrative call.
            current = self.incidents.get(item.id)
            assert current is not None
            self.incidents.reporter = prepared_narrative
            try:
                incident = await self.incidents.upsert(result)
            finally:
                self.incidents.reporter = reporter
            if current.last_seen_ts > incident.last_seen_ts:
                incident.last_seen_ts = current.last_seen_ts
                incident.updated_ts = max(incident.updated_ts, incident.last_seen_ts)
                self.incidents._save(incident)
            if failure is not None:
                incident.summary = failure
                self.incidents._save(incident)
        self.emit("incident_upserted", incident, entry.ts)
