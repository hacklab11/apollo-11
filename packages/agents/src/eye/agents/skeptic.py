"""Check whether a measured anomaly is explained by a plan or normal seasonality."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from importlib.resources import files
from typing import cast

import duckdb
from pydantic import BaseModel

from eye.agents.investigation_tools import (
    _ControlInput,
    _EventsInput,
    _MetricInput,
    _Window,
    _WindowTools,
)
from eye.config import LlmSettings, SentinelSettings
from eye.contracts import Anomaly, Hypothesis, TrailStep
from eye.contracts_v2 import SkepticResult
from eye.llm.client import BudgetExceeded
from eye.llm.runtime import AgentSpec, ToolSpec, run_agent
from eye.registry.loader import Registry
from eye.tools.models import (
    BaselineCompareOutput,
    ListEventsOutput,
    PlannedEffectCheckInput,
    PlannedEffectCheckOutput,
)
from eye.warehouse import operations as ops
from eye.warehouse.queries import DataQueries, LocalQueries

_PROMPT = files("eye.llm").joinpath("prompts/skeptic.md")
_MAX_STEPS = 6


class _PlanInput(BaseModel):
    event_id: str


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
        if not isinstance(self._data, LocalQueries):
            spec.tools = [tool.offloaded() for tool in spec.tools]
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
            freshness = await asyncio.to_thread(tools.freshness, entry)
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
