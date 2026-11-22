"""Durable deterministic clock workflows; agents execute exclusively over A2A."""

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

from dbos import DBOS, DBOSConfiguredInstance, SetEnqueueOptions, SetWorkflowID
from pydantic import BaseModel

from eye.api.events import EventBus
from eye.config import Settings
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Event,
    SentinelDecision,
    TrailStep,
)
from eye.contracts_v2 import (
    DEFAULT_TENANT,
    Budget,
    DetectRequest,
    DetectResult,
    InvestigateRequest,
    InvestigateResult,
    MapRequest,
    ReportRequest,
    ReportResult,
    TaskMetadata,
)
from eye.control import ControlPlane, RunKilled
from eye.directory import AgentDirectory
from eye.incidents.store import IncidentStore

HOUR = timedelta(hours=1)
log = logging.getLogger(__name__)


def _log_error(event: str, exc: Exception, incident_id: str | None = None) -> None:
    log.warning(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "component": "orchestrator",
                "event": event,
                "incident_id": incident_id,
                "error": type(exc).__name__,
            }
        )
    )


class ClockSource(Protocol):
    now: datetime
    history_start: datetime
    live_start: datetime

    def step(self) -> None: ...


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
    investigations_running: int
    llm_stop_reason: str | None = None


class StepInProgress(Exception):
    pass


