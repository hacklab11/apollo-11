"""Public HTTP API and static application."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError

from eye.api import chat
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Dimension,
    Edge,
    GraphView,
    MetricDef,
    SentinelDecision,
    Series,
    SeriesPoint,
    TrailStep,
)
from eye.contracts_v2 import (
    DEFAULT_TENANT,
    IncidentCloseRequest,
    IncidentClosure,
    IncidentStatus,
    IncidentV2,
)
from eye.orchestrator import Orchestrator, Status, StepInProgress
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.tools.models import BaselineCompareInput, BaselineCompareOutput
from eye.warehouse import operations as ops
from eye.warehouse.client import DataClient, DataUnavailable
from eye.warehouse.domain import create_router
from eye.warehouse.queries import LocalQueries

_SEGMENT = TypeAdapter(dict[Dimension, str])
_PARAMS = TypeAdapter(dict)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


def human_actor(request: Request) -> str:
    actor = getattr(request.state, "person", None)
    if not isinstance(actor, str) or not actor.strip():
        raise HTTPException(401, "authenticated person required")
    return actor


class Graph(Protocol):
    def view(self, node: str | None = None, depth: int | None = None) -> GraphView: ...

    def metrics(self) -> list[MetricDef]: ...

    def in_edges(self, node: str) -> list[Edge]: ...

    def out_edges(self, node: str) -> list[Edge]: ...


class MetricDefinition(MetricDef):
    in_edges: list[Edge]
    out_edges: list[Edge]


def create_app(
    orchestrator: Orchestrator,
    connection: duckdb.DuckDBPyConnection | None,
    registry: Registry | None,
    graph: Graph,
    engine: MetricEngine | None,
    web_dir: Path = Path("web"),
    *,
    data: DataClient | None = None,
) -> FastAPI:
    if data is None:
        assert connection is not None and registry is not None and engine is not None
        queries = LocalQueries(connection, registry, engine=engine)
    else:
        queries = data

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await orchestrator.start()
            yield
        finally:
            await orchestrator.close()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(DataUnavailable)
    async def warehouse_unavailable(_: Request, exc: DataUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    app.include_router(
        chat.create_router(
            orchestrator.settings.llm,
            os.environ.get(
                "EYE_MCP_URL",
                f"http://127.0.0.1:{orchestrator.settings.ports.mcp}/mcp",
            ),
        )
    )

    def compare(
        name: str, start: datetime, end: datetime, segment: dict[str, str]
    ) -> BaselineCompareOutput:
        return queries.read(
            ops.BASELINE,
            BaselineCompareInput(name=name, start=start, end=end, segment=segment),
        )

    def query_metric(
        name: str, start: datetime, end: datetime, segment: dict[str, str]
    ) -> list[Series]:
        return queries.read(
            ops.SERIES,
            ops.SeriesInput(metric=name, start=start, end=end, segment=segment),
        )

    app.include_router(
        create_router(
            connection,
            query_metric=query_metric,
            baseline_compare=compare,
            read=data.domain if data is not None else None,
        )
    )

    @app.get("/api/status")
    async def status() -> Status:
        return orchestrator.status()

    @app.get("/api/graph")
    def graph_view(
        node: str | None = None, depth: Annotated[int | None, Query(ge=0)] = None
    ) -> GraphView:
        if node is not None and not any(
            metric.name == node for metric in graph.metrics()
        ):
            raise HTTPException(404, "unknown metric")
        return graph.view(node, depth)

    @app.get("/api/metrics", response_model_exclude={"__all__": {"sql"}})
    def metrics() -> list[MetricDef]:
        return graph.metrics()

    def lookup_metric(name: str) -> MetricDef:
        for metric in graph.metrics():
            if metric.name == name:
                return metric
        raise HTTPException(404, "unknown metric")

    @app.get("/api/metrics/{name}/definition")
    def definition(name: str) -> MetricDefinition:
        return MetricDefinition(
            **lookup_metric(name).model_dump(),
            in_edges=graph.in_edges(name),
            out_edges=graph.out_edges(name),
        )

    @app.get("/api/metrics/{name}")
    def series(
        name: str,
        start: AwareDatetime | None = None,
        end: AwareDatetime | None = None,
        segment: str = "{}",
    ) -> Series:
        lookup_metric(name)
        try:
            selection = _SEGMENT.validate_json(segment)
            end = end or orchestrator.sim.now
            start = start or end - timedelta(days=1)
            result = compare(name, start, end, selection)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return Series(
            metric=name,
            segment=selection,
            points=[
                SeriesPoint(
                    ts=point.ts,
                    value=point.actual,
                    expected=point.expected,
                    lower=point.lower,
                    upper=point.upper,
                    completeness=point.completeness,
                )
                for point in result.points
            ],
        )

    @app.post("/api/metrics")
    async def add_metric(request: AddMetricRequest) -> AddMetricResult:
        try:
            return await orchestrator.register_metric(request)
        except NotImplementedError as exc:
            raise HTTPException(503, "Cartographer is not available") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/anomalies")
    async def anomalies(since: AwareDatetime | None = None) -> list[SentinelDecision]:
        return [
            item
            for item in orchestrator.decisions
            if since is None or item.anomaly.ts >= since
        ]

    @app.get("/api/incidents")
    async def incidents(status: IncidentStatus | None = None) -> list[IncidentV2]:
        return [
            item.model_copy(update={"root_cause": None, "hypotheses": []})
            for raw in orchestrator.incidents.all()
            for item in [orchestrator.incidents.as_v2(raw)]
            if item.merged_into is None and (status is None or item.status == status)
        ]

    @app.get("/api/incidents/{id}")
    async def incident(id: str) -> IncidentV2:
        item = orchestrator.incidents.get(id)
        if item is None:
            raise HTTPException(404, "unknown incident")
        return orchestrator.incidents.as_v2(item)

    @app.post("/api/incidents/{id}/close")
    async def close_incident(
        id: str, request: IncidentCloseRequest, actor: str = Depends(human_actor)
    ) -> IncidentV2:
        try:
            item = orchestrator.incidents.human_close(
                id,
                IncidentClosure(
                    tenant_id=request.tenant_id,
                    closed_by=actor,
                    note=request.note,
                    closed_ts=datetime.now(UTC),
                ),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        orchestrator.control.audit(
            actor=actor,
            action="incident_closed",
            incident_id=id,
            detail={"note": request.note},
        )
        orchestrator.emit("incident_upserted", item)
        return item

    @app.get("/api/audit")
    async def audit() -> list[dict]:
        return orchestrator.control.audit_rows()

    @app.get("/api/approvals")
    async def approvals() -> list[dict]:
        return orchestrator.control.approval_rows()

    @app.post("/api/approvals/{id}")
    async def decide_approval(
        id: str, decision: ApprovalDecision, actor: str = Depends(human_actor)
    ) -> dict:
        try:
            orchestrator.control.decide(id, decision.approve, actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": id, "approved": decision.approve}

    @app.post("/api/runs/{id}/kill")
    async def kill_run(id: str, actor: str = Depends(human_actor)) -> dict:
        orchestrator.control.set_kill("run", id, True, actor)
        return {"active": True}

    @app.delete("/api/runs/{id}/kill")
    async def clear_run(id: str, actor: str = Depends(human_actor)) -> dict:
        orchestrator.control.set_kill("run", id, False, actor)
        return {"active": False}

    @app.post("/api/tenants/{id}/kill")
    async def kill_tenant(id: str, actor: str = Depends(human_actor)) -> dict:
        orchestrator.control.set_kill("tenant", id, True, actor)
        return {"active": True}

    @app.delete("/api/tenants/{id}/kill")
    async def clear_tenant(id: str, actor: str = Depends(human_actor)) -> dict:
        orchestrator.control.set_kill("tenant", id, False, actor)
        return {"active": False}

    @app.post("/api/kill")
    async def kill_system(actor: str = Depends(human_actor)) -> dict:
        orchestrator.control.set_kill("system", DEFAULT_TENANT, True, actor)
        return {"active": True}

    @app.delete("/api/kill")
    async def clear_system(actor: str = Depends(human_actor)) -> dict:
        orchestrator.control.set_kill("system", DEFAULT_TENANT, False, actor)
        return {"active": False}

    @app.get("/api/incidents/{id}/trail")
    async def trail(id: str) -> list[TrailStep]:
        item = await incident(id)
        return [
            step
            for investigation_id in item.investigation_ids
            for step in orchestrator.incidents.trail(investigation_id)
        ]

    @app.post("/api/clock/pause")
    async def pause() -> Status:
        orchestrator.pause()
        return orchestrator.status()

    @app.post("/api/clock/resume")
    async def resume() -> Status:
        orchestrator.resume()
        return orchestrator.status()

    @app.post("/api/clock/step")
    async def step(wait_for_analysis: bool = True) -> Status:
        try:
            return await orchestrator.step_once(wait_for_analysis=wait_for_analysis)
        except StepInProgress as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            orchestrator.events.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.get("/web/{asset}", include_in_schema=False)
    @app.get("/{asset}", include_in_schema=False)
    async def web_asset(asset: str) -> FileResponse:
        if asset not in {"app.js", "styles.css", "index.html"}:
            raise HTTPException(404, "not found")
        return FileResponse(web_dir / asset)

    return app
