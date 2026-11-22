"""Run the read-only marketplace data service independently of core."""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import RLock

import duckdb
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from eye.config import SentinelSettings, load_settings
from eye.contracts import GraphView, MetricDef
from eye.graph.bootstrap import load_or_bootstrap
from eye.graph.store import GRAPH_PATH, GraphStore
from eye.registry.loader import CONFIG_DIR, load_registry
from eye.schema import validate_schema
from eye.warehouse import operations as ops
from eye.warehouse.domain import (
    DataParams,
    DataResponse,
    create_router,
    local_domain_reader,
)
from eye.warehouse.landing import ParquetLanding
from eye.warehouse.queries import LocalQueries


class Warehouse:
    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        settings: SentinelSettings,
        *,
        lake: Path | None = None,
        graph_path: Path = GRAPH_PATH,
        config_dir: Path = CONFIG_DIR,
    ) -> None:
        self.connection = connection
        self.settings = settings
        self.graph_path = graph_path
        self.config_dir = config_dir
        self.landing = ParquetLanding(lake, connection) if lake is not None else None
        self.queries: LocalQueries | None = None
        self.lock = RLock()
        if self.landing is None:
            validate_schema(connection)

    @contextmanager
    def snapshot(self) -> Iterator[LocalQueries]:
        # The landed snapshot and engine cache must change together.
        with self.lock:
            changed = self.landing is not None and self.landing.refresh()
            if self.queries is None or (
                changed
                and any(
                    m.status == "pending"
                    for m in self.queries.registry.metrics.values()
                )
            ):
                registry = load_registry(self.connection, self.config_dir)
                self.queries = LocalQueries(
                    self.connection, registry, settings=self.settings
                )
            elif changed:
                self.queries.engine.invalidate()
            yield self.queries

    def graph(self, queries: LocalQueries) -> GraphStore:
        if queries.graph is None:
            latest = queries.partitions().latest
            queries.graph = load_or_bootstrap(
                queries.engine,
                path=self.graph_path,
                config_dir=self.config_dir,
                end=latest + timedelta(hours=1) if latest else None,
            )
        return queries.graph


def create_app(warehouse: Warehouse) -> FastAPI:
    app = FastAPI(title="Eye Data Service", version="1")

    @app.get("/healthz")
    def health() -> ops.PartitionWindow:
        with warehouse.snapshot() as queries:
            window = queries.partitions()
            if window.latest is None:
                raise HTTPException(503, "no completed warehouse hours")
            warehouse.graph(queries)
            return window

    @app.get("/v1/partitions")
    def partitions() -> ops.PartitionWindow:
        with warehouse.snapshot() as queries:
            return queries.partitions()

    @app.get("/v1/metrics")
    def metrics() -> list[MetricDef]:
        with warehouse.snapshot() as queries:
            return warehouse.graph(queries).metrics()

    @app.get("/v1/graph")
    def graph(node: str | None = None, depth: int | None = None) -> GraphView:
        if depth is not None and depth < 0:
            raise HTTPException(422, "graph depth cannot be negative")
        return execute(ops.GRAPH, ops.GraphInput(node=node, depth=depth))

    def execute[T: BaseModel, R](operation: ops.Operation[T, R], args: T) -> R:
        try:
            ops.enforce_query_bounds(args)
            with warehouse.snapshot() as queries:
                if operation.path in {
                    ops.GRAPH.path,
                    ops.CYCLES.path,
                    ops.PATH.path,
                    ops.SCAN.path,
                    ops.DIVERGENT.path,
                }:
                    warehouse.graph(queries)
                return queries.read(operation, args)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def endpoint[T: BaseModel, R](operation: ops.Operation[T, R]) -> Callable[[T], R]:
        def run(args: T) -> R:
            return execute(operation, args)

        run.__annotations__["args"] = operation.input
        return run

    for operation in (
        ops.SERIES,
        ops.BASELINE,
        ops.FRESHNESS,
        ops.EVENTS,
        ops.SCAN,
        ops.DIVERGENT,
        ops.COMPARE,
        ops.DRILL,
        ops.FIRST_CHANGE,
        ops.PLANNED,
        ops.DECOMPOSE,
        ops.MIX_RATE,
        ops.GRAPH,
        ops.CYCLES,
        ops.PATH,
        ops.COMPLETE_HOUR,
    ):
        app.add_api_route(
            f"/v1/{operation.path}",
            endpoint(operation),
            methods=["POST"],
            response_model=operation.response,
            name=operation.path,
        )

    def domain(path: str, params: DataParams) -> DataResponse:
        with warehouse.snapshot() as queries:
            return local_domain_reader(queries.connection)(path, params)

    app.include_router(
        create_router(
            read=domain,
            query_metric=lambda name, **kwargs: execute(
                ops.SERIES, ops.SeriesInput(metric=name, **kwargs)
            ),
            baseline_compare=lambda **kwargs: execute(
                ops.BASELINE, ops.BaselineCompareInput(**kwargs)
            ),
        )
    )
    return app


def main() -> None:
    path = os.environ.get("EYE_WAREHOUSE_PATH")
    lake = None if path else Path(os.environ.get("SIM_LAKE_DIR", "data/lake"))
    settings = load_settings()
    with duckdb.connect(path or ":memory:", read_only=bool(path)) as connection:
        app = create_app(Warehouse(connection, settings.sentinel, lake=lake))
        uvicorn.run(
            app,
            host=os.environ.get("EYE_DATA_HOST", "127.0.0.1"),
            port=int(os.environ.get("EYE_DATA_PORT", "8150")),
            access_log=False,
        )
