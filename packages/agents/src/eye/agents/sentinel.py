"""Triage a simulation hour without starting investigations."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Protocol, cast
from uuid import uuid4

import duckdb
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import Model

from agent_runtime import (
    Budget,
    KillSwitch,
    PriceTable,
    RunLimits,
    RunScope,
    RunUsageSummary,
    end_state_outputs,
    run_agent,
)
from agent_runtime.configured_models import build_model, price_table
from eye.config import LlmSettings, SentinelSettings
from eye.contracts import (
    Anomaly,
    AnomalyKind,
    GraphView,
    Incident,
    SentinelDecision,
    TrailStep,
)
from eye.contracts_v2 import IncidentV2
from eye.registry.loader import Registry
from eye.tools.models import (
    BaselineCompareInput,
    CheckFreshnessInput,
    ListEventsInput,
    ScanAnomaliesInput,
    ScanDivergentSegmentsInput,
)
from eye.warehouse import operations as ops
from eye.warehouse.queries import DataQueries, LocalQueries

_HOUR = timedelta(hours=1)
_PROMPT = files("eye.llm").joinpath("prompts/sentinel.md")
# Compact ASCII JSON at roughly 3 characters per token; counting via the API costs a call.
_MAX_CONTEXT_CHARS = 18_000
_OPEN = {"investigating", "open", "persisting", "mitigating"}
type _WatchKey = tuple[str, tuple[tuple[str, str], ...], AnomalyKind]


class GraphSource(Protocol):
    def view(self, node: str | None = None, depth: int | None = None) -> GraphView: ...


class _GetGraphInput(BaseModel):
    node: str | None = None
    depth: int | None = Field(default=None, ge=0)


class _GraphOutput(GraphView):
    summary: str = Field(max_length=300)


class SentinelSubmission(BaseModel):
    decisions: list[SentinelDecision]


@dataclass(frozen=True)
class _Candidate:
    anomaly: Anomaly
    completeness: float
    freshness: str

    @property
    def watch_key(self) -> _WatchKey:
        return _watch_key(self.anomaly)


def _watch_key(anomaly: Anomaly) -> _WatchKey:
    return (anomaly.metric, tuple(sorted(anomaly.segment.items())), anomaly.kind)


async def _discard_step(step: TrailStep) -> None:
    pass


class Sentinel:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection | None = None,
        registry: Registry | None = None,
        *,
        settings: SentinelSettings | None = None,
        llm: LlmSettings | None = None,
        graph: GraphSource | None = None,
        data: DataQueries | None = None,
        kill_switch: KillSwitch | None = None,
        prices: PriceTable | None = None,
        model: Model | None = None,
    ) -> None:
        self._settings = settings or SentinelSettings()
        self._model = model
        if data is None:
            assert con is not None and registry is not None
            data = LocalQueries(con, registry, graph=graph, settings=self._settings)
        self._data = data
        self._llm = llm or LlmSettings()
        self._kill_switch = kill_switch
        self._prices = prices or price_table(self._llm)
        self._graph = graph
        self._watch_tick: datetime | None = None
        self._watched: set[_WatchKey] = set()

    def _get_graph(self, args: _GetGraphInput) -> _GraphOutput:
        if self._graph is None:
            return _GraphOutput(
                nodes=[], edges=[], summary="Graph unavailable: 0 nodes and 0 edges."
            )
        view = self._graph.view(node=args.node, depth=args.depth)
        return _GraphOutput(
            nodes=view.nodes,
            edges=view.edges,
            summary=f"Graph: {len(view.nodes)} nodes and {len(view.edges)} edges.",
        )

    def _agent(
        self, run_id: str, on_step: Callable[[TrailStep], Awaitable[None]]
    ) -> Agent:
        agent = Agent(
            self._model or build_model(self._llm, "sentinel"),
            instructions=_PROMPT.read_text(),
            output_type=[
                ToolOutput(SentinelSubmission, name="submit"),
                *end_state_outputs(SentinelSubmission)[1:],
            ],
            model_settings={"max_tokens": self._llm.max_tokens},
        )
        n = 0

        async def record(name: str, args: BaseModel, output: BaseModel) -> None:
            nonlocal n
            n += 1
            await on_step(
                TrailStep(
                    id=str(uuid4()),
                    investigation_id=run_id,
                    n=n,
                    real_ts=datetime.now(UTC),
                    actor="sentinel",
                    action="tool_call",
                    tool=name,
                    input=args.model_dump(mode="json"),
                    output=output.model_dump(mode="json"),
                    output_summary=getattr(
                        output, "summary", "Sentinel submitted decisions."
                    ),
                )
            )

        async def read(
            name: str, operation: ops.Operation, args: BaseModel
        ) -> BaseModel:
            if isinstance(self._data, LocalQueries):
                output = self._data.read(operation, args)
            else:
                output = await asyncio.to_thread(self._data.read, operation, args)
            await record(name, args, output)
            return output

        @agent.tool_plain
        async def baseline_compare(args: BaselineCompareInput) -> BaseModel:
            """Compare actual and expected metric values for an hourly window."""
            return await read("baseline_compare", ops.BASELINE, args)

        @agent.tool_plain
        async def check_freshness(args: CheckFreshnessInput) -> BaseModel:
            """Check pipeline completeness for metrics or tables."""
            return await read("check_freshness", ops.FRESHNESS, args)

        @agent.tool_plain
        async def list_events(args: ListEventsInput) -> BaseModel:
            """Read change-log and external status events in a time window."""
            return await read("list_events", ops.EVENTS, args)

        @agent.tool_plain
        async def get_graph(args: _GetGraphInput) -> _GraphOutput:
            """Read the metric graph or a neighborhood."""
            output = await asyncio.to_thread(self._get_graph, args)
            await record("get_graph", args, output)
            return output

        return agent

    def _candidates(self, ts: datetime) -> list[_Candidate]:
        point = self._data.read(ops.SCAN, ScanAnomaliesInput(ts=ts))
        divergent = self._data.read(ops.DIVERGENT, ScanDivergentSegmentsInput(ts=ts))
        ranked = sorted(
            point.anomalies + divergent.anomalies, key=lambda a: (-a.priority, a.id)
        )
        candidates = []
        for anomaly in ranked[: self._settings.top_k]:
            fresh = self._data.read(
                ops.FRESHNESS,
                CheckFreshnessInput(
                    metric=anomaly.metric,
                    segment=anomaly.segment,
                    start=ts - (anomaly.window_hours - 1) * _HOUR,
                    end=ts + _HOUR,
                ),
            )
            completeness = min(
                [anomaly.completeness, *(p.completeness for p in fresh.points)]
            )
            candidates.append(_Candidate(anomaly, completeness, fresh.verdict))
        return candidates

    def _prefetch(
        self,
        ts: datetime,
        incidents: Sequence[Incident | IncidentV2],
        previous: set[_WatchKey],
    ) -> tuple[str, list[_Candidate]]:
        candidates = self._candidates(ts)
        if not candidates:
            return "", []
        end = ts + _HOUR
        events = self._data.read(
            ops.EVENTS, ListEventsInput(start=end - 24 * _HOUR, end=end)
        )
        shown_candidates: list[dict[str, object]] = []
        shown_incidents: list[dict[str, object]] = []
        shown_events: list[dict[str, object]] = []
        payload = {
            "ts": ts.isoformat(),
            "min_completeness": self._settings.min_completeness,
            "candidates": shown_candidates,
            "open_incidents": shown_incidents,
            "events": shown_events,
        }

        def fill(
            target: list[dict[str, object]], rows: Iterable[dict[str, object]]
        ) -> None:
            for row in rows:
                target.append(row)
                if len(serialize()) > _MAX_CONTEXT_CHARS:
                    target.pop()
                    break

        def serialize() -> str:
            return json.dumps(payload, separators=(",", ":"), sort_keys=True)

        fill(
            shown_candidates,
            (
                {
                    "anomaly": item.anomaly.model_dump(mode="json", exclude_none=True),
                    "freshness": item.freshness,
                    "completeness": item.completeness,
                    "watched_last_tick": item.watch_key in previous,
                }
                for item in candidates
            ),
        )
        fill(
            shown_incidents,
            (
                {
                    "id": incident.id,
                    "fingerprint": incident.fingerprint,
                    "title": incident.title,
                    "metric": incident.entry.metric,
                    "segment": incident.entry.segment,
                    "root_node": incident.root_cause.root_node
                    if incident.root_cause
                    else None,
                }
                for incident in sorted(
                    incidents,
                    key=lambda incident: (incident.last_seen_ts, incident.id),
                    reverse=True,
                )
                if incident.status in _OPEN and incident.merged_into is None
            ),
        )
        fill(
            shown_events,
            (
                event.model_dump(mode="json", exclude_none=True)
                for event in reversed(events.events)
            ),
        )
        return serialize(), candidates[: len(shown_candidates)]

    async def run(
        self,
        ts: datetime,
        *,
        open_incidents: Sequence[Incident | IncidentV2] = (),
        on_step: Callable[[TrailStep], Awaitable[None]] = _discard_step,
        previous_decisions: Sequence[SentinelDecision] | None = None,
        budget: Budget | None = None,
        scope: RunScope | None = None,
        on_usage: Callable[[RunUsageSummary], None] | None = None,
    ) -> list[SentinelDecision]:
        if previous_decisions is None:
            previous = self._watched if self._watch_tick == ts - _HOUR else set()
        else:
            previous = {
                _watch_key(decision.anomaly)
                for decision in previous_decisions
                if decision.action == "watch" and decision.anomaly.ts == ts - _HOUR
            }
        content, candidates = await asyncio.to_thread(
            self._prefetch, ts, open_incidents, previous
        )
        if not candidates:
            self._watched.clear()
            self._watch_tick = ts
            return []

        run_id = f"sentinel:{ts.isoformat()}"
        outcome = await run_agent(
            self._agent(run_id, on_step),
            content,
            scope=scope or RunScope(run_id=run_id, tenant_id="eye"),
            limits=RunLimits(
                max_steps=5,
                max_tool_calls=16,
                max_tokens=64000,
                max_cost_usd=1,
                max_wall_clock_s=60,
            ),
            prices=self._prices,
            kill_switch=self._kill_switch,
            budget=budget,
        )
        if on_usage is not None:
            on_usage(outcome.result.usage)
        await on_step(
            TrailStep(
                id=str(uuid4()),
                investigation_id=run_id,
                n=0,
                real_ts=datetime.now(UTC),
                actor="sentinel",
                action="llm_call",
                output_summary=f"Model finished after {outcome.result.usage.requests} requests.",
                tokens_in=outcome.result.usage.input_tokens,
                tokens_out=outcome.result.usage.output_tokens,
            )
        )
        if outcome.result.status != "done":
            error = (
                UsageLimitExceeded
                if outcome.result.status == "gave_up"
                else RuntimeError
            )
            raise error(
                f"Sentinel {outcome.result.status}: {outcome.result.reason or outcome.result.question}"
            )
        submitted = cast(SentinelSubmission, outcome.result.result)
        for message in outcome.messages:
            if isinstance(message, ModelResponse):
                for part in message.parts:
                    if isinstance(part, ToolCallPart) and part.tool_name == "submit":
                        await on_step(
                            TrailStep(
                                id=str(uuid4()),
                                investigation_id=run_id,
                                n=outcome.result.usage.tool_calls + 1,
                                real_ts=datetime.now(UTC),
                                actor="sentinel",
                                action="tool_call",
                                tool="submit",
                                input=part.args_as_dict(),
                                output=submitted.model_dump(mode="json"),
                                output_summary=f"Submitted {len(submitted.decisions)} decisions.",
                            )
                        )
        by_id: dict[str, SentinelDecision] = {}
        for decision in submitted.decisions:
            by_id.setdefault(decision.anomaly.id, decision)

        decisions = []
        watched = set()
        for candidate in candidates:
            anomaly = candidate.anomaly
            decision = by_id.get(anomaly.id)
            minimum = self._settings.min_completeness
            if candidate.completeness < minimum:
                decision = SentinelDecision(
                    anomaly=anomaly,
                    action="defer",
                    reason=f"Data incomplete: {candidate.completeness:.2%} completeness is below {minimum:.2%}.",
                )
            elif decision is None:
                continue
            else:
                decision = decision.model_copy(update={"anomaly": anomaly})
                if decision.action == "watch":
                    if candidate.watch_key in previous:
                        decision = SentinelDecision(
                            anomaly=anomaly,
                            action="investigate",
                            reason=f"Watched in two consecutive hours. {decision.reason}",
                        )
                    else:
                        watched.add(candidate.watch_key)
            decisions.append(decision)

        self._watched = watched
        self._watch_tick = ts
        return decisions
