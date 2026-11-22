"""Bind the deterministic tools to one warehouse and graph."""

from collections.abc import Callable
from functools import partial
from typing import Any, Protocol, cast

import duckdb
from pydantic import BaseModel

from eye.config import SentinelSettings
from eye.graph.store import GraphStore
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.tools.context import check_freshness, list_events, planned_effect_check
from eye.tools.drill import (
    compare_segments,
    decompose,
    drill_down,
    first_change,
    mix_rate_split,
)
from eye.tools.stats import baseline_compare, scan_anomalies, scan_divergent_segments
from eye.warehouse import operations as ops
from eye.warehouse.landing import COMPLETE_HOURS


class DataQueries(Protocol):
    def read[T: BaseModel, R](self, operation: ops.Operation[T, R], args: T) -> R: ...


class LocalQueries:
    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        registry: Registry,
        graph: GraphStore | None = None,
        settings: SentinelSettings | None = None,
        engine: MetricEngine | None = None,
    ) -> None:
        self.connection = connection
        self.registry = registry
        self.graph = graph
        self.engine = engine or MetricEngine(connection, registry)
        options = {"con": connection, "registry": registry}
        scan = {"settings": settings, **options}
        self.handlers: dict[str, Callable[[Any], Any]] = {
            ops.SERIES.path: lambda args: self.engine.query_metric(
                args.metric, args.start, args.end, args.segment, args.group_by
            ),
            ops.BASELINE.path: partial(baseline_compare, **options),
            ops.FRESHNESS.path: partial(check_freshness, settings=settings, **options),
            ops.EVENTS.path: partial(list_events, con=connection),
            ops.SCAN.path: lambda args: scan_anomalies(
                args,
                graph=self.graph.view() if self.graph is not None else None,
                **scan,
            ),
            ops.DIVERGENT.path: lambda args: scan_divergent_segments(
                args,
                graph=self.graph.view() if self.graph is not None else None,
                **scan,
            ),
            ops.COMPARE.path: partial(compare_segments, **options),
            ops.DRILL.path: partial(drill_down, **options),
            ops.FIRST_CHANGE.path: partial(first_change, **options),
            ops.PLANNED.path: partial(planned_effect_check, **options),
            ops.DECOMPOSE.path: lambda args: decompose(
                self.engine, **args.model_dump()
            ),
            ops.MIX_RATE.path: lambda args: mix_rate_split(
                self.engine, args.metric, **args.model_dump(exclude={"metric"})
            ),
            ops.GRAPH.path: lambda args: self._graph().view(args.node, args.depth),
            ops.CYCLES.path: lambda args: self._graph().cycles_through(args.node),
            ops.PATH.path: lambda args: self._graph().path_exists(args.src, args.dst),
            ops.COMPLETE_HOUR.path: self._complete_hour,
        }

    def _graph(self) -> GraphStore:
        if self.graph is None:
            raise ValueError("graph is not available")
        return self.graph

    def read[T: BaseModel, R](self, operation: ops.Operation[T, R], args: T) -> R:
        return cast(R, self.handlers[operation.path](args))

    def partitions(self) -> ops.PartitionWindow:
        with self.connection.cursor() as cursor:
            row = cursor.execute(
                f"SELECT min(ts), max(ts) FROM ({COMPLETE_HOURS})"
            ).fetchone()
        assert row is not None
        return ops.PartitionWindow(earliest=row[0], latest=row[1])

    def _complete_hour(self, args: ops.CompleteHourInput) -> bool:
        with self.connection.cursor() as cursor:
            row = cursor.execute(
                "SELECT count(*) > 0 AND bool_and(coalesce(loaded_rows >= expected_rows, false)) "
                "FROM partition_status WHERE ts = ?",
                [args.ts],
            ).fetchone()
        assert row is not None
        return row[0]
