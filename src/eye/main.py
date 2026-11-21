"""Build and run the core service."""

import json
import time
from collections.abc import Sequence
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any
from uuid import uuid4

import uvicorn
from anthropic import AnthropicError

from eye.a2a_client import A2AClient
from eye.agents.reporter import Reporter
from eye.agents.sentinel import Sentinel
from eye.api.app import create_app
from eye.config import Settings, load_settings
from eye.contracts import InvestigationResult, TrailStep
from eye.datasource import open_source
from eye.incidents.store import IncidentStore
from eye.llm.client import BudgetExceeded, LLMClient, configure_client
from eye.orchestrator import Orchestrator, StepSink
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.tools.stats import expected


def _optional_module(name: str) -> ModuleType | None:
    try:
        return import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


def _reporter(
    llm: LLMClient, settings: Settings, orchestrator: Orchestrator
) -> Reporter:
    def for_result(result: InvestigationResult) -> Reporter:
        n = max(
            (step.n for step in orchestrator.incidents.trail(result.investigation_id)),
            default=0,
        )

        async def narrative(**request: Any) -> dict:
            nonlocal n
            request["system"] = [
                {
                    "type": "text",
                    "text": request["system"],
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            schema = request["output_config"]["format"]["schema"]
            for prop in schema["properties"].values():
                prop.pop("minLength", None)
                prop.pop("maxLength", None)
            request.update(
                tools=[], betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
            started = time.monotonic()
            n += 1
            try:
                response = await llm.create(request)
            except AnthropicError as exc:
                await orchestrator.record_step(
                    TrailStep(
                        id=f"stp_{uuid4().hex}",
                        investigation_id=result.investigation_id,
                        n=n,
                        real_ts=datetime.now(UTC),
                        actor="reporter",
                        action="llm_call",
                        output_summary=f"Reporter unavailable: {type(exc).__name__}",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    ),
                    result.entry.ts,
                )
                raise RuntimeError("Reporter LLM unavailable") from exc
            await orchestrator.record_step(
                TrailStep(
                    id=f"stp_{uuid4().hex}",
                    investigation_id=result.investigation_id,
                    n=n,
                    real_ts=datetime.now(UTC),
                    actor="reporter",
                    action="llm_call",
                    output_summary=f"Reporter completion: {response.stop_reason}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                ),
                result.entry.ts,
            )
            if response.stop_reason != "end_turn":
                raise ValueError("Reporter did not finish the narrative")
            return json.loads(
                "".join(
                    block.text for block in response.content if block.type == "text"
                )
            )

        return Reporter(narrative, settings=settings.llm)

    class CoreReporter(Reporter):
        async def write(self, result: InvestigationResult) -> tuple[str, str]:
            if result.stop_reason == "error":
                return self.template(result)[0], result.summary
            try:
                return await for_result(result).write(result)
            except BudgetExceeded:
                return self.template(result)

    return CoreReporter(settings=settings.llm)


def main() -> None:
    settings = load_settings()
    bootstrap = import_module("eye.graph.bootstrap")
    detective = _optional_module("eye.agents.detective")
    cartographer = _optional_module("eye.agents.cartographer")
    llm = LLMClient(settings.llm)
    configure_client(llm)

    with ExitStack() as stack:
        source = stack.enter_context(open_source(settings))
        scenarios = source.scenarios
        connection = stack.enter_context(source.cursor())
        registry = load_registry(connection)
        engine = MetricEngine(connection, registry)
        incidents = stack.enter_context(
            closing(
                IncidentStore(
                    path_exists=lambda src, dst: graph.path_exists(src, dst),
                    notify=lambda alert: a2a.notify_seller_os(alert),
                    settings=settings.reporter,
                )
            )
        )
        graph = bootstrap.load_or_bootstrap(
            engine, scenario_hours=sorted(incidents.excluded_hours(source.now))
        )
        cart = (
            cartographer.Cartographer(engine, graph, llm=settings.llm)
            if cartographer is not None
            else None
        )

        async def refine(clean_hours: Sequence[datetime], on_step: StepSink) -> None:
            assert cart is not None
            seeds = [edge for edge in graph.edges() if edge.created_by == "seed"]
            if any(not edge.evidence.get("llm_refined") for edge in seeds):
                await cart.refine_causal_seeds(clean_hours, on_step)
                for edge in graph.edges():
                    if edge.created_by == "seed":
                        graph.set_edge_status(
                            edge.id, edge.status, {"llm_refined": True}
                        )

        def unavailable(name: str, reason: str) -> None:
            orchestrator.agent_unavailable(name, reason)

        a2a = A2AClient(settings.a2a, on_unavailable=unavailable)
        investigate = (
            detective.Detective(
                connection,
                registry,
                graph,
                a2a=a2a,
                settings=settings.detective,
                freshness=settings.sentinel,
                llm=settings.llm,
            ).investigate
            if detective is not None
            else None
        )

        def z_lookup(
            name: str, segment: dict[str, str], ts: datetime
        ) -> tuple[float, float]:
            reading = expected(name, ts, segment, con=connection, registry=registry)
            return reading.z if reading.z is not None else float(
                "inf"
            ), reading.completeness

        orchestrator = Orchestrator(
            settings,
            source,
            Sentinel(
                connection,
                registry,
                settings=settings.sentinel,
                llm=settings.llm,
                graph=graph,
            ),
            incidents,
            llm,
            z_lookup,
            scenarios=scenarios,
            scenario_titles=source.scenario_titles,
            a2a=a2a,
            investigate=investigate,
            add_metric=cart.add_metric if cart else None,
            refine=refine if cart else None,
            invalidate=engine.invalidate,
            record_scenario=source.record_scenario,
        )
        incidents.reporter = _reporter(llm, settings, orchestrator).write
        graph.on_change(lambda view: orchestrator.emit("graph_changed", view))
        stack.callback(graph.save)
        uvicorn.run(
            create_app(orchestrator, connection, registry, graph, engine),
            host="127.0.0.1",
            port=settings.ports.core,
            access_log=False,
            log_config=None,
        )
