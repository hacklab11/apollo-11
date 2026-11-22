"""Lead an investigation and enforce the evidence required for its verdict."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from inspect import isawaitable
from itertools import count
from math import fsum
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import duckdb
from pydantic import AwareDatetime, BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model, ModelRequestContext

from agent_runtime import (
    Budget,
    KillSwitch,
    RunLimits,
    RunScope,
    RunUsageSummary,
    end_state_outputs,
    run_agent,
)
from eye.a2a_client import A2AClient
from eye.agents.investigation_tools import (
    _ControlInput,
    _EventsInput,
    _MetricInput,
    _Window,
    _WindowTools,
)
from eye.agents.models import build_model, price_table
from eye.config import DetectiveSettings, LlmSettings, SentinelSettings
from eye.contracts import (
    Anomaly,
    Contribution,
    Dimension,
    Evidence,
    GraphView,
    Hypothesis,
    Incident,
    InvestigationResult,
    MetricDef,
    Series,
    SpecialistRequest,
    SpecialistResponse,
    Symptom,
    TrailStep,
    Verdict,
)
from eye.contracts_v2 import IncidentV2, SkepticResult
from eye.llm.client import BudgetExceeded, LLMUnavailable
from eye.registry.loader import Registry
from eye.tools.drill import AttributionResult
from eye.tools.models import (
    CompareWindowInput,
    DrillDownInput,
    DrillDownOutput,
    FirstChangeInput,
    FirstChangeOutput,
    ToolOutput,
)
from eye.warehouse import operations as ops
from eye.warehouse.queries import DataQueries, LocalQueries

_PROMPT = files("eye.llm").joinpath("prompts/detective.md")
_WEEK = timedelta(weeks=1)


@dataclass
class _Deps:
    investigation_id: str
    emit: Callable[[TrailStep], Awaitable[None]]


class _Trail(AbstractCapability[_Deps]):
    async def after_model_request(
        self,
        ctx: RunContext[_Deps],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        await ctx.deps.emit(
            TrailStep(
                id=uuid4().hex,
                investigation_id=ctx.deps.investigation_id,
                n=0,
                real_ts=datetime.now(UTC),
                actor="detective",
                action="llm_call",
                output_summary="Model turn finished",
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
            )
        )
        return response


class _DrillInput(_MetricInput):
    dim: Dimension


class _QueryInput(_MetricInput):
    group_by: list[Dimension] = Field(default_factory=list)


class _SeriesOutput(ToolOutput):
    series: list[Series]


class _ChangeInput(BaseModel):
    metrics: list[str]
    segment: dict[Dimension, str] = Field(default_factory=dict)
    start: AwareDatetime | None = None


class _GraphInput(BaseModel):
    node: str | None = None
    depth: int = 2


class _GraphOutput(GraphView, ToolOutput):
    cycles: list[list[str]]


class _SpecialistInput(_MetricInput):
    agent: Literal["growth", "marketplace", "discovery", "selleros"]
    question: str


class _SpecialistOutput(SpecialistResponse, ToolOutput):
    pass


class _SkepticInput(BaseModel):
    hypothesis_id: str


class _SkepticOutput(SkepticResult, ToolOutput):
    pass


class _BoardInput(BaseModel):
    hypotheses: list[Hypothesis]


class _BoardOutput(_BoardInput, ToolOutput):
    steps_used: int


class _Submission(BaseModel):
    verdict: Verdict
    summary: str
    root_cause: Hypothesis | None
    hypotheses: list[Hypothesis]
    contributions: list[Contribution]
    loop: list[str] | None = None
    trigger: str | None = None
    affected_actors: list[str] = Field(default_factory=list)
    affected_sellers: list[str] = Field(default_factory=list)
    open_questions: list[str]
    suggested_actions: list[str]


def _upstream(graph: GraphView, node: str, depth: int) -> GraphView:
    names = frontier = {node}
    for _ in range(depth):
        frontier = {
            edge.src
            for edge in graph.edges
            if edge.dst in frontier
            and edge.type != "breakdown"
            and edge.status != "rejected"
        } - names
        names |= frontier
    return GraphView(
        nodes=[item for item in graph.nodes if item["id"] in names],
        edges=[edge for edge in graph.edges if edge.src in names and edge.dst in names],
    )


def _stop_failures(
    answer: _Submission,
    settings: DetectiveSettings,
    *,
    innocent: bool,
    stale: bool,
) -> list[str]:
    if answer.verdict == "CONFIRMED":
        failures = []
        root = answer.root_cause
        if root is None or root.explained_share < settings.confirm_share:
            failures.append(f"Root must explain at least {settings.confirm_share:.0%}.")
        if root is not None and root.status == "refuted":
            failures.append("Root hypothesis was refuted.")
        groups = {item.group for item in root.evidence_for} if root else set()
        if len(groups) < settings.min_independent_evidence:
            failures.append(
                f"Need {settings.min_independent_evidence} independent evidence groups; "
                f"found {len(groups)}."
            )
        if any(
            item.explained_share >= 0.2
            and item.status != "refuted"
            and (root is None or item.id != root.id)
            for item in answer.hypotheses
        ):
            failures.append(
                "A competing hypothesis explaining at least 20% was not refuted."
            )
        return failures
    if answer.verdict == "MIXED":
        supported = {
            hyp.id: hyp for hyp in answer.hypotheses if hyp.status == "supported"
        }
        if (
            len(supported) < 2
            or fsum(h.explained_share for h in supported.values())
            < settings.confirm_share
        ):
            return [
                f"At least two supported hypotheses must explain {settings.confirm_share:.0%} together."
            ]
        factors = {
            row.factor
            for row in answer.contributions
            if row.kind != "residual" and row.share != 0
        }
        roots = [hyp.root_node for hyp in supported.values()]
        if any(
            hyp.id not in factors
            and (hyp.root_node not in factors or roots.count(hyp.root_node) != 1)
            for hyp in supported.values()
        ):
            return ["Contributions must include every supported hypothesis."]
    if answer.verdict == "INNOCENT" and not innocent:
        return ["Skeptic did not establish a matching planned effect or seasonality."]
    if answer.verdict == "DATA_ISSUE" and not stale:
        return ["Entry data is not late or incomplete in the investigation window."]
    return []


def _contributions(rows: list[Contribution], delta: float) -> list[Contribution]:
    if abs(1 - fsum(row.share for row in rows)) <= 0.02:
        return rows
    attributed = [row for row in rows if row.kind != "residual"]
    share = 1 - fsum(row.share for row in attributed)
    return [
        *attributed,
        Contribution(
            factor="residual", kind="residual", delta=delta * share, share=share
        ),
    ]


def _publish_hypothesis_ids(
    answer: _Submission, investigation_id: str, prior: dict[str, Hypothesis]
) -> None:
    ids = {}
    for hypothesis in answer.hypotheses:
        existing = prior.get(hypothesis.id)
        same_cause = existing and (
            existing.root_node,
            existing.cause_type,
            existing.segment,
            existing.first_change_ts,
        ) == (
            hypothesis.root_node,
            hypothesis.cause_type,
            hypothesis.segment,
            hypothesis.first_change_ts,
        )
        ids[hypothesis.id] = (
            hypothesis.id if same_cause else f"{investigation_id}:{hypothesis.id}"
        )
    answer.trigger = ids.get(answer.trigger) if answer.trigger is not None else None
    answer.hypotheses = [
        hyp.model_copy(update={"id": ids[hyp.id]}) for hyp in answer.hypotheses
    ]
    if root := answer.root_cause:
        answer.root_cause = root.model_copy(update={"id": ids[root.id]})
    answer.contributions = [
        row.model_copy(update={"factor": ids[row.factor]})
        if row.factor in ids and row.kind != "residual"
        else row
        for row in answer.contributions
    ]


class DetectiveGraph(Protocol):
    def view(self, node: str | None = None, depth: int | None = None) -> GraphView: ...

    def metrics(self) -> list[MetricDef]: ...

    def cycles_through(self, node: str) -> list[list[str]]: ...


class SkepticReview(Protocol):
    async def __call__(
        self,
        anomaly: Anomaly,
        investigation_id: str,
        on_step: Callable[[TrailStep], Awaitable[None]],
        *,
        hypothesis: Hypothesis | None = None,
    ) -> SkepticResult: ...


class Detective:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection | None,
        registry: Registry | None,
        graph: DetectiveGraph,
        *,
        a2a: A2AClient | None = None,
        settings: DetectiveSettings | None = None,
        freshness: SentinelSettings | None = None,
        llm: LlmSettings | None = None,
        data: DataQueries | None = None,
        review: SkepticReview | None = None,
        model: Model | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._registry = registry
        self._graph = graph
        self._a2a = a2a
        self._settings = settings or DetectiveSettings()
        self._freshness = freshness or SentinelSettings()
        self._llm = llm or LlmSettings()
        if data is None:
            assert con is not None and registry is not None
            data = LocalQueries(con, registry, settings=self._freshness)
        self._data = data
        self._review = review
        self._model = model
        self._kill_switch = kill_switch

    def _metrics(self) -> dict[str, MetricDef]:
        if self._registry is not None:
            return self._registry.metrics
        return {metric.name: metric for metric in self._graph.metrics()}

    def _first_changes(
        self,
        segment: dict[str, str],
        window: _Window,
        nodes: Iterable[str],
        catalog: dict[str, MetricDef],
    ) -> dict[str, datetime | None]:
        scoped: dict[tuple[tuple[str, str], ...], list[str]] = {}
        for node in sorted(nodes):
            dims = catalog[node].dimensions
            scope = tuple(
                sorted((dim, value) for dim, value in segment.items() if dim in dims)
            )
            scoped.setdefault(scope, []).append(node)
        changes: dict[str, datetime | None] = {}
        for scope, metrics in scoped.items():
            result = self._data.read(
                ops.FIRST_CHANGE,
                FirstChangeInput(
                    metrics=metrics,
                    segment=dict(scope),
                    start=window.lookback,
                    end=window.end,
                ),
            )
            changes.update(result.changes)
        return changes

    def _symptoms(
        self, anomaly: Anomaly, window: _Window, cause: Hypothesis | None
    ) -> list[Symptom]:
        if cause is None:
            return []
        catalog = self._metrics()
        names = frontier = {cause.root_node}
        edges = self._graph.view().edges
        while frontier:
            frontier = {
                edge.dst
                for edge in edges
                if edge.src in frontier
                and edge.type != "breakdown"
                and edge.status != "rejected"
            } - names
            names |= frontier
        candidates = {
            node
            for node in (names - {cause.root_node}) & catalog.keys()
            if catalog[node].status == "active"
        }
        measured = set(candidates)
        if (
            cause.first_change_ts is None
            and cause.root_node in catalog
            and catalog[cause.root_node].status == "active"
        ):
            measured.add(cause.root_node)
        changes = self._first_changes(
            {**anomaly.segment, **cause.segment}, window, measured, catalog
        )
        cause_ts = cause.first_change_ts or changes.get(cause.root_node)
        if cause_ts is None:
            return []
        return sorted(
            [
                Symptom(node=node, first_change_ts=ts)
                for node in candidates
                if (ts := changes.get(node)) is not None and ts > cause_ts
            ],
            key=lambda symptom: (symptom.first_change_ts, symptom.node),
        )

    def _detect_loop(
        self,
        anomaly: Anomaly,
        window: _Window,
        nodes: Iterable[str],
        hypotheses: list[Hypothesis],
    ) -> tuple[list[str], str, datetime] | None:
        catalog = self._metrics()
        cycles = {
            tuple(cycle)
            for node in dict.fromkeys(nodes)
            if node in catalog
            for cycle in self._graph.cycles_through(node)
        }
        active = {
            node
            for cycle in cycles
            for node in cycle
            if catalog[node].status == "active"
        }
        changes = {
            node: ts
            for node, ts in self._first_changes(
                anomaly.segment, window, active, catalog
            ).items()
            if ts is not None
        }
        corroborated = {
            hyp.root_node
            for hyp in hypotheses
            if hyp.status != "refuted"
            and any(e.group in {"change_log", "external"} for e in hyp.evidence_for)
        }
        candidates = []
        for cycle in sorted(cycles):
            changed = set(cycle) & changes.keys()
            if len(changed) >= 2:
                trigger = min(
                    changed,
                    key=lambda node: (node not in corroborated, changes[node], node),
                )
                candidates.append(
                    (
                        trigger not in corroborated,
                        changes[trigger],
                        len(cycle),
                        cycle,
                        trigger,
                    )
                )
        if not candidates:
            return None
        _, changed_at, _, cycle, trigger = min(candidates)
        return list(cycle), trigger, changed_at

    async def investigate(
        self,
        anomaly: Anomaly,
        on_step: Callable[[TrailStep], Awaitable[None]],
        *,
        investigation_id: str | None = None,
        open_incidents: Iterable[Incident | IncidentV2] = (),
        budget: Budget | None = None,
        scope: RunScope | None = None,
        on_usage: Callable[[RunUsageSummary], None] | None = None,
    ) -> InvestigationResult:
        investigation_id = investigation_id or uuid4().hex
        window = _Window.from_anomaly(anomaly)
        common = _WindowTools(self._data, window)
        board: list[Hypothesis] = []
        reviews: dict[str, SkepticResult] = {}
        visited = {anomaly.metric}
        trail_n = turns = 0
        evidence_steps: dict[str, str] = {}
        references = count(1)

        async def emit(step: TrailStep) -> None:
            nonlocal trail_n, turns
            trail_n += 1
            turns += int(step.actor == "detective" and step.action == "llm_call")
            if (
                step.actor == "detective"
                and step.action == "tool_call"
                and step.output is not None
            ):
                reference = step.output_summary.partition(": ")[0]
                if reference.startswith("ref-"):
                    evidence_steps[reference] = step.id
            await on_step(step.model_copy(update={"n": trail_n}))

        def tool(
            name: str,
            description: str,
            input_model: type[BaseModel],
            fn: Callable[..., Any],
        ) -> Tool[_Deps]:
            async def invoke(ctx: RunContext[_Deps], **kwargs: Any) -> BaseModel:
                try:
                    args = input_model.model_validate(kwargs)
                    if not isinstance(
                        self._data, LocalQueries
                    ) and not asyncio.iscoroutinefunction(fn):
                        result = await asyncio.to_thread(fn, args)
                    else:
                        result = fn(args)
                    output = await result if isawaitable(result) else result
                except (ValueError, KeyError) as exc:
                    raise ModelRetry(str(exc)) from exc
                if name != "update_hypotheses":
                    reference = f"ref-{next(references)}"
                    output = output.model_copy(
                        update={"summary": f"{reference}: {output.summary}"[:300]}
                    )
                await ctx.deps.emit(
                    TrailStep(
                        id=uuid4().hex,
                        investigation_id=ctx.deps.investigation_id,
                        n=0,
                        real_ts=datetime.now(UTC),
                        actor="detective",
                        action="tool_call",
                        tool=name,
                        input=args.model_dump(mode="json"),
                        output=output.model_dump(mode="json"),
                        output_summary=output.summary,
                    )
                )
                return output

            return Tool.from_schema(
                invoke,
                name=name,
                description=description,
                json_schema=input_model.model_json_schema(),
                takes_ctx=True,
                sequential=True,
            )

        async def record(
            action: Literal["note", "a2a_call", "decision"],
            summary: str,
            *,
            tool: str | None = None,
            input_: BaseModel | None = None,
            output: BaseModel | None = None,
        ) -> str:
            step_id = uuid4().hex
            await emit(
                TrailStep(
                    id=step_id,
                    investigation_id=investigation_id,
                    n=0,
                    real_ts=datetime.now(UTC),
                    actor="detective",
                    action=action,
                    tool=tool,
                    input=input_.model_dump(mode="json") if input_ else {},
                    output=output.model_dump(mode="json") if output else None,
                    output_summary=summary[:500],
                )
            )
            return step_id

        def get_graph(args: _GraphInput) -> _GraphOutput:
            node = args.node or anomaly.metric
            if not 0 <= args.depth <= 6:
                raise ValueError("depth must be between 0 and 6")
            graph = _upstream(self._graph.view(node, args.depth), node, args.depth)
            visited.add(node)
            return _GraphOutput(
                **graph.model_dump(),
                cycles=self._graph.cycles_through(node),
                summary=f"Upstream graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges.",
            )

        def drill(args: _DrillInput) -> DrillDownOutput:
            return self._data.read(
                ops.DRILL,
                DrillDownInput(**args.model_dump(), **window.bounds()),
            )

        def formula(args: _MetricInput) -> AttributionResult:
            return self._data.read(
                ops.DECOMPOSE,
                CompareWindowInput(**args.model_dump(), **window.bounds()),
            )

        def mix(args: _DrillInput) -> AttributionResult:
            return self._data.read(
                ops.MIX_RATE, DrillDownInput(**args.model_dump(), **window.bounds())
            )

        def query(args: _QueryInput) -> _SeriesOutput:
            series = self._data.read(
                ops.SERIES, ops.SeriesInput(**args.model_dump(), **window.bounds())
            )
            return _SeriesOutput(
                series=series,
                summary=f"{args.metric}: {len(series)} series, {sum(len(s.points) for s in series)} points.",
            )

        def change(args: _ChangeInput) -> FirstChangeOutput:
            result = self._data.read(
                ops.FIRST_CHANGE,
                FirstChangeInput(
                    metrics=args.metrics,
                    segment=args.segment,
                    **window.bounded(args.start, None),
                ),
            )
            visited.update(args.metrics)
            return result

        async def ask_specialist(args: _SpecialistInput) -> _SpecialistOutput:
            request = SpecialistRequest(
                investigation_id=investigation_id,
                question=args.question,
                metric=args.metric,
                segment=args.segment,
                window_start_ts=window.start,
                window_end_ts=window.end,
                baseline_start_ts=window.start - _WEEK,
                baseline_end_ts=window.end - _WEEK,
                context=f"Entry metric: {anomaly.metric}; focus: {json.dumps(anomaly.segment, sort_keys=True)}.",
            )
            response = (
                await self._a2a.ask(args.agent, request)
                if self._a2a is not None
                else SpecialistResponse(agent=args.agent, answered=False, findings=[])
            )
            summary = f"{args.agent}: {len(response.findings)} findings."
            if not response.answered:
                summary = f"{args.agent}: agent unavailable (0 findings)."
                await record("note", summary, tool="ask_specialist")
            # Protobuf Struct does not preserve map key order; replay keys do.
            result = _SpecialistOutput.model_validate_json(
                json.dumps(
                    {**response.model_dump(mode="json"), "summary": summary},
                    sort_keys=True,
                )
            )
            await record(
                "a2a_call", summary, tool=args.agent, input_=args, output=result
            )
            return result

        async def ask_skeptic(args: _SkepticInput) -> _SkepticOutput:
            hypothesis = next(
                (hyp for hyp in board if hyp.id == args.hypothesis_id), None
            )
            if hypothesis is None:
                raise ValueError("Unknown hypothesis; call update_hypotheses first")
            if self._review is None:
                raise ValueError("Skeptic is unavailable")
            review = await self._review(
                anomaly, investigation_id, emit, hypothesis=hypothesis
            )
            reviews[hypothesis.id] = review
            return _SkepticOutput.model_validate_json(
                json.dumps(
                    {
                        **review.model_dump(mode="json"),
                        "summary": f"Skeptic: {review.verdict}, {review.explained_share:.0%} explained.",
                    },
                    sort_keys=True,
                )
            )

        def update_board(args: _BoardInput) -> _BoardOutput:
            if len({hyp.id for hyp in args.hypotheses}) != len(args.hypotheses):
                raise ValueError("Hypothesis IDs must be unique")
            board[:] = _BoardInput.model_validate_json(
                json.dumps(args.model_dump(mode="json"), sort_keys=True)
            ).hypotheses
            return _BoardOutput(
                hypotheses=board,
                steps_used=turns,
                summary=f"{len(board)} hypotheses after {turns} Detective turns.",
            )

        graph = await asyncio.to_thread(get_graph, _GraphInput())
        cards = await self._a2a.fetch_cards() if self._a2a is not None else {}
        active_incidents = [
            incident
            for incident in open_incidents
            if incident.status in {"investigating", "open", "persisting", "mitigating"}
        ]
        payload = {
            "entry": anomaly.model_dump(mode="json"),
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "evidence_start": window.lookback.isoformat(),
            "upstream": graph.model_dump(mode="json"),
            "open_incidents": [
                {
                    "id": incident.id,
                    "title": incident.title,
                    "root_cause": incident.root_cause.model_dump(mode="json")
                    if incident.root_cause
                    else None,
                }
                for incident in active_incidents
            ],
            "agents": {
                name: card.skills for name, card in cards.items() if card.healthy
            },
            "thresholds": self._settings.model_dump(),
        }
        stop: Literal["confirmed", "budget", "innocent", "data_issue", "error"]
        try:
            agent = Agent(
                self._model or build_model(self._llm, "detective"),
                name="detective",
                system_prompt=_PROMPT.read_text(),
                deps_type=_Deps,
                output_type=end_state_outputs(_Submission),
                capabilities=[_Trail()],
                tools=[
                    tool(
                        "baseline_compare",
                        "Measure against the seasonal baseline.",
                        _MetricInput,
                        common.baseline,
                    ),
                    tool(
                        "drill_down",
                        "Rank segments by contribution to the delta.",
                        _DrillInput,
                        drill,
                    ),
                    tool(
                        "decompose",
                        "Attribute the delta to formula components.",
                        _MetricInput,
                        formula,
                    ),
                    tool(
                        "mix_rate_split",
                        "Separate denominator mix shifts from rate changes.",
                        _DrillInput,
                        mix,
                    ),
                    tool(
                        "compare_segments",
                        "Compare a focus segment with a control.",
                        _ControlInput,
                        common.controls,
                    ),
                    tool(
                        "first_change",
                        "Find the first two consecutive anomalous hours per metric.",
                        _ChangeInput,
                        change,
                    ),
                    tool(
                        "query_metric",
                        "Read hourly series in the fixed current window.",
                        _QueryInput,
                        query,
                    ),
                    tool(
                        "list_events",
                        "Find change-log events and external status in the evidence window.",
                        _EventsInput,
                        common.events,
                    ),
                    tool(
                        "check_freshness",
                        "Check pipeline completeness in the fixed current window.",
                        _MetricInput,
                        common.freshness,
                    ),
                    tool(
                        "get_graph",
                        "Read incoming edges and cycles through the node.",
                        _GraphInput,
                        get_graph,
                    ),
                    tool(
                        "ask_specialist",
                        "Ask a matching A2A specialist one precise question.",
                        _SpecialistInput,
                        ask_specialist,
                    ),
                    tool(
                        "ask_skeptic",
                        "Have the Skeptic challenge a hypothesis on the current board.",
                        _SkepticInput,
                        ask_skeptic,
                    ),
                    tool(
                        "update_hypotheses",
                        "Replace the investigation hypothesis board.",
                        _BoardInput,
                        update_board,
                    ),
                ],
                model_settings={
                    "max_tokens": self._llm.max_tokens,
                    **(
                        {
                            "anthropic_thinking": {"type": "adaptive"},
                        }
                        if self._llm.provider == "anthropic"
                        else {}
                    ),
                },
            )
            outcome = await run_agent(
                agent,
                json.dumps(payload, sort_keys=True),
                scope=scope or RunScope(run_id=investigation_id, tenant_id="default"),
                limits=RunLimits(max_steps=self._settings.max_steps),
                prices=price_table(self._llm),
                budget=budget,
                deps=_Deps(investigation_id, emit),
                kill_switch=self._kill_switch,
            )
            if on_usage is not None:
                on_usage(outcome.result.usage)
            if outcome.result.status != "done":
                reason = (
                    outcome.result.reason
                    or outcome.result.question
                    or outcome.result.status
                )
                if outcome.result.status == "gave_up":
                    raise BudgetExceeded(reason)
                raise LLMUnavailable(reason)
            answer = cast(_Submission, outcome.result.result)
            await emit(
                TrailStep(
                    id=uuid4().hex,
                    investigation_id=investigation_id,
                    n=0,
                    real_ts=datetime.now(UTC),
                    actor="detective",
                    action="tool_call",
                    tool="submit",
                    input=answer.model_dump(mode="json"),
                    output=answer.model_dump(mode="json"),
                    output_summary=answer.summary[:500],
                )
            )
        except (BudgetExceeded, LLMUnavailable, KeyError, UserError) as exc:
            stop = "budget" if isinstance(exc, BudgetExceeded) else "error"
            answer = _Submission(
                verdict="HYPOTHESIS",
                summary="Investigation stopped before reaching a supported verdict.",
                root_cause=None,
                hypotheses=board,
                contributions=[],
                open_questions=[f"Investigation stopped: {exc}"],
                suggested_actions=[],
            )
        else:
            combined = {hyp.id: hyp for hyp in board}
            combined.update((hyp.id, hyp) for hyp in answer.hypotheses)
            if answer.root_cause is not None:
                combined[answer.root_cause.id] = answer.root_cause
            answer.hypotheses = list(combined.values())
            innocent = any(review.verdict == "innocent" for review in reviews.values())
            stale = (
                answer.verdict == "DATA_ISSUE"
                and (
                    await asyncio.to_thread(
                        common.freshness,
                        _MetricInput(metric=anomaly.metric, segment=anomaly.segment),
                    )
                ).verdict
                != "ok"
            )
            failures = _stop_failures(
                answer, self._settings, innocent=innocent, stale=stale
            )
            if failures:
                answer.verdict = "HYPOTHESIS"
                answer.summary = f"Unconfirmed: {answer.summary}"
                answer.open_questions.extend(failures)
            stop = {
                "CONFIRMED": "confirmed",
                "MIXED": "confirmed",
                "INNOCENT": "innocent",
                "DATA_ISSUE": "data_issue",
                "HYPOTHESIS": "error",
            }[answer.verdict]

        for hypothesis in answer.hypotheses:
            for evidence in [*hypothesis.evidence_for, *hypothesis.evidence_against]:
                evidence.step_id = evidence_steps.get(evidence.step_id, "")

        if answer.trigger is not None:
            candidates = [h for h in answer.hypotheses if h.status != "refuted"]
            if not any(h.id == answer.trigger for h in candidates):
                matches = [h.id for h in candidates if h.root_node == answer.trigger]
                answer.trigger = matches[0] if len(matches) == 1 else None

        if not answer.loop or not answer.trigger:
            nodes = [
                anomaly.metric,
                *sorted(visited),
                *(hyp.root_node for hyp in answer.hypotheses),
            ]
            if answer.root_cause is not None:
                nodes.append(answer.root_cause.root_node)
            if inferred := await asyncio.to_thread(
                self._detect_loop,
                anomaly,
                window,
                answer.loop or nodes,
                answer.hypotheses,
            ):
                loop, node, changed_at = inferred
                if not answer.loop:
                    answer.loop = loop
                step_id = await record(
                    "note",
                    f"Feedback cycle: {' → '.join(loop)}; trigger candidate: {node} at {changed_at.isoformat()}.",
                    tool="first_change",
                )
                if not answer.trigger and node in answer.loop:
                    trigger = next(
                        (
                            h.id
                            for h in [*answer.hypotheses, answer.root_cause]
                            if h is not None
                            and h.root_node == node
                            and h.status != "refuted"
                        ),
                        None,
                    )
                    if trigger is None:
                        trigger = f"trigger-{uuid4().hex}"
                        metric = self._metrics()[node]
                        answer.hypotheses.append(
                            Hypothesis(
                                id=trigger,
                                statement=f"{node} is a candidate trigger based on change order; cause unverified.",
                                cause_type="unknown",
                                root_node=node,
                                segment={
                                    dim: value
                                    for dim, value in anomaly.segment.items()
                                    if dim in metric.dimensions
                                },
                                first_change_ts=changed_at,
                                explained_share=0,
                                status="open",
                                evidence_for=[
                                    Evidence(
                                        group="data",
                                        source="first_change",
                                        step_id=step_id,
                                        summary=f"{node} first changed at {changed_at.isoformat()}.",
                                    )
                                ],
                            )
                        )
                    answer.trigger = trigger

        answer.contributions = _contributions(answer.contributions, anomaly.delta)
        _publish_hypothesis_ids(
            answer,
            investigation_id,
            {
                incident.root_cause.id: incident.root_cause
                for incident in active_incidents
                if incident.root_cause is not None
            },
        )
        cause = answer.root_cause or next(
            (hyp for hyp in answer.hypotheses if hyp.id == answer.trigger), None
        )
        symptoms = await asyncio.to_thread(self._symptoms, anomaly, window, cause)
        if symptoms:
            await record(
                "note",
                "Downstream symptoms: " + ", ".join(item.node for item in symptoms),
                tool="first_change",
            )
        result = InvestigationResult(
            **answer.model_dump(),
            symptoms=symptoms,
            investigation_id=investigation_id,
            entry=anomaly,
            steps_used=turns,
            stop_reason=stop,
        )
        await record("decision", f"{result.verdict}: {result.summary}", output=result)
        return result
