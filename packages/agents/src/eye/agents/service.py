"""Run one existing agent per process with its own A2A card and endpoint."""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

import duckdb
import httpx
import uvicorn
from agent_runtime import Budget, RunScope
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_runtime import Budget, RunScope
from eye.agents.a2a_service import Run, Skill, SkillExecutor, agent_card, send_skill
from eye.config import Settings, load_settings
from eye.contracts import AddMetricResult, Anomaly, Hypothesis, TrailStep
from eye.contracts_v2 import (
    ChallengeRequest,
    ChallengeResult,
    DetectRequest,
    DetectResult,
    InvestigateRequest,
    InvestigateResult,
    MapRequest,
    ReportRequest,
    ReportResult,
    SkepticResult,
)
from eye.graph.store import GraphStore
from eye.llm.client import LLMClient, configure_client
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.warehouse.client import DataClient, RemoteGraph
from eye.warehouse.landing import ParquetLanding

PORTS = {
    "sentinel": 8210,
    "detective": 8220,
    "skeptic": 8230,
    "reporter": 8240,
    "cartographer": 8250,
}


def _detect(settings: Settings, data: DataClient) -> Skill[DetectRequest, DetectResult]:
    from eye.agents.sentinel import Sentinel

    sentinel = Sentinel(
        settings=settings.sentinel, llm=settings.llm, data=data, graph=RemoteGraph(data)
    )

    async def detect(request: DetectRequest, run: Run) -> DetectResult:
        decisions = await sentinel.run(
            request.ts,
            open_incidents=request.open_incidents,
            on_step=run.record,
            previous_decisions=request.previous_decisions,
        )
        return DetectResult(decisions=decisions, trail=run.trail)

    return Skill(
        "detect",
        "Scan a completed hour for anomalies.",
        DetectRequest,
        DetectResult,
        detect,
    )


def _investigate(
    settings: Settings, data: DataClient, http: httpx.AsyncClient, skeptic_url: str
) -> Skill[InvestigateRequest, InvestigateResult]:
    from eye.agents.detective import Detective

    graph = RemoteGraph(data)

    async def investigate(request: InvestigateRequest, run: Run) -> InvestigateResult:
        delegation = asyncio.Lock()

        async def review(
            anomaly: Anomaly,
            investigation_id: str,
            on_step: Callable[[TrailStep], Awaitable[None]],
            *,
            hypothesis: Hypothesis | None = None,
        ) -> SkepticResult:
            # Parallel tool calls must not delegate the same remaining budget twice.
            async with delegation:
                response = await send_skill(
                    http,
                    skeptic_url,
                    "challenge",
                    ChallengeRequest(
                        anomaly=anomaly,
                        investigation_id=investigation_id,
                        hypothesis=hypothesis,
                    ),
                    run.delegate(),
                    ChallengeResult,
                )
                run.add_usage(response.usage)
                run.check_tokens()
                if response.status != "done" or response.result is None:
                    raise ValueError("Skeptic did not complete the challenge")
                for step in response.result.trail:
                    await on_step(step)
                return response.result.review

        detective = Detective(
            None,
            None,
            graph,
            settings=settings.detective,
            freshness=settings.sentinel,
            llm=settings.llm,
            data=data,
            review=review,
        )
        result = await detective.investigate(
            request.anomaly,
            run.record,
            investigation_id=request.investigation_id,
            open_incidents=request.open_incidents,
        )
        return InvestigateResult(investigation=result, trail=run.trail)

    return Skill(
        "investigate",
        "Investigate a flagged anomaly.",
        InvestigateRequest,
        InvestigateResult,
        investigate,
    )


def _challenge(
    settings: Settings, data: DataClient
) -> Skill[ChallengeRequest, ChallengeResult]:
    from eye.agents.skeptic import Skeptic

    skeptic = Skeptic(data=data, freshness=settings.sentinel, llm=settings.llm)

    async def challenge(request: ChallengeRequest, run: Run) -> ChallengeResult:
        review = await skeptic.review(
            request.anomaly,
            request.investigation_id,
            run.record,
            hypothesis=request.hypothesis,
        )
        return ChallengeResult(review=review, trail=run.trail)

    return Skill(
        "challenge",
        "Challenge a proposed explanation for an anomaly.",
        ChallengeRequest,
        ChallengeResult,
        challenge,
    )


