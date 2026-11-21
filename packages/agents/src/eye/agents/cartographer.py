"""Place contributor metrics using agent proposals and deterministic evidence."""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Self, cast, get_args
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from eye.config import LlmSettings
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Dimension,
    Edge,
    EdgeProposal,
    MetricDef,
    TrailStep,
)
from eye.graph.bootstrap import _mean_shares
from eye.graph.store import GraphStore
from eye.llm.runtime import AgentSpec, ToolSpec, run_agent
from eye.registry.engine import MetricEngine
from eye.registry.loader import formula_inputs
from eye.tools import sqlguard
from eye.tools.identity import parse_identity
from eye.tools.validate import (
    CorrelationResult,
    ValidationResult,
    _window,
    lagged_correlation,
    validate_breakdown,
    validate_identity,
)


class Submission(BaseModel):
    edges: list[EdgeProposal]
    notes: str
    unit: str = "count"


class SeedDecision(BaseModel):
    src: str
    dst: str
    status: str
    confidence: float
    rationale: str


class SeedSubmission(BaseModel):
    seeds: list[SeedDecision]
    edges: list[EdgeProposal]
    notes: str


class IdentityInput(BaseModel):
    identity: str


class BreakdownInput(BaseModel):
    metric: str
    dimension: Dimension


class CorrelationInput(BaseModel):
    src: str
    dst: str
    max_lag: int = 96


class QueryInput(BaseModel):
    metric: str


class GraphInput(BaseModel):
    node: str | None = None


class Output(BaseModel):
    summary: str = Field(max_length=300)
    data: dict


async def _discard(step: TrailStep) -> None:
    pass


