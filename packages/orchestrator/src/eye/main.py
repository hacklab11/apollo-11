"""Build and run the core service."""

import json
import logging
import os
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import uvicorn
from anthropic import AnthropicError
from fastapi import FastAPI

from eye.a2a_client import A2AClient
from eye.agents.reporter import Reporter
from eye.agents.sentinel import Sentinel
from eye.agents.skeptic import Skeptic
from eye.api.app import create_app
from eye.config import Settings, load_settings
from eye.contracts import InvestigationResult, TrailStep
from eye.datasource import ServiceClock, open_source
from eye.incidents.store import IncidentStore
from eye.llm.client import (
    BudgetExceeded,
    LLMClient,
    MissingProviderCredentials,
    RunCallLimitExceeded,
    configure_client,
)
from eye.llm.runtime import AgentRefused
from eye.orchestrator import HOUR, Orchestrator, StepSink
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.tools.models import BaselineCompareInput
from eye.warehouse import operations as ops
from eye.warehouse.client import DataClient, RemoteGraph
from eye.warehouse.queries import LocalQueries

if TYPE_CHECKING:
    from eye.agents.cartographer import Cartographer


async def _refine_causal_seeds(
    cart: "Cartographer", clean_hours: Sequence[datetime], on_step: StepSink
) -> None:
    graph = cart.graph
    seeds = [edge for edge in graph.edges() if edge.created_by == "seed"]
    if any(not edge.evidence.get("llm_refined") for edge in seeds):
        n = 0

        async def record_step(step: TrailStep) -> None:
            nonlocal n
            n = step.n
            await on_step(step)

        try:
            await cart.refine_causal_seeds(clean_hours, record_step)
        except (AnthropicError, BudgetExceeded, AgentRefused) as exc:
            missing = (
                exc.env_var if isinstance(exc, MissingProviderCredentials) else None
            )
            detail = f"set {missing}" if missing else type(exc).__name__
            print(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "level": "warning",
                        "component": "cartographer",
                        "event": "refinement_unavailable",
                        "error": type(exc).__name__,
                        **({"missing_env": missing} if missing else {}),
                    }
                ),
                flush=True,
            )
            await on_step(
                TrailStep(
                    id=f"stp_{uuid4().hex}",
                    investigation_id="cartographer:bootstrap",
                    n=n + 1,
                    real_ts=datetime.now(UTC),
                    actor="cartographer",
                    action="note",
                    output_summary=(
                        f"Seed refinement unavailable: {detail}; "
                        "seeds unchanged; retry on next startup"
                    ),
                )
            )
            return
        for edge in graph.edges():
            if edge.created_by == "seed":
                graph.set_edge_status(edge.id, edge.status, {"llm_refined": True})


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
            except (AnthropicError, RunCallLimitExceeded) as exc:
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


@contextmanager
def core_app(settings: Settings, *, data_url: str | None = None) -> Iterator[FastAPI]:
    detective = _optional_module("eye.agents.detective")
    cartographer = _optional_module("eye.agents.cartographer")
    llm = LLMClient(settings.llm)
    configure_client(llm)

    with ExitStack() as stack:
        client = (
            stack.enter_context(closing(DataClient(data_url))) if data_url else None
        )
        local = stack.enter_context(open_source(settings)) if client is None else None
        source = ServiceClock(client) if client is not None else local
        assert source is not None
        incidents = stack.enter_context(
            closing(
                IncidentStore(
                    path_exists=lambda src, dst: graph.path_exists(src, dst),
                    notify=lambda alert: a2a.notify_seller_os(alert),
                    settings=settings.reporter,
                )
            )
        )
        if client is not None:
            connection = registry = engine = None
            graph = RemoteGraph(client)
            queries = client
            cart = None
        else:
            assert local is not None
            bootstrap = import_module("eye.graph.bootstrap")
            connection = stack.enter_context(local.cursor())
            registry = load_registry(connection)
            engine = MetricEngine(connection, registry)
            graph = bootstrap.load_or_bootstrap(
                engine, excluded_hours=sorted(incidents.excluded_hours(source.now))
            )
            queries = LocalQueries(
                connection, registry, graph, settings.sentinel, engine
            )
            cart = (
                cartographer.Cartographer(engine, graph, llm=settings.llm)
                if cartographer is not None
                else None
            )

        async def refine(clean_hours: Sequence[datetime], on_step: StepSink) -> None:
            assert cart is not None
            await _refine_causal_seeds(cart, clean_hours, on_step)

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
                data=queries,
                review=Skeptic(
                    data=queries, freshness=settings.sentinel, llm=settings.llm
                ).review,
            ).investigate
            if detective is not None
            else None
        )

        def z_lookup(
            name: str, segment: dict[str, str], ts: datetime
        ) -> tuple[float, float]:
            reading = queries.read(
                ops.BASELINE,
                BaselineCompareInput(
                    name=name, start=ts, end=ts + HOUR, segment=segment
                ),
            ).points[0]
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
                data=queries,
            ),
            incidents,
            llm,
            z_lookup,
            a2a=a2a,
            investigate=investigate,
            add_metric=cart.add_metric if cart else None,
            refine=refine if cart else None,
            **({"invalidate": engine.invalidate} if engine is not None else {}),
        )
        incidents.reporter = _reporter(llm, settings, orchestrator).write
        if local is not None:
            graph.on_change(lambda view: orchestrator.emit("graph_changed", view))
            stack.callback(graph.save)
        yield create_app(orchestrator, connection, registry, graph, engine, data=client)


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )
    settings = load_settings()
    with core_app(settings, data_url=os.environ.get("EYE_DATA_URL")) as app:
        uvicorn.run(
            app,
            host=os.environ.get("EYE_BIND_HOST", "127.0.0.1"),
            port=settings.ports.core,
            access_log=False,
            log_config=None,
        )