def _report(settings: Settings) -> Skill[ReportRequest, ReportResult]:
    from eye.agents.reporter import Reporter

    async def report(request: ReportRequest, run: Run) -> ReportResult:
        reporter = Reporter(settings=settings.llm)
        narrative = await reporter.write(
            request.investigation,
            scope=RunScope(
                run_id=run.metadata.context_id, tenant_id=run.metadata.tenant_id
            ),
            budget=Budget.model_validate(run.metadata.budget.model_dump()),
        )
        run.add_usage(narrative.usage)
        run.check_tokens()
        return ReportResult(title=narrative.title, summary=narrative.summary)

    return Skill(
        "report",
        "Write a grounded incident summary.",
        ReportRequest,
        ReportResult,
        report,
    )


class _Cartography:
    def __init__(self, lake: Path, graph: RemoteGraph) -> None:
        self.connection = duckdb.connect(config={"threads": 2})
        self.landing = ParquetLanding(lake, self.connection)
        self.graph = graph
        self.lock = Lock()

    def prepare(self) -> tuple[MetricEngine, GraphStore]:
        with self.lock:
            self.landing.refresh()
            registry = load_registry(self.connection)
        graph = GraphStore(path=None)
        for metric in registry.metrics.values():
            graph.add_metric(metric)
        for edge in self.graph.view().edges:
            if edge.src in registry.metrics and edge.dst in registry.metrics:
                graph.add_edge(edge)
        return MetricEngine(self.connection, registry), graph


def _map(
    settings: Settings, warehouse: _Cartography
) -> Skill[MapRequest, AddMetricResult]:
    from eye.agents.cartographer import Cartographer

    lock = asyncio.Lock()

    async def map_metric(request: MapRequest, run: Run) -> AddMetricResult:
        # Legacy validation stages contributor SQL and graph edits on a private snapshot.
        async with lock:
            engine, graph = await asyncio.to_thread(warehouse.prepare)
            cartographer = Cartographer(
                engine,
                graph,
                llm=settings.llm,
                budget=Budget.model_validate(run.metadata.budget.model_dump()),
                scope=RunScope(
                    run_id=run.metadata.context_id, tenant_id=run.metadata.tenant_id
                ),
            )
            try:
                return await cartographer.add_metric(request.metric, on_step=run.record)
            finally:
                run.usage = cartographer.usage

    return Skill(
        "map",
        "Validate and propose a metric graph extension.",
        MapRequest,
        AddMetricResult,
        map_metric,
    )


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(
    name: str,
    url: str,
    settings: Settings,
    *,
    data: DataClient | None = None,
    http: httpx.AsyncClient | None = None,
    lake: Path | None = None,
) -> Starlette:
    if name not in PORTS:
        raise ValueError(f"Unknown agent: {name}")
    owned_data = data is None and name != "reporter"
    if owned_data:
        data = DataClient(os.environ.get("EYE_DATA_URL", "http://127.0.0.1:8150"))
    owned_http = http is None and name == "detective"
    if owned_http:
        http = httpx.AsyncClient(timeout=settings.a2a.timeout_s, trust_env=False)
    warehouse = None
    match name:
        case "reporter":
            skill = _report(settings)
        case "sentinel":
            assert data is not None
            skill = _detect(settings, data)
        case "detective":
            assert data is not None and http is not None
            skeptic_url = os.environ.get("EYE_SKEPTIC_URL", "http://127.0.0.1:8230/")
            skill = _investigate(settings, data, http, skeptic_url)
        case "skeptic":
            assert data is not None
            skill = _challenge(settings, data)
        case "cartographer":
            assert data is not None
            warehouse = _Cartography(
                lake or Path(os.environ.get("SIM_LAKE_DIR", "data/lake")),
                RemoteGraph(data),
            )
            skill = _map(settings, warehouse)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if warehouse is not None:
                with warehouse.lock:
                    warehouse.connection.close()
            if owned_data and data is not None:
                data.close()
            if owned_http and http is not None:
                await http.aclose()

    card = agent_card(name, url, skill)
    handler = DefaultRequestHandler(
        SkillExecutor(name, skill), InMemoryTaskStore(), card
    )
    return Starlette(
        routes=[
            Route("/healthz", _health),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", choices=PORTS)
    name = parser.parse_args().agent
    port = PORTS[name]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonLogFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    settings = load_settings()
    configure_client(LLMClient(settings.llm))
    uvicorn.run(
        create_app(
            name, os.environ.get("EYE_AGENT_URL", f"http://127.0.0.1:{port}/"), settings
        ),
        host=os.environ.get("EYE_BIND_HOST", "127.0.0.1"),
        port=port,
        access_log=False,
        log_config=None,
    )


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )
