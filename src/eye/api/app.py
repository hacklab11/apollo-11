"""Public HTTP API and static application."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, TypeAdapter, ValidationError

from eye.api.data_api import create_router
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Dimension,
    Edge,
    GraphView,
    Incident,
    IncidentStatus,
    MetricDef,
    ScenarioId,
    ScenarioState,
    SentinelDecision,
    Series,
    SeriesPoint,
    TrailStep,
)
from eye.orchestrator import Orchestrator, Status
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.tools.models import BaselineCompareInput, BaselineCompareOutput
from eye.tools.stats import baseline_compare

_SEGMENT = TypeAdapter(dict[Dimension, str])
_PARAMS = TypeAdapter(dict)


class Graph(Protocol):
    def view(self, node: str | None = None, depth: int | None = None) -> GraphView: ...

    def metrics(self) -> list[MetricDef]: ...

    def in_edges(self, node: str) -> list[Edge]: ...

    def out_edges(self, node: str) -> list[Edge]: ...


class MetricDefinition(MetricDef):
    in_edges: list[Edge]
    out_edges: list[Edge]


class TruthRow(BaseModel):
    id: str
    ts: datetime
    scenario_id: ScenarioId
    action: str
    params: dict


def create_app(
    orchestrator: Orchestrator,
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    graph: Graph,
    engine: MetricEngine,
    web_dir: Path = Path("web"),
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await orchestrator.start()
            yield
        finally:
            await orchestrator.close()

    app = FastAPI(lifespan=lifespan)

    def compare(
        name: str, start: datetime, end: datetime, segment: dict[str, str]
    ) -> BaselineCompareOutput:
        return baseline_compare(
            BaselineCompareInput(name=name, start=start, end=end, segment=segment),
            con=connection,
            registry=registry,
        )

    def query_metric(
        name: str, start: datetime, end: datetime, segment: dict[str, str]
    ) -> list[Series]:
        return engine.query_metric(name, start, end, segment)

    app.include_router(
        create_router(connection, query_metric=query_metric, baseline_compare=compare)
    )

    @app.get("/api/status")
    async def status() -> Status:
        return orchestrator.status()

    @app.get("/api/graph")
    async def graph_view(
        node: str | None = None, depth: Annotated[int | None, Query(ge=0)] = None
    ) -> GraphView:
        if node is not None and not any(
            metric.name == node for metric in graph.metrics()
        ):
            raise HTTPException(404, "unknown metric")
        return graph.view(node, depth)

    @app.get("/api/metrics", response_model_exclude={"__all__": {"sql"}})
    async def metrics() -> list[MetricDef]:
        return graph.metrics()

    def lookup_metric(name: str) -> MetricDef:
        for metric in graph.metrics():
            if metric.name == name:
                return metric
        raise HTTPException(404, "unknown metric")

    @app.get("/api/metrics/{name}/definition")
    async def definition(name: str) -> MetricDefinition:
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

    @app.get("/api/incidents/{id}/trail")
    async def trail(id: str) -> list[TrailStep]:
        item = await incident(id)
        return [
            step
            for investigation_id in item.investigation_ids
            for step in orchestrator.incidents.trail(investigation_id)
        ]

    @app.get("/api/scenarios")
    async def scenarios() -> list[ScenarioState]:
        return orchestrator.scenario_states()

    async def change_scenario(id: ScenarioId, active: bool) -> ScenarioState:
        try:
            return await orchestrator.set_scenario(id, active=active)
        except LookupError as exc:
            raise HTTPException(404, "scenario is not available") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/scenarios/{id}/inject")
    async def inject(id: ScenarioId) -> ScenarioState:
        return await change_scenario(id, True)

    @app.post("/api/scenarios/{id}/clear")
    async def clear(id: ScenarioId) -> ScenarioState:
        return await change_scenario(id, False)

    @app.post("/api/clock/pause")
    async def pause() -> Status:
        orchestrator.pause()
        return orchestrator.status()

    @app.post("/api/clock/resume")
    async def resume() -> Status:
        orchestrator.resume()
        return orchestrator.status()

    @app.post("/api/clock/step")
    async def step() -> Status:
        return await orchestrator.step_once()

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            orchestrator.events.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/demo/truth")
    def truth() -> list[TruthRow]:
        if not orchestrator.settings.demo.show_truth:
            raise HTTPException(404, "not found")
        with connection.cursor() as cursor:
            rows = cursor.execute(
                "SELECT id, ts, scenario_id, action, CAST(params AS JSON) FROM scenario_log ORDER BY ts, id"
            ).fetchall()
        return [
            TruthRow(
                id=id,
                ts=ts,
                scenario_id=scenario_id,
                action=action,
                params=_PARAMS.validate_json(params),
            )
            for id, ts, scenario_id, action, params in rows
        ]

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    app.mount("/web", StaticFiles(directory=web_dir), name="web")
    # The mock page uses relative assets and is served unchanged at the root.
    app.mount("/", StaticFiles(directory=web_dir), name="root-assets")
    return app