@DBOS.dbos_class()
class Orchestrator(DBOSConfiguredInstance):
    def __init__(
        self,
        settings: Settings,
        sim: ClockSource,
        incidents: IncidentStore,
        z_lookup: Callable[
            [str, dict[str, str], datetime], float | tuple[float, float]
        ],
        *,
        directory: AgentDirectory | None = None,
        control: ControlPlane | None = None,
        database_url: str | None = None,
        invalidate: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__("orchestrator")
        self.settings, self.sim, self.incidents, self.z_lookup = (
            settings,
            sim,
            incidents,
            z_lookup,
        )
        self.directory = directory or AgentDirectory(settings.a2a)
        self.database_url = database_url or os.environ.get(
            "EYE_DATABASE_URL", "sqlite:///data/control.sqlite"
        )
        self.control = control or ControlPlane(self.database_url)
        self.invalidate = invalidate
        self.events = EventBus()
        self.decisions: deque[SentinelDecision] = deque(maxlen=2000)
        self.tick = max(0, (sim.now - sim.live_start) // HOUR)
        self.sim_ts = sim.now - HOUR if self.tick else sim.now
        self.paused = not settings.sim.autostart
        self._clock: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._pending: set[asyncio.Task] = set()
        self._stepping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._usage = {"calls": 0, "tokens_in": 0, "tokens_out": 0}

    def emit(
        self, kind: str, payload: BaseModel | dict, ts: datetime | None = None
    ) -> None:
        event = Event(
            type=kind,
            sim_ts=ts or self.sim_ts,
            payload=payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload,
        )
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.events.publish, event)
        else:
            self.events.publish(event)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        DBOS(
            config={
                "name": "eye-orchestrator",
                "system_database_url": self.database_url,
            }
        )
        await asyncio.to_thread(DBOS.launch)
        self.queue = await asyncio.to_thread(
            DBOS.register_queue,
            "data-hours",
            global_concurrency=self.settings.orchestrator.max_concurrent_investigations,
            partition_concurrency=1,
        )
        await self.directory.resolve()
        if self.settings.sim.autostart:
            self.resume()

    async def close(self) -> None:
        self.pause()
        self._stop.set()
        if self._clock:
            await self._clock
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=30)
        self.control.close()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        if self._clock is None:
            self._clock = asyncio.create_task(self._run_clock())

    async def _run_clock(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), self.settings.sim.seconds_per_sim_hour
                )
            except TimeoutError:
                if (
                    not self.paused
                    and not self._stepping
                    and not (
                        self._pending
                        and self.settings.orchestrator.pause_sim_during_investigation
                    )
                ):
                    try:
                        await self.step_once(wait_for_analysis=False)
                    except Exception as exc:  # noqa: BLE001 -- preserve the clock
                        _log_error("clock_step_failed", exc)

    def status(self) -> Status:
        return Status(
            sim_ts=self.sim_ts,
            tick=self.tick,
            running=not self.paused,
            paused=self.paused,
            llm_mode=self.settings.llm.mode,
            llm_calls=self._usage["calls"],
            tokens_in=self._usage["tokens_in"],
            tokens_out=self._usage["tokens_out"],
            investigations_running=len(self._pending),
            agents={
                skill: AgentHealth(
                    ok=skill in self.directory.cards,
                    url=getattr(self.settings.a2a, f"{name}_url"),
                )
                for skill, name in (
                    ("detect", "sentinel"),
                    ("investigate", "detective"),
                    ("report", "reporter"),
                    ("map", "cartographer"),
                )
            },
        )

    def _metadata(self, context_id: str) -> TaskMetadata:
        settings = self.settings.orchestrator
        return TaskMetadata(
            tenant_id=DEFAULT_TENANT,
            context_id=context_id,
            traceparent=f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
            budget=Budget(
                tokens=settings.run_tokens,
                cost_usd=settings.run_cost_usd,
                deadline=datetime.now(UTC) + timedelta(seconds=settings.run_deadline_s),
            ),
        )

    async def step_once(self, *, wait_for_analysis: bool = True) -> Status:
        if self._stepping:
            raise StepInProgress("The previous clock step is still processing.")
        self._stepping = True
        try:
            ts = self.sim.now
            await asyncio.to_thread(self.sim.step)
            if self.sim.now == ts:
                return self.status()
            self.invalidate()
            self.sim_ts, self.tick = ts, self.tick + 1
            self.emit("tick", {"tick": self.tick})
            run_id = f"hour-{DEFAULT_TENANT}-{ts.isoformat()}"
            with (
                SetWorkflowID(run_id),
                SetEnqueueOptions(queue_partition_key=DEFAULT_TENANT),
            ):
                handle = await self.queue.enqueue_async(
                    self.run_hour, run_id, ts, self._metadata(run_id)
                )
            task = asyncio.create_task(handle.get_result())
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            if wait_for_analysis:
                await task
            return self.status()
        finally:
            self._stepping = False

    @DBOS.step()
    async def agent_call(
        self,
        run_id: str,
        skill: str,
        payload: BaseModel,
        metadata: TaskMetadata,
        result_type: type[BaseModel],
        incident_id: str | None = None,
    ) -> tuple[BaseModel | None, TaskMetadata]:
        self.control.check(run_id, metadata.tenant_id)
        budget = metadata.budget
        if (
            budget.tokens <= 0
            or budget.cost_usd <= 0
            or datetime.now(UTC) >= budget.deadline
        ):
            self.control.audit(
                actor="orchestrator",
                action="budget_exhausted",
                run_id=run_id,
                agent=skill,
            )
            return None, metadata
        try:
            result = await self.directory.call(skill, payload, metadata, result_type)
        except Exception as exc:  # noqa: BLE001 -- an unavailable agent must not stop lifecycle
            self.control.audit(
                actor="orchestrator",
                action="agent_error",
                run_id=run_id,
                incident_id=incident_id,
                agent=skill,
                detail={"error": type(exc).__name__},
            )
            log.warning("A2A %s failed: %s", skill, type(exc).__name__)
            return None, metadata
        self._usage["calls"] += result.usage.requests
        self._usage["tokens_in"] += result.usage.input_tokens
        self._usage["tokens_out"] += result.usage.output_tokens
        metadata = metadata.model_copy(
            update={
                "budget": budget.model_copy(
                    update={
                        "tokens": max(0, budget.tokens - result.usage.total_tokens),
                        "cost_usd": max(0, budget.cost_usd - result.usage.cost_usd),
                    }
                )
            }
        )
        self.control.audit(
            actor="orchestrator",
            action=f"agent_{result.status}",
            run_id=run_id,
            incident_id=incident_id,
            agent=skill,
            detail={"usage": result.usage.model_dump()},
        )
        if result.status == "needs_input":
            for approval in result.pending_approvals:
                self.control.pending(
                    run_id, incident_id, approval.tool_name, approval.args
                )
            if not result.pending_approvals:
                self.control.pending(
                    run_id, incident_id, "human_input", {"question": result.question}
                )
        elif result.status != "done":
            log.warning("A2A %s ended %s", skill, result.status)
        return result.result if result.status == "done" else None, metadata

    @DBOS.step()
    def _open_incidents(self) -> list:
        return [
            self.incidents.as_v2(item)
            for item in self.incidents.all()
            if item.merged_into is None
            and self.incidents.as_v2(item).status not in {"recovered", "closed"}
        ]

    @DBOS.step()
    def save_trail(self, steps: list[TrailStep]) -> None:
        for step in steps:
            self.incidents.add_step(step)
            self.emit("trail_step", step)

    @DBOS.step()
    def previous_decisions(self, ts: datetime) -> list[SentinelDecision]:
        return [item for item in self.decisions if item.anomaly.ts == ts - HOUR]

    @DBOS.workflow()
    async def run_hour(self, run_id: str, ts: datetime, metadata: TaskMetadata) -> None:
        try:
            detected, metadata = await self.agent_call(
                run_id,
                "detect",
                DetectRequest(
                    ts=ts,
                    open_incidents=self._open_incidents(),
                    previous_decisions=self.previous_decisions(ts),
                ),
                metadata,
                DetectResult,
            )
            if detected is not None:
                await self._process_decisions(run_id, detected, metadata)
            self.update_lifecycle(ts)
        except RunKilled:
            self.control.audit(actor="orchestrator", action="run_killed", run_id=run_id)

    async def _process_decisions(
        self, run_id: str, detected: DetectResult, metadata: TaskMetadata
    ) -> None:
        self.save_trail(detected.trail)
        for decision in detected.decisions:
            self.decisions.append(decision)
            self.emit("anomaly", decision.anomaly)
            self.emit("sentinel_decision", decision)
            if decision.action != "investigate":
                continue
            incident = self.incidents.match(decision)
            if incident is None:
                continue
            metadata = metadata.model_copy(update={"context_id": incident.id})
            self.emit(
                "investigation_started",
                {
                    "incident_id": incident.id,
                    "anomaly": decision.anomaly.model_dump(mode="json"),
                },
            )
            result, metadata = await self.agent_call(
                run_id,
                "investigate",
                InvestigateRequest(
                    anomaly=decision.anomaly,
                    investigation_id=incident.investigation_ids[-1]
                    if incident.investigation_ids
                    else None,
                    open_incidents=self._open_incidents(),
                ),
                metadata,
                InvestigateResult,
                incident.id,
            )
            if result is None:
                continue
            self.save_trail(result.trail)
            incident = await self.upsert(result)
            report, metadata = await self.agent_call(
                run_id,
                "report",
                ReportRequest(investigation=result.investigation),
                metadata,
                ReportResult,
                incident.id,
            )
            if report is not None:
                self.save_report(incident.id, report)
            self.emit("investigation_finished", result.investigation)
            self.emit(
                "incident_upserted",
                self.incidents.as_v2(self.incidents.get(incident.id)),
            )

    @DBOS.step()
    async def upsert(self, result: InvestigateResult):
        return await self.incidents.upsert(result.investigation)

    @DBOS.step()
    def save_report(self, incident_id: str, report: ReportResult) -> None:
        incident = self.incidents.get(incident_id)
        incident.title, incident.summary = report.title, report.summary
        self.incidents._save(incident)

    @DBOS.step()
    def update_lifecycle(self, ts: datetime) -> None:
        for incident in self.incidents.lifecycle(ts, self.z_lookup):
            self.emit("incident_upserted", self.incidents.as_v2(incident), ts)

    @DBOS.workflow()
    async def map_metric(
        self, run_id: str, request: AddMetricRequest, metadata: TaskMetadata
    ) -> AddMetricResult:
        result, _ = await self.agent_call(
            run_id, "map", MapRequest(metric=request), metadata, AddMetricResult
        )
        if result is None:
            raise ValueError("Cartographer did not complete the request")
        self.emit("graph_changed", {})
        return result

    async def register_metric(self, request: AddMetricRequest) -> AddMetricResult:
        run_id = f"map-{uuid4().hex}"
        with (
            SetWorkflowID(run_id),
            SetEnqueueOptions(queue_partition_key=DEFAULT_TENANT),
        ):
            handle = await self.queue.enqueue_async(
                self.map_metric, run_id, request, self._metadata(run_id)
            )
        return await handle.get_result()