class Cartographer:
    def __init__(
        self,
        engine: MetricEngine,
        graph: GraphStore,
        *,
        llm: LlmSettings | None = None,
        scenario_hours: list[datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.graph = graph
        self.llm = llm or LlmSettings()
        self.scenario_hours = scenario_hours
        self._allowed_tables = frozenset(
            table
            for name, tables in engine._registry.source_tables.items()
            if engine._registry.get(name).added_by == "seed"
            for table in tables
        )

    def _identity(
        self, engine: MetricEngine, text: str, *, end: datetime | None = None
    ) -> ValidationResult:
        return validate_identity(
            engine, text, end=end, scenario_hours=self.scenario_hours
        )

    def _breakdown(
        self,
        engine: MetricEngine,
        metric: str,
        dim: str,
        *,
        end: datetime | None = None,
    ) -> ValidationResult:
        return validate_breakdown(
            engine, metric, dim, end=end, scenario_hours=self.scenario_hours
        )

    def _correlation(
        self,
        engine: MetricEngine,
        src: str,
        dst: str,
        lag: int = 96,
        *,
        end: datetime | None = None,
    ) -> CorrelationResult:
        return lagged_correlation(
            engine, src, dst, lag, end=end, scenario_hours=self.scenario_hours
        )

    def _spec(self, engine: MetricEngine, *, end: datetime | None = None) -> AgentSpec:
        def query(args: QueryInput) -> Output:
            start, through = _window(engine, None, end, [args.metric])
            series = engine.query_metric(args.metric, start, through)
            return Output(
                summary=f"{len(series)} series for {args.metric}",
                data={"series": [s.model_dump(mode="json") for s in series]},
            )

        return AgentSpec(
            name="cartographer",
            system_prompt_file=str(
                files("eye.llm").joinpath("prompts/cartographer.md")
            ),
            tools=[
                ToolSpec(
                    "query_metric",
                    "Read hourly total metric history.",
                    QueryInput,
                    query,
                ),
                ToolSpec(
                    "validate_identity",
                    "Test a candidate identity at total and shared segments.",
                    IdentityInput,
                    lambda a: self._identity(engine, a.identity, end=end),
                ),
                ToolSpec(
                    "validate_breakdown",
                    "Test whether dimension segments reconcile to total.",
                    BreakdownInput,
                    lambda a: self._breakdown(engine, a.metric, a.dimension, end=end),
                ),
                ToolSpec(
                    "lagged_correlation",
                    "Measure lagged deseasonalized first-difference correlation, not causation.",
                    CorrelationInput,
                    lambda a: self._correlation(
                        engine, a.src, a.dst, a.max_lag, end=end
                    ),
                ),
                ToolSpec(
                    "get_graph",
                    "Read graph edges and nodes.",
                    GraphInput,
                    lambda a: Output(
                        summary=f"Graph has {len(self.graph.edges())} edges",
                        data=self.graph.view(a.node).model_dump(mode="json"),
                    ),
                ),
            ],
            submit_model=Submission,
            effort="high",
            model=self.llm.model_for("cartographer"),
            max_steps=8,
            max_tokens=self.llm.max_tokens,
        )

    def _edges(
        self,
        engine: MetricEngine,
        proposal: EdgeProposal,
        index: int,
        *,
        end: datetime | None = None,
    ) -> list[Edge]:
        engine._registry.get(proposal.src)
        engine._registry.get(proposal.dst)
        evidence = {"rationale": proposal.rationale}
        status = "ambiguous"
        confidence = 0.0
        pairs = [(proposal.src, proposal.dst)]
        shares: dict[str, float] = {}
        if proposal.type == "formula":
            parsed = parse_identity(proposal.identity or "")
            result = self._identity(engine, proposal.identity)
            evidence.update(result.evidence)
            residual = result.evidence["median_residual"]
            status = (
                "validated"
                if result.passed
                else "rejected"
                if residual is not None and residual > 0.05
                else "ambiguous"
            )
            if not result.passed and self._identity_total_passes(
                engine, proposal.identity
            ):
                status = "ambiguous"
            pairs = [(t.name, parsed.target) for t in parsed.terms]
            if parsed.kind == "sum":
                shares = _mean_shares(
                    engine, parsed.target, [t.name for t in parsed.terms]
                )
            if len(parsed.terms) == 1 and result.passed:
                status = "ambiguous"
                evidence["note"] = f"possible duplicate of {parsed.terms[0].name}"
            confidence = 1.0 if status == "validated" else 0.0
        elif proposal.type == "breakdown":
            if (
                proposal.src != proposal.dst
                or proposal.dimension
                not in engine._registry.get(proposal.src).dimensions
            ):
                raise ValueError("breakdowns must be self edges on declared dimensions")
            result = self._breakdown(engine, proposal.src, proposal.dimension)
            evidence.update(result.evidence)
            status = "validated" if result.passed else "rejected"
            confidence = float(result.passed)
        else:
            lag = proposal.lag_hours or 0
            if not 0 <= lag <= 96:
                raise ValueError("causal lag must be between 0 and 96 hours")
            result = self._correlation(engine, proposal.src, proposal.dst, end=end)
            evidence.update(result.model_dump(exclude={"summary"}))
            nearby = [
                abs(row["corr"])
                for row in result.evidence["lags"]
                if abs(row["lag"] - lag) <= 6
            ]
            confidence = max(nearby, default=0.0)
            formula_path = self._formula_path(proposal.src, proposal.dst)
            if confidence >= 0.3 or (
                "formula" in proposal.rationale.lower() and formula_path
            ):
                status = "hypothesis"
                confidence = max(confidence, 0.3)
        return [
            Edge(
                id=f"edg_cartographer_{index}_{src}_{dst}_{proposal.dimension or proposal.type}",
                src=src,
                dst=dst,
                type=proposal.type,
                status=status,
                identity=proposal.identity,
                dimension=proposal.dimension,
                lag_hours=proposal.lag_hours,
                confidence=confidence,
                evidence={
                    **evidence,
                    **({"mean_share": shares[src]} if src in shares else {}),
                },
                created_by="cartographer",
            )
            for src, dst in pairs
        ]

    def _identity_total_passes(self, engine: MetricEngine, identity: str) -> bool:
        return validate_identity(
            engine, identity, segments=[], scenario_hours=self.scenario_hours
        ).passed

    def _formula_path(self, src: str, dst: str) -> bool:
        pending, seen = [src], set()
        while pending:
            node = pending.pop()
            if node == dst and node != src:
                return True
            if node not in seen:
                seen.add(node)
                pending.extend(
                    e.dst
                    for e in self.graph.edges()
                    if e.src == node and e.type == "formula" and e.status == "validated"
                )
        return False

    async def add_metric(
        self,
        req: AddMetricRequest,
        clean_hours: Sequence[datetime] | None = None,
        on_step: Callable[[TrailStep], Awaitable[None]] = _discard,
    ) -> AddMetricResult:
        investigation = f"cartographer:{uuid4().hex}"
        n = 0

        async def step(label: str, detail: str) -> None:
            nonlocal n
            n += 1
            await on_step(
                TrailStep(
                    id=f"{investigation}:{n}",
                    investigation_id=investigation,
                    n=n,
                    real_ts=datetime.now(UTC),
                    actor="cartographer",
                    action="note",
                    output_summary=f"{label}: {detail}"[:500],
                )
            )

        registry = self.engine._registry
        allowed = self._allowed_tables
        tables: tuple[str, ...] = ()
        try:
            if (
                not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", req.name)
                or req.name in registry.metrics
            ):
                raise ValueError("invalid or existing metric name")
            if req.sql:
                tables = sqlguard.check(req.sql, allowed)
            else:
                names = formula_inputs(req.expression)
                if not names:
                    raise ValueError("expression must reference registered metrics")
                for name in names:
                    registry.get(name)
            await step("guard", "passed")
        except ValueError as error:
            await step("guard", str(error))
            raise
        columns = (
            sqlguard.dry_run(self.engine._con, req.sql, allowed) if req.sql else []
        )
        if req.sql and not {"ts", "num"}.issubset(columns):
            await step("dry_run", "missing ts or num")
            raise ValueError("SQL must return ts and num")
        inputs = formula_inputs(req.expression) if req.expression else []
        dims = (
            [d for d in get_args(Dimension) if d in columns]
            if req.sql
            else sorted(
                set.intersection(
                    *(set(registry.get(name).dimensions) for name in inputs)
                )
            )
        )
        metric = MetricDef(
            name=req.name,
            description=req.description,
            unit=req.unit or ("ratio" if "den" in columns else "count"),
            domain="quality",
            kind="sql" if req.sql else "formula",
            sql=req.sql.rstrip().rstrip(";") if req.sql else None,
            expression=req.expression,
            dimensions=dims,
            added_by="user",
            status="active",
        )
        staged = replace(
            registry,
            metrics={**registry.metrics, req.name: metric},
            source_tables={
                **registry.source_tables,
                req.name: tables
                if req.sql
                else tuple(
                    sorted({t for name in inputs for t in registry.source_tables[name]})
                ),
            },
            has_den={**registry.has_den, req.name: "den" in columns},
        )
        engine = MetricEngine(self.engine._con, staged)
        self._set_clean_hours(clean_hours)
        _, end = _window(engine, None, None, [req.name])
        samples = engine.query_metric(req.name, end - timedelta(days=7), end)
        if not any(p.value is not None for s in samples for p in s.points):
            await step("dry_run", "0 usable history points")
            raise ValueError("metric has no usable history")
        await step(
            "dry_run",
            f"{len(dims)} dimensions; {sum(len(s.points) for s in samples)} hourly values",
        )
        context = {
            "metric": metric.model_dump(),
            "samples": [p.model_dump(mode="json") for s in samples for p in s.points][
                -24:
            ],
            "catalog": [
                {
                    "name": m.name,
                    "unit": m.unit,
                    "description": m.description,
                    "dimensions": m.dimensions,
                }
                for m in list(registry.metrics.values())[:60]
            ],
            "identities": [i.text for i in registry.identities],
        }

        async def agent_step(record: TrailStep) -> None:
            nonlocal n
            n += 1
            await on_step(record.model_copy(update={"n": n}))

        submitted = cast(
            Submission,
            await run_agent(
                self._spec(engine),
                json.dumps(context, sort_keys=True),
                investigation,
                agent_step,
            ),
        )
        await step("proposals", f"{len(submitted.edges)} proposals")
        await step(
            "tools",
            "query_metric, validate_identity, validate_breakdown, lagged_correlation, get_graph available",
        )
        edges, notes = [], [submitted.notes]
        if req.unit is None:
            metric.unit = MetricDef.model_validate(
                {**metric.model_dump(), "unit": submitted.unit}
            ).unit
        proposals = list(submitted.edges)
        for dim in dims:
            if not any(
                p.type == "breakdown" and p.src == req.name and p.dimension == dim
                for p in proposals
            ):
                proposals.append(
                    EdgeProposal(
                        type="breakdown",
                        src=req.name,
                        dst=req.name,
                        dimension=dim,
                        rationale="Declared SQL dimension",
                    )
                )
        for index, proposal in enumerate(proposals):
            try:
                if req.name not in {proposal.src, proposal.dst}:
                    raise ValueError("proposal must include new metric")
                edges.extend(self._edges(engine, proposal, index))
            except ValueError as error:
                notes.append(str(error))
        for edge in edges:
            if "note" in edge.evidence:
                notes.append(edge.evidence["note"])
            parent_dims = set(staged.get(edge.src).dimensions) | set(
                staged.get(edge.dst).dimensions
            )
            if missing := parent_dims - set(dims):
                notes.append(f"Missing dimensions: {', '.join(sorted(missing))}")
        self._competing(edges, notes)
        await step("acceptance", f"{len(edges)} deterministically checked edges")
        if req.name in registry.metrics:
            raise ValueError("metric name was registered concurrently")
        registry.metrics[req.name] = metric
        registry.source_tables[req.name] = staged.source_tables[req.name]
        registry.has_den[req.name] = staged.has_den[req.name]
        self.engine.__init__(self.engine._con, registry)
        self.graph.add_metric(metric)
        for edge in edges:
            self.graph.add_edge(edge)
        await step(
            "persist",
            f"{req.name} active; impact weights recomputed; graph_changed emitted",
        )
        await step("return", f"{len(edges)} edges; scanned from next tick")
        return AddMetricResult(
            metric=metric,
            accepted=[e for e in edges if e.status in {"validated", "hypothesis"}],
            ambiguous=[e for e in edges if e.status == "ambiguous"],
            rejected=[e for e in edges if e.status == "rejected"],
            notes="; ".join(dict.fromkeys(notes)),
            trail_investigation_id=investigation,
        )

    @staticmethod
    def _competing(edges: list[Edge], notes: list[str]) -> None:
        causal = [
            e
            for e in edges
            if e.type == "causal_hypothesis" and e.status == "hypothesis"
        ]
        competing = {
            e.id
            for e in causal
            for other in causal
            if e.src != other.src
            and e.dst == other.dst
            and abs(e.confidence - other.confidence) <= 0.05
        }
        for edge in causal:
            if edge.id in competing:
                edge.confidence *= 0.5
                edge.evidence["note"] = "competing parents"
        if competing:
            notes.append("competing parents")

    async def refine_causal_seeds(
        self,
        clean_hours: Sequence[datetime] | None = None,
        on_step: Callable[[TrailStep], Awaitable[None]] = _discard,
    ) -> None:
        seeds = [
            e
            for e in self.graph.edges()
            if e.created_by == "seed"
            and not e.evidence.get("refined")
            and not e.evidence.get("llm_refined")
        ]
        if not seeds:
            return
        # The warehouse can advance during model calls; keep the initial window.
        _, end = _window(self.engine, None, None, list(self.engine._registry.metrics))
        self._set_clean_hours(clean_hours, end=end)
        payload = []
        for edge in seeds:
            pending = [
                name
                for name in (edge.src, edge.dst)
                if self.engine._registry.get(name).status == "pending"
            ]
            numbers = None
            if not pending:
                numbers = self._correlation(
                    self.engine, edge.src, edge.dst, end=end
                ).model_dump()
            payload.append(
                {
                    "edge": edge.model_dump(),
                    "numbers": numbers,
                    **({"pending_metrics": pending} if pending else {}),
                    "src_description": self.engine._registry.get(edge.src).description,
                    "dst_description": self.engine._registry.get(edge.dst).description,
                }
            )
        required = {(edge.src, edge.dst) for edge in seeds}

        class CompleteSeedSubmission(SeedSubmission):
            @model_validator(mode="after")
            def complete(self) -> Self:
                pairs = [(decision.src, decision.dst) for decision in self.seeds]
                supplied = set(pairs)
                errors = []
                for label, pairs_to_report in (
                    ("Missing", required - supplied),
                    ("Unexpected", supplied - required),
                    ("Duplicate", {pair for pair in pairs if pairs.count(pair) > 1}),
                ):
                    if pairs_to_report:
                        names = ", ".join(
                            f"{src} -> {dst}" for src, dst in sorted(pairs_to_report)
                        )
                        errors.append(f"{label} seed decisions: {names}")
                if errors:
                    raise ValueError("; ".join(errors))
                return self

        spec = replace(
            self._spec(self.engine, end=end), submit_model=CompleteSeedSubmission
        )
        submission = cast(
            SeedSubmission,
            await run_agent(
                spec,
                json.dumps({"mode": "refine_seeds", "seeds": payload}, sort_keys=True),
                "cartographer:bootstrap",
                on_step,
            ),
        )
        decisions = {(d.src, d.dst): d for d in submission.seeds}
        if set(decisions) != required or len(submission.seeds) != len(required):
            raise ValueError("refinement must decide every seed exactly once")
        updated = []
        for edge, data in zip(seeds, payload, strict=True):
            decision = decisions[(edge.src, edge.dst)]
            if (
                decision.status not in {"hypothesis", "ambiguous", "rejected"}
                or not 0 <= decision.confidence <= 1
            ):
                raise ValueError("invalid seed status or confidence")
            updated.append(
                edge.model_copy(
                    update={
                        "status": decision.status,
                        "confidence": decision.confidence,
                        "evidence": {
                            **edge.evidence,
                            "refined": True,
                            "rationale": decision.rationale,
                            "numbers": data["numbers"],
                        },
                    }
                )
            )
        for index, proposal in enumerate(submission.edges[:10]):
            if proposal.type != "causal_hypothesis":
                raise ValueError("bootstrap extras must be causal")
            updated.extend(self._edges(self.engine, proposal, index, end=end))
        self._competing(updated, [])
        for edge in updated:
            self.graph.add_edge(edge)

    def _set_clean_hours(
        self, clean_hours: Sequence[datetime] | None, *, end: datetime | None = None
    ) -> None:
        if clean_hours is None:
            return
        _, end = _window(self.engine, None, end, list(self.engine._registry.metrics))
        clean = set(clean_hours)
        hours = (end - timedelta(hours=i + 1) for i in range(21 * 24))
        self.scenario_hours = [hour for hour in hours if hour not in clean]
