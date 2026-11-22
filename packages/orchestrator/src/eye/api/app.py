"""Public HTTP API and static application."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Protocol

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError

from eye.api import chat
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Dimension,
    Edge,
    GraphView,
    Incident,
    IncidentStatus,
    MetricDef,
    SellerAlert,
    SentinelDecision,
    Series,
    SeriesPoint,
    TrailStep,
)
from eye.contracts_v2 import (
    DEFAULT_TENANT,
    SELLER_REVERT_TOOL,
    IncidentCloseRequest,
    IncidentClosure,
    IncidentV2,
    SellerPriceRevert,
    SellerRevertApproval,
)
from eye.control import RunKilled
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
_BEARER = HTTPBearer(auto_error=False)
Bearer = Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


def human_actor(request: Request) -> str:
    actor = getattr(request.state, "person", None)
    if actor is None:
        actor = request.headers.get("X-Eye-Person")
    if not isinstance(actor, str) or not actor.strip():
        raise HTTPException(401, "authenticated person required")
    return actor


def has_seller_revert_token(
    credentials: HTTPAuthorizationCredentials | None, *, role: str
) -> bool:
    expected = os.environ.get(f"EYE_SELLER_REVERT_{role}_TOKEN")
    other_role = "OPERATOR" if role == "SERVICE" else "SERVICE"
    return bool(
        expected
        and expected != os.environ.get(f"EYE_SELLER_REVERT_{other_role}_TOKEN")
        and credentials
        and compare_digest(credentials.credentials.encode(), expected.encode())
    )


def seller_revert_actor(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str | None:
    actor = getattr(request.state, "person", None)
    if isinstance(actor, str) and actor.strip():
        return actor
    if has_seller_revert_token(credentials, role="OPERATOR"):
        return "seller-revert-operator"
    return None


def kill_actor(request: Request, credentials: Bearer) -> str:
    if not os.environ.get("EYE_SELLER_REVERT_SERVICE_TOKEN"):
        return human_actor(request)
    actor = seller_revert_actor(request, credentials)
    if actor is None:
        raise HTTPException(401, "Operator credential required")
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
    async def incidents(status: IncidentStatus | None = None) -> list[Incident]:
        return [
            item.model_copy(update={"root_cause": None, "hypotheses": []})
            for item in orchestrator.incidents.all()
            if item.merged_into is None and (status is None or item.status == status)
        ]

    @app.get("/api/incidents/{id}")
    async def incident(id: str) -> Incident:
        item = orchestrator.incidents.get(id)
        if item is None:
            raise HTTPException(404, "unknown incident")
        return item

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
        orchestrator.emit("incident_upserted", orchestrator.incidents.get(id))
        return item

    @app.get("/api/audit")
    async def audit() -> list[dict]:
        return orchestrator.control.audit_rows()

    @app.get("/api/approvals")
    async def approvals(request: Request, credentials: Bearer) -> list[dict]:
        rows = orchestrator.control.approval_rows()
        if seller_revert_actor(request, credentials) is not None:
            return rows
        return [row for row in rows if row["tool_name"] != SELLER_REVERT_TOOL]

    def check_seller_revert_running(run_id: str) -> None:
        try:
            orchestrator.control.check(run_id, DEFAULT_TENANT)
        except RunKilled as exc:
            raise HTTPException(409, "SellerOS price reverts are stopped") from exc

    @app.put("/api/approvals/seller-revert")
    async def request_seller_revert(
        action: SellerPriceRevert, credentials: Bearer
    ) -> SellerRevertApproval:
        if not has_seller_revert_token(credentials, role="SERVICE"):
            raise HTTPException(401, "SellerOS service credential required")
        key = action.idempotency_key()
        run_id = f"selleros:{action.incident_id}"
        check_seller_revert_running(run_id)
        try:
            row = orchestrator.control.ensure_pending(
                key,
                run_id,
                action.incident_id,
                SELLER_REVERT_TOOL,
                action.model_dump(mode="json"),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return SellerRevertApproval(
            id=row["id"],
            tool_name=row["tool_name"],
            args=row["args"],
            status=row["status"],
            decided_by=row["decided_by"],
        )

    @app.post("/api/approvals/{id}")
    async def decide_approval(
        id: str, decision: ApprovalDecision, request: Request, credentials: Bearer
    ) -> dict:
        row = orchestrator.control.approval(id)
        seller_revert = row is not None and row["tool_name"] == SELLER_REVERT_TOOL
        if seller_revert:
            actor = seller_revert_actor(request, credentials)
            if actor is None:
                raise HTTPException(401, "SellerOS operator credential required")
        else:
            actor = human_actor(request)
        if row is None:
            raise HTTPException(409, "unknown or already decided approval")
        if decision.approve and seller_revert:
            check_seller_revert_running(row["run_id"])
        try:
            orchestrator.control.decide(id, decision.approve, actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if decision.approve and seller_revert:
            action = SellerPriceRevert.model_validate(row["args"])
            ack = await orchestrator.directory.notify_seller_os(
                SellerAlert(
                    incident_id=action.incident_id,
                    seller_ids=[action.seller_id],
                    skus=[action.sku],
                    summary="Human-approved SellerOS price revert.",
                    recommended_action="revert manual price",
                ),
                approval_id=id,
            )
            if not ack.received:
                raise HTTPException(502, "Approval recorded; SellerOS delivery failed.")
        return {"id": id, "approved": decision.approve}

    @app.post("/api/runs/{id}/kill")
    async def kill_run(id: str, actor: str = Depends(kill_actor)) -> dict:
        orchestrator.control.set_kill("run", id, True, actor)
        return {"active": True}

    @app.delete("/api/runs/{id}/kill")
    async def clear_run(id: str, actor: str = Depends(kill_actor)) -> dict:
        orchestrator.control.set_kill("run", id, False, actor)
        return {"active": False}

    @app.post("/api/tenants/{id}/kill")
    async def kill_tenant(id: str, actor: str = Depends(kill_actor)) -> dict:
        orchestrator.control.set_kill("tenant", id, True, actor)
        return {"active": True}

    @app.delete("/api/tenants/{id}/kill")
    async def clear_tenant(id: str, actor: str = Depends(kill_actor)) -> dict:
        orchestrator.control.set_kill("tenant", id, False, actor)
        return {"active": False}

    @app.post("/api/kill")
    async def kill_system(actor: str = Depends(kill_actor)) -> dict:
        orchestrator.control.set_kill("system", DEFAULT_TENANT, True, actor)
        return {"active": True}

    @app.delete("/api/kill")
    async def clear_system(actor: str = Depends(kill_actor)) -> dict:
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
