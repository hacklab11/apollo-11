"""Triage a simulation hour without starting investigations."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from importlib.resources import files
from typing import Protocol, cast

import duckdb
from pydantic import BaseModel, Field

from eye.config import LlmSettings, SentinelSettings
from eye.contracts import (
    Anomaly,
    AnomalyKind,
    GraphView,
    Incident,
    SentinelDecision,
    TrailStep,
)
from eye.llm.runtime import AgentSpec, ToolSpec, run_agent
from eye.registry.loader import Registry
from eye.tools.context import check_freshness, list_events
from eye.tools.models import (
    BaselineCompareInput,
    CheckFreshnessInput,
    ListEventsInput,
    ScanAnomaliesInput,
    ScanDivergentSegmentsInput,
)
from eye.tools.stats import baseline_compare, scan_anomalies, scan_divergent_segments

_HOUR = timedelta(hours=1)
_PROMPT = files("eye.llm").joinpath("prompts/sentinel.md")
# Compact ASCII JSON at roughly 3 characters per token; counting via the API costs a call.
_MAX_CONTEXT_CHARS = 18_000
_OPEN = {"investigating", "open", "persisting"}
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
        return (
            self.anomaly.metric,
            tuple(sorted(self.anomaly.segment.items())),
            self.anomaly.kind,
        )


async def _discard_step(step: TrailStep) -> None:
    pass


class Sentinel:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        registry: Registry,
        *,
        settings: SentinelSettings | None = None,
        llm: LlmSettings | None = None,
        graph: GraphSource | None = None,
    ) -> None:
        self._con = con
        self._registry = registry
        self._settings = settings or SentinelSettings()
        self._llm = llm or LlmSettings()
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

    def _spec(self) -> AgentSpec:
        return AgentSpec(
            name="sentinel",
            system_prompt_file=str(_PROMPT),
            tools=[
                ToolSpec(
                    "baseline_compare",
                    "Compare actual and expected metric values for an hourly window.",
                    BaselineCompareInput,
                    partial(baseline_compare, con=self._con, registry=self._registry),
                ),
                ToolSpec(
                    "check_freshness",
                    "Check pipeline completeness for a metric and segment or a list of tables.",
                    CheckFreshnessInput,
                    partial(
                        check_freshness,
                        con=self._con,
                        registry=self._registry,
                        settings=self._settings,
                    ),
                ),
                ToolSpec(
                    "list_events",
                    "Read change-log and external status events in a time window.",
                    ListEventsInput,
                    partial(list_events, con=self._con),
                ),
                ToolSpec(
                    "get_graph",
                    "Read the whole metric graph or a neighborhood by node and depth.",
                    _GetGraphInput,
                    self._get_graph,
                ),
            ],
            submit_model=SentinelSubmission,
            effort="low",
            model=self._llm.model_for("sentinel"),
            max_steps=4,
            max_tokens=self._llm.max_tokens,
        )

    def _candidates(self, ts: datetime) -> list[_Candidate]:
        graph = self._graph.view() if self._graph is not None else None
        scan_options = {
            "con": self._con,
            "registry": self._registry,
            "settings": self._settings,
            "graph": graph,
        }
        point = scan_anomalies(ScanAnomaliesInput(ts=ts), **scan_options)
        divergent = scan_divergent_segments(
            ScanDivergentSegmentsInput(ts=ts), **scan_options
        )
        ranked = sorted(
            point.anomalies + divergent.anomalies, key=lambda a: (-a.priority, a.id)
        )
        candidates = []
        for anomaly in ranked[: self._settings.top_k]:
            fresh = check_freshness(
                CheckFreshnessInput(
                    metric=anomaly.metric,
                    segment=anomaly.segment,
                    start=ts - (anomaly.window_hours - 1) * _HOUR,
                    end=ts + _HOUR,
                ),
                con=self._con,
                registry=self._registry,
                settings=self._settings,
            )
            completeness = min(
                [anomaly.completeness, *(p.completeness for p in fresh.points)]
            )
            candidates.append(_Candidate(anomaly, completeness, fresh.verdict))
        return candidates

    def _prefetch(
        self, ts: datetime, incidents: Sequence[Incident], previous: set[_WatchKey]
    ) -> tuple[str, list[_Candidate]]:
        candidates = self._candidates(ts)
        if not candidates:
            return "", []
        end = ts + _HOUR
        events = list_events(
            ListEventsInput(start=end - 24 * _HOUR, end=end), con=self._con
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
        open_incidents: Sequence[Incident] = (),
        on_step: Callable[[TrailStep], Awaitable[None]] = _discard_step,
    ) -> list[SentinelDecision]:
        previous = self._watched if self._watch_tick == ts - _HOUR else set()
        content, candidates = await asyncio.to_thread(
            self._prefetch, ts, open_incidents, previous
        )
        if not candidates:
            self._watched.clear()
            self._watch_tick = ts
            return []

        submitted = cast(
            SentinelSubmission,
            await run_agent(
                self._spec(), content, f"sentinel:{ts.isoformat()}", on_step
            ),
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
