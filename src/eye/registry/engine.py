"""Run registry metrics over DuckDB: wrapper SQL for `sql` metrics, Python for formulas."""

import ast
import operator
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta

import duckdb

from eye.contracts import MetricDef, Series, SeriesPoint
from eye.registry.loader import Registry

type Key = tuple[str, ...]
# Per series: ts -> (num, den); den is None for metrics without a denominator.
type Sums = dict[Key, dict[datetime, tuple[float | None, float | None]]]

_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class MetricEngine:
    def __init__(self, con: duckdb.DuckDBPyConnection, registry: Registry) -> None:
        self._con = con
        self._registry = registry
        self._cache: dict[tuple, list[Series]] = {}
        self._formulas = {
            m.name: ast.parse(m.expression, mode="eval").body
            for m in registry.metrics.values()
            if m.kind == "formula" and m.expression
        }

    def invalidate(self) -> None:
        self._cache.clear()

    def query_metric(
        self,
        name: str,
        start_ts: datetime,
        end_ts: datetime,
        segment: Mapping[str, str] | None = None,
        group_by: Sequence[str] = (),
    ) -> list[Series]:
        """One series per group_by value combination, hourly points in [start_ts, end_ts)."""
        segment = dict(segment or {})
        group = list(group_by)
        metric = self._metric(name, [*segment, *group])
        key = (name, start_ts, end_ts, tuple(sorted(segment.items())), tuple(group))
        if key not in self._cache:
            self._cache[key] = self._series(metric, start_ts, end_ts, segment, group)
        return [s.model_copy(deep=True) for s in self._cache[key]]

    def metric_value(
        self, name: str, ts: datetime, segment: Mapping[str, str] | None = None
    ) -> float | None:
        """Value of the hour starting at ts, or None when there is no data."""
        series = self.query_metric(name, ts, ts + timedelta(hours=1), segment)
        points = series[0].points if series else []
        return points[0].value if points else None

    def metric_window(
        self,
        name: str,
        start_ts: datetime,
        end_ts: datetime,
        segment: Mapping[str, str] | None = None,
    ) -> float | None:
        """One value for the whole window, pooled as SUM(num) / SUM(den); None without data."""
        segment = dict(segment or {})
        metric = self._metric(name, list(segment))
        return self._window(metric, start_ts, end_ts, segment)

    def _metric(self, name: str, dimensions: list[str]) -> MetricDef:
        metric = self._registry.get(name)
        if unknown := [d for d in dimensions if d not in metric.dimensions]:
            raise ValueError(
                f"{name}: unknown dimensions {unknown}; allowed: {metric.dimensions}"
            )
        if len(set(dimensions)) != len(dimensions):
            raise ValueError(f"{name}: a dimension is both in segment and group_by")
        if metric.status == "pending":
            raise ValueError(f"{name} is pending: its source data is not available yet")
        return metric

    def _series(
        self,
        metric: MetricDef,
        start: datetime,
        end: datetime,
        segment: dict[str, str],
        group: list[str],
    ) -> list[Series]:
        values = self._values(metric, start, end, segment, group)
        tables = self._registry.source_tables[metric.name]
        completeness = self._completeness(tables, start, end, segment, group)
        return [
            Series(
                metric=metric.name,
                segment={**segment, **dict(zip(group, gkey, strict=True))},
                points=[
                    SeriesPoint(
                        ts=ts,
                        value=value,
                        completeness=completeness(gkey, ts),
                    )
                    for ts, value in sorted(points.items())
                ],
            )
            for gkey, points in sorted(values.items())
        ]

    def _values(
        self,
        metric: MetricDef,
        start: datetime,
        end: datetime,
        segment: dict[str, str],
        group: list[str],
    ) -> dict[Key, dict[datetime, float | None]]:
        if metric.kind == "sql":
            sums = self._sums(metric, start, end, segment, group, by_ts=True)
            return {
                gkey: {ts: _ratio(*nd) for ts, nd in points.items()}
                for gkey, points in sums.items()
            }
        inputs = {
            name: self._values(self._registry.get(name), start, end, segment, group)
            for name in _names(self._formulas[metric.name])
        }
        keys = sorted(set().union(*inputs.values()))
        out: dict[Key, dict[datetime, float | None]] = {}
        for gkey in keys:
            series = {name: values.get(gkey, {}) for name, values in inputs.items()}
            stamps = sorted(set().union(*series.values()))
            out[gkey] = {
                ts: _evaluate(
                    self._formulas[metric.name],
                    {n: s.get(ts) for n, s in series.items()},
                )
                for ts in stamps
            }
        return out

    def _window(
        self,
        metric: MetricDef,
        start: datetime,
        end: datetime,
        segment: dict[str, str],
    ) -> float | None:
        if metric.kind == "sql":
            sums = self._sums(metric, start, end, segment, [], by_ts=False)
            return _ratio(*sums[()][start]) if sums else None
        node = self._formulas[metric.name]
        return _evaluate(
            node,
            {
                n: self._window(self._registry.get(n), start, end, segment)
                for n in _names(node)
            },
        )

    def _sums(
        self,
        metric: MetricDef,
        start: datetime,
        end: datetime,
        segment: dict[str, str],
        group: list[str],
        *,
        by_ts: bool,
    ) -> Sums:
        # Dimension names are checked against the metric's declared list, so they are safe
        # to place in the SQL text; every value is a bound parameter.
        keys = (["ts"] if by_ts else []) + [f'"{d}"' for d in group]
        has_den = self._registry.has_den[metric.name]
        select = [*keys, "SUM(num)", "SUM(den)" if has_den else "NULL"]
        where = ["ts >= ?", "ts < ?", *(f'"{d}" = ?' for d in segment)]
        sql = (
            f"SELECT {', '.join(select)} FROM ({metric.sql}) m "
            f"WHERE {' AND '.join(where)}"
            + (f" GROUP BY {', '.join(keys)}" if keys else "")
        )
        with self._con.cursor() as cur:
            rows = cur.execute(sql, [start, end, *segment.values()]).fetchall()
        out: Sums = defaultdict(dict)
        for row in rows:
            *key_values, num, den = row
            if not by_ts and num is None and den is None:
                continue
            ts = key_values.pop(0) if by_ts else start
            out[tuple(str(v) for v in key_values)][ts] = (num, den)
        return dict(out)

    def _completeness(
        self,
        tables: tuple[str, ...],
        start: datetime,
        end: datetime,
        segment: dict[str, str],
        group: list[str],
    ) -> Callable[[Key, datetime], float]:
        """loaded_rows / expected_rows for the hour, worst source table; 1.0 if unknown."""
        if not tables:
            return lambda gkey, ts: 1.0
        with self._con.cursor() as cur:
            rows = cur.execute(
                "SELECT table_name, country, ts, loaded_rows, expected_rows "
                "FROM partition_status "
                f"WHERE table_name IN ({', '.join('?' * len(tables))}) "
                "AND ts >= ? AND ts < ?",
                [*tables, start, end],
            ).fetchall()
        loaded: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        for table, country, ts, got, expected in rows:
            if "country" in segment and country != segment["country"]:
                continue
            for scope in (None, country):
                totals = loaded[(scope, ts, table)]
                totals[0] += got or 0
                totals[1] += expected or 0
        country_at = group.index("country") if "country" in group else None

        def completeness(gkey: Key, ts: datetime) -> float:
            scope = gkey[country_at] if country_at is not None else None
            shares = [
                min(1.0, got / expected)
                for got, expected in (
                    loaded.get((scope, ts, t), (0, 0)) for t in tables
                )
                if expected > 0
            ]
            return min(shares, default=1.0)

        return completeness


def _names(node: ast.expr) -> list[str]:
    return list(dict.fromkeys(n.id for n in ast.walk(node) if isinstance(n, ast.Name)))


def _evaluate(node: ast.expr, values: Mapping[str, float | None]) -> float | None:
    match node:
        case ast.Name(id=name):
            return values.get(name)
        case ast.BinOp(left=left, op=op, right=right):
            a, b = _evaluate(left, values), _evaluate(right, values)
            if a is None or b is None or (isinstance(op, ast.Div) and b == 0):
                return None
            return _OPS[type(op)](a, b)
    raise ValueError(f"unsupported formula node: {ast.dump(node)}")


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None:
        return None
    if den is None:
        return float(num)
    return float(num) / den if den else None
