"""Check whether a measured anomaly is explained by a plan or normal seasonality."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.resources import files
from typing import Literal, cast

import duckdb
from pydantic import AwareDatetime, BaseModel, Field

from eye.config import LlmSettings, SentinelSettings
from eye.contracts import Anomaly, Dimension, Evidence, Hypothesis, TrailStep
from eye.llm.client import BudgetExceeded
from eye.llm.runtime import AgentSpec, ToolSpec, run_agent
from eye.registry.loader import Registry
from eye.tools.models import (
    BaselineCompareInput,
    BaselineCompareOutput,
    CheckFreshnessInput,
    CheckFreshnessOutput,
    CompareSegmentsInput,
    CompareSegmentsOutput,
    ListEventsInput,
    ListEventsOutput,
    PlannedEffectCheckInput,
    PlannedEffectCheckOutput,
)
from eye.warehouse import operations as ops
from eye.warehouse.queries import DataQueries, LocalQueries

_HOUR = timedelta(hours=1)
_PROMPT = files("eye.llm").joinpath("prompts/skeptic.md")
_MAX_STEPS = 6


class SkepticResult(BaseModel):
    verdict: Literal["innocent", "partly_innocent", "not_innocent", "data_issue"]
    explained_share: float = Field(ge=0, le=1)
    reasons: list[str]
    evidence: list[Evidence]


class _MetricInput(BaseModel):
    metric: str
    segment: dict[Dimension, str] = Field(default_factory=dict)


class _ControlInput(_MetricInput):
    dim: Dimension
    value_a: str
    value_b: str


class _EventsInput(BaseModel):
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    system: str | None = None
    kind: str | None = None
    country: str | None = None
    planned: bool | None = None


class _PlanInput(BaseModel):
    event_id: str


@dataclass(frozen=True)
class _Window:
    start: datetime
    end: datetime

    @classmethod
    def from_anomaly(cls, anomaly: Anomaly) -> "_Window":
        end = anomaly.ts + _HOUR
        return cls(end - anomaly.window_hours * _HOUR, end)

    @property
    def lookback(self) -> datetime:
        return self.start - timedelta(hours=24)

    def bounded(
        self, start: datetime | None, end: datetime | None
    ) -> dict[str, datetime]:
        start = start or self.lookback
        end = end or self.end
        if not self.lookback <= start < end <= self.end:
            raise ValueError("Use a time range within evidence_start and window_end")
        return {"start": start, "end": end}

    def bounds(self) -> dict[str, datetime]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class _WindowTools:
    data: DataQueries
    window: _Window

    def baseline(self, args: _MetricInput) -> BaselineCompareOutput:
        return self.data.read(
            ops.BASELINE,
            BaselineCompareInput(
                name=args.metric, segment=args.segment, **self.window.bounds()
            ),
        )

    def freshness(self, args: _MetricInput) -> CheckFreshnessOutput:
        return self.data.read(
            ops.FRESHNESS,
            CheckFreshnessInput(**args.model_dump(), **self.window.bounds()),
        )

    def controls(self, args: _ControlInput) -> CompareSegmentsOutput:
        segment = {dim: value for dim, value in args.segment.items() if dim != args.dim}
        return self.data.read(
            ops.COMPARE,
            CompareSegmentsInput(
                **args.model_dump(exclude={"segment"}),
                segment=segment,
                **self.window.bounds(),
            ),
        )

    def events(
        self, args: _EventsInput, *, planned_only: bool = False
    ) -> ListEventsOutput:
        filters = args.model_dump(exclude={"start", "end", "planned"})
        return self.data.read(
            ops.EVENTS,
            ListEventsInput(
                **filters,
                **self.window.bounded(args.start, args.end),
                planned=True if planned_only else args.planned,
            ),
        )


class Skeptic:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection | None = None,
        registry: Registry | None = None,
        *,
        freshness: SentinelSettings | None = None,
        llm: LlmSettings | None = None,
        data: DataQueries | None = None,
    ) -> None:
        self._freshness = freshness or SentinelSettings()
        self._llm = llm or LlmSettings()
        if data is None:
            assert con is not None and registry is not None
            data = LocalQueries(con, registry, settings=self._freshness)
        self._data = data

    async def review(
        self,
        anomaly: Anomaly,
        investigation_id: str,
        on_step: Callable[[TrailStep], Awaitable[None]],
        *,
        hypothesis: Hypothesis | None = None,
    ) -> SkepticResult:
        window = _Window.from_anomaly(anomaly)
        tools = _WindowTools(self._data, window)
        entry = _MetricInput(metric=anomaly.metric, segment=anomaly.segment)
        checks: dict[str, PlannedEffectCheckOutput] = {}
        seasonal = False

        def baseline(args: _MetricInput) -> BaselineCompareOutput:
            nonlocal seasonal
            result = tools.baseline(args)
            if args == entry:
                seasonal = bool(result.points) and all(
                    point.z is not None and abs(point.z) < self._freshness.z_threshold
                    for point in result.points
                )
            return result

        def plans(args: _EventsInput) -> ListEventsOutput:
            return tools.events(args, planned_only=True)

        def check_plan(args: _PlanInput) -> PlannedEffectCheckOutput:
            scoped_events = plans(_EventsInput(country=anomaly.segment.get("country")))
            if not any(
                event.id == args.event_id
                and all(
                    entry.segment.get(dim) in value
                    if isinstance(value, list)
                    else entry.segment.get(dim) == value
                    for dim, value in event.scope.items()
                )
                for event in scoped_events.events
            ):
                raise ValueError("Plan is outside the entry segment or evidence window")
            result = self._data.read(
                ops.PLANNED,
                PlannedEffectCheckInput(
                    event_id=args.event_id, **entry.model_dump(), **window.bounds()
                ),
            )
            checks[args.event_id] = result
            return result

        spec = AgentSpec(
            name="skeptic",
            system_prompt_file=str(_PROMPT),
            tools=[
                ToolSpec(
                    "list_events", "List planned events only.", _EventsInput, plans
                ),
                ToolSpec(
                    "planned_effect_check",
                    "Compare this plan to the entry metric and segment.",
                    _PlanInput,
                    check_plan,
                ),
                ToolSpec(
                    "check_freshness",
                    "Check metric completeness.",
                    _MetricInput,
                    tools.freshness,
                ),
                ToolSpec(
                    "baseline_compare",
                    "Compare to the seasonal baseline.",
                    _MetricInput,
                    baseline,
                ),
                ToolSpec(
                    "compare_segments",
                    "Compare affected and control segments.",
                    _ControlInput,
                    tools.controls,
                ),
            ],
            submit_model=SkepticResult,
            effort="medium",
            model=self._llm.model_for("skeptic"),
            max_steps=_MAX_STEPS,
            max_tokens=self._llm.max_tokens,
        )
        payload = {
            "entry": anomaly.model_dump(mode="json"),
            "hypothesis": hypothesis.model_dump(mode="json") if hypothesis else None,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "evidence_start": window.lookback.isoformat(),
        }
        try:
            result = cast(
                SkepticResult,
                await run_agent(
                    spec, json.dumps(payload, sort_keys=True), investigation_id, on_step
                ),
            )
        except BudgetExceeded:
            return SkepticResult(
                verdict="not_innocent",
                explained_share=0,
                reasons=["Skeptic ran out of budget; innocence was not established."],
                evidence=[],
            )

        matches = [
            check.explained_share
            for check in checks.values()
            if check.verdict == "matches"
        ]
        if result.verdict in {"innocent", "data_issue"}:
            freshness = tools.freshness(entry)
            if freshness.verdict != "ok":
                return result.model_copy(
                    update={
                        "verdict": "data_issue",
                        "explained_share": 0.0,
                        "reasons": [*result.reasons, freshness.summary],
                    }
                )
            if result.verdict == "data_issue":
                return result.model_copy(
                    update={
                        "verdict": "not_innocent",
                        "explained_share": 0.0,
                        "reasons": [
                            *result.reasons,
                            "Entry data is complete and on time.",
                        ],
                    }
                )
        if result.verdict == "innocent":
            if matches:
                return result.model_copy(update={"explained_share": max(matches)})
            if seasonal:
                return result.model_copy(update={"explained_share": 1.0})
            return result.model_copy(
                update={
                    "verdict": "not_innocent",
                    "explained_share": 0.0,
                    "reasons": [
                        *result.reasons,
                        "No matching planned effect or normal seasonal baseline.",
                    ],
                }
            )
        if result.verdict == "partly_innocent":
            share = max(
                (
                    check.explained_share
                    for check in checks.values()
                    if check.verdict in {"matches", "exceeds"}
                ),
                default=0.0,
            )
            return result.model_copy(
                update={
                    "verdict": "partly_innocent" if share else "not_innocent",
                    "explained_share": share,
                }
            )
        return result.model_copy(update={"explained_share": 0.0})
