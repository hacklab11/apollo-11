"""Seasonal baselines and deterministic anomaly ranking."""

import ast
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from math import sqrt
from statistics import mean, median

import duckdb

from eye.config import SentinelSettings
from eye.contracts_v2 import Anomaly, AnomalyKind, Dimension, GraphView, MetricDef
from eye.registry.engine import _evaluate, _names
from eye.registry.loader import Registry
from eye.tools.models import (
    BaselineCompareInput,
    BaselineCompareOutput,
    BaselinePoint,
    ScanAnomaliesInput,
    ScanDivergentSegmentsInput,
    ScanOutput,
)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = 7 * DAY
EPSILON = 1e-9
type Segment = dict[Dimension, str]
type SegmentKey = tuple[tuple[Dimension, str], ...]
type Samples = dict[datetime, _Sample]


@dataclass(frozen=True)
class _Sample:
    num: float
    den: float | None = None

    @property
    def value(self) -> float | None:
        if self.den is None:
            return self.num
        return self.num / self.den if self.den else None


def _pool(samples: Iterable[_Sample]) -> _Sample | None:
    rows = list(samples)
    if not rows:
        return None
    den = None if rows[0].den is None else sum(row.den or 0 for row in rows)
    return _Sample(sum(row.num for row in rows), den)


def _value(sample: _Sample | None) -> float | None:
    return sample.value if sample else None


def _hours(start: datetime, end: datetime) -> tuple[datetime, ...]:
    start = start.astimezone(UTC)
    aligned = start.replace(minute=0, second=0, microsecond=0)
    if aligned < start:
        start = aligned + HOUR
    return tuple(start + i * HOUR for i in range(-((start - end) // HOUR)))


def _z(actual: float, reference: float, spread: float) -> float:
    return (actual - reference) / max(spread, 0.02 * abs(reference), EPSILON)


def _relative(delta: float, reference: float) -> float:
    return delta / abs(reference) if reference else 0.0


def _midnight(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _seasonal_stamps(hours: Sequence[datetime]) -> tuple[datetime, ...]:
    return tuple(sorted({ts - i * WEEK for ts in hours for i in range(5)}))


def _fallback_stamps(hours: Sequence[datetime]) -> tuple[datetime, ...]:
    return tuple(sorted({ts - i * DAY for ts in hours for i in range(1, 8)}))


def _bare_tables(tables: Iterable[str]) -> tuple[str, ...]:
    names = tuple(
        dict.fromkeys(table.rpartition(".")[2].strip('"').lower() for table in tables)
    )
    if "scenario_log" in names:
        raise ValueError("scenario_log is not a tool data source")
    return names


def _partitions(
    con: duckdb.DuckDBPyConnection,
    tables: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> list[tuple[str, str | None, datetime, int, int, datetime | None]]:
    if not tables:
        return []
    with con.cursor() as cur:
        return cur.execute(
            "SELECT table_name, country, ts, loaded_rows, expected_rows, loaded_at "
            f"FROM partition_status WHERE table_name IN ({', '.join('?' * len(tables))}) "
            "AND ts >= ? AND ts < ?",
            [*tables, start, end],
        ).fetchall()


class _Stats:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        registry: Registry,
        start: datetime,
        end: datetime,
        *,
        max_group_dims: int = 1,
    ) -> None:
        self.con = con
        self.registry = registry
        self._max_group_dims = max_group_dims
        self._cache: dict[tuple, dict[SegmentKey, Samples]] = {}
        self._partitions: dict[tuple[str | None, datetime, str], list[int]] = (
            defaultdict(lambda: [0, 0])
        )
        tables = _bare_tables(
            table
            for metric in registry.active
            for table in registry.source_tables[metric.name]
        )
        for table, country, ts, got, expected, _ in _partitions(
            con, tables, start, end
        ):
            for scope in {None, country}:
                totals = self._partitions[scope, ts, table]
                totals[0] += got or 0
                totals[1] += expected or 0

    def completeness(self, metric: MetricDef, segment: Segment, ts: datetime) -> float:
        return min(
            (
                min(1.0, got / expected)
                for table in _bare_tables(self.registry.source_tables[metric.name])
                if (counts := self._partitions.get((segment.get("country"), ts, table)))
                and (got := counts[0]) >= 0
                and (expected := counts[1]) > 0
            ),
            default=1.0,
        )

    def samples(
        self,
        metric: MetricDef,
        stamps: tuple[datetime, ...],
        segment: Segment,
        dims: Sequence[Dimension] = (),
    ) -> dict[SegmentKey, Samples]:
        unknown = (segment.keys() | set(dims)) - set(metric.dimensions)
        if unknown or segment.keys() & set(dims):
            raise ValueError(
                f"{metric.name}: invalid dimensions for slice {segment} / {dims}"
            )
        if metric.status != "active":
            raise ValueError(
                f"{metric.name} is pending: its source data is not available yet"
            )
        if not stamps:
            return {}
        key = metric.name, stamps, tuple(sorted(segment.items())), tuple(dims)
        if key not in self._cache:
            self._cache[key] = (
                self._sql_samples(metric, stamps, segment, dims)
                if metric.kind == "sql"
                else self._formula_samples(metric, stamps, segment, dims)
            )
        return self._cache[key]

    def _sql_samples(
        self,
        metric: MetricDef,
        stamps: tuple[datetime, ...],
        segment: Segment,
        dims: Sequence[Dimension],
    ) -> dict[SegmentKey, Samples]:
        has_den = self.registry.has_den[metric.name]
        keys = [f'"{dim}"' for dim in dims]
        select = ["ts", *keys, "SUM(num)", "SUM(den)" if has_den else "NULL"]
        grouped_indexes = {}
        if keys:
            mask = (1 << len(dims)) - 1
            grouped_indexes = {
                mask ^ sum(1 << (len(dims) - 1 - idx) for idx in indexes): indexes
                for size in range(self._max_group_dims + 1)
                for indexes in combinations(range(len(dims)), size)
            }
            select.append(f"GROUPING_ID({', '.join(keys)})")
            groups = ", ".join(
                f"({', '.join(['ts', *(keys[idx] for idx in indexes)])})"
                for indexes in grouped_indexes.values()
            )
            grouping = f"GROUP BY GROUPING SETS ({groups})"
        else:
            grouping = "GROUP BY ts"
        where = ["ts >= ?", "ts <= ?", *(f'"{dim}" = ?' for dim in segment)]
        args = [stamps[0], stamps[-1], *segment.values()]
        if stamps[-1] - stamps[0] != (len(stamps) - 1) * HOUR:
            where.append(f"ts IN ({', '.join('?' * len(stamps))})")
            args.extend(stamps)
        with self.con.cursor() as cur:
            rows = cur.execute(
                f"SELECT {', '.join(select)} FROM ({metric.sql}) m "
                f"WHERE {' AND '.join(where)} {grouping}",
                args,
            ).fetchall()
        total = tuple(sorted(segment.items()))
        slices: dict[SegmentKey, Samples] = defaultdict(dict)
        for ts, *row in rows:
            num, den = row[len(dims) : len(dims) + 2]
            if num is None:
                continue
            scope = total
            if dims and (indexes := grouped_indexes[row[-1]]):
                if any(row[idx] is None for idx in indexes):
                    continue
                scope = tuple(
                    sorted((*total, *((dims[idx], str(row[idx])) for idx in indexes)))
                )
            slices[scope][ts] = _Sample(
                float(num), float(den or 0) if has_den else None
            )
        return dict(slices)

    def _formula_samples(
        self,
        metric: MetricDef,
        stamps: tuple[datetime, ...],
        segment: Segment,
        dims: Sequence[Dimension],
    ) -> dict[SegmentKey, Samples]:
        assert metric.expression is not None
        expr = ast.parse(metric.expression, mode="eval").body
        inputs = {
            name: self.samples(self.registry.get(name), stamps, segment, dims)
            for name in _names(expr)
        }
        result: dict[SegmentKey, Samples] = {}
        for key in set().union(*inputs.values()):
            by_name = {name: slices.get(key, {}) for name, slices in inputs.items()}
            rows = {}
            for ts in set().union(*by_name.values()):
                values = {
                    name: _value(samples.get(ts)) for name, samples in by_name.items()
                }
                if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
                    num, den = (
                        _evaluate(expr.left, values),
                        _evaluate(expr.right, values),
                    )
                    if num is not None and den is not None:
                        rows[ts] = _Sample(num, den)
                elif (value := _evaluate(expr, values)) is not None:
                    rows[ts] = _Sample(value)
            if rows:
                result[key] = rows
        return result

    def baselines(
        self,
        metric: MetricDef,
        hours: tuple[datetime, ...],
        segment: Segment,
        dims: Sequence[Dimension] = (),
    ) -> dict[SegmentKey, list[tuple[BaselinePoint, _Sample | None, _Sample | None]]]:
        seasonal = self.samples(metric, _seasonal_stamps(hours), segment, dims)
        if not dims and not seasonal and hours:
            seasonal = {tuple(sorted(segment.items())): {}}
        fallback: dict[SegmentKey, Samples] | None = None
        daily: dict[SegmentKey, Samples] | None = None
        result = {}
        for key, sample in seasonal.items():
            scope = dict(key)
            if dims and not any(ts in sample for ts in hours):
                continue
            rows = []
            for ts in hours:
                references = self._references(
                    metric, scope, sample, (ts - i * WEEK for i in range(1, 5))
                )
                current = sample.get(ts)
                if len(references) >= 3:
                    reference, spread = _expected(references)
                elif current is not None or not dims:
                    if fallback is None:
                        fallback = self.samples(
                            metric, _fallback_stamps(hours), segment, dims
                        )
                    references = self._references(
                        metric,
                        scope,
                        fallback.get(key, {}),
                        (ts - i * DAY for i in range(1, 8)),
                    )
                    reference, spread = None, None
                    if references:
                        if daily is None:
                            daily = self.samples(
                                metric,
                                _hours(
                                    _midnight(hours[0]) - 28 * DAY, _midnight(hours[-1])
                                ),
                                segment,
                                dims,
                            )
                        factors = self._weekday_factors(
                            metric, scope, daily.get(key, {}), ts
                        )
                        reference, spread = _expected(references, factors)
                else:
                    reference, spread = None, None
                actual, center = _value(current), _value(reference)
                point = BaselinePoint(
                    ts=ts,
                    actual=actual,
                    expected=center,
                    spread=spread,
                    lower=center - 3 * spread
                    if center is not None and spread is not None
                    else None,
                    upper=center + 3 * spread
                    if center is not None and spread is not None
                    else None,
                    z=_z(actual, center, spread)
                    if actual is not None and center is not None and spread is not None
                    else None,
                    completeness=self.completeness(metric, scope, ts),
                )
                rows.append((point, current, reference))
            result[key] = rows
        return result

    def _references(
        self,
        metric: MetricDef,
        scope: Segment,
        samples: Samples,
        stamps: Iterable[datetime],
    ) -> list[_Sample]:
        return [
            row
            for ts in stamps
            if (row := samples.get(ts)) is not None
            and row.value is not None
            and self.completeness(metric, scope, ts) >= 0.9
        ]

    def _weekday_factors(
        self, metric: MetricDef, scope: Segment, samples: Samples, ts: datetime
    ) -> _Sample:
        days: dict[datetime, list[_Sample]] = defaultdict(list)
        end = _midnight(ts)
        for stamp, row in samples.items():
            if (
                end - 28 * DAY <= stamp < end
                and self.completeness(metric, scope, stamp) >= 0.9
            ):
                days[_midnight(stamp)].append(row)
        totals = {day: _pool(rows) for day, rows in days.items()}
        all_days = [row for row in totals.values() if row is not None]
        weekdays = [
            row
            for day, row in totals.items()
            if row is not None and day.weekday() == ts.weekday()
        ]

        def factor(all_values: list[float], matching: list[float]) -> float:
            return (
                mean(matching) / mean(all_values)
                if matching and mean(all_values)
                else 1.0
            )

        return _Sample(
            factor([r.num for r in all_days], [r.num for r in weekdays]),
            factor([r.den or 0 for r in all_days], [r.den or 0 for r in weekdays]),
        )


def _expected(
    rows: list[_Sample], factors: _Sample = _Sample(1.0, 1.0)
) -> tuple[_Sample, float | None]:
    median_num = median(row.num for row in rows)
    median_den = (
        median(row.den or 0 for row in rows) if rows[0].den is not None else None
    )
    reference = _Sample(
        median_num * factors.num,
        median_den * (factors.den or 0) if median_den is not None else None,
    )
    if reference.value is None:
        return reference, None
    adjusted = (
        _Sample(
            row.num * factors.num,
            row.den * (factors.den or 0) if row.den is not None else None,
        )
        for row in rows
    )
    values = [value for row in adjusted if (value := row.value) is not None]
    if not values:
        return reference, None
    center = median(values)
    return reference, median(abs(value - center) for value in values) * 1.4826


def expected(
    metric: str,
    ts: datetime,
    segment: Segment,
    *,
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
) -> BaselinePoint:
    return baseline_compare(
        BaselineCompareInput(name=metric, start=ts, end=ts + HOUR, segment=segment),
        con=con,
        registry=registry,
    ).points[0]


def _baseline_compare(
    data: BaselineCompareInput, stats: _Stats
) -> BaselineCompareOutput:
    metric = stats.registry.get(data.name)
    hours = _hours(data.start, data.end)
    rows = stats.baselines(metric, hours, data.segment).get(
        tuple(sorted(data.segment.items())), []
    )
    keyed = {point.ts: point for point, _, _ in rows}
    points = [
        keyed.get(ts)
        or BaselinePoint(
            ts=ts,
            actual=None,
            expected=None,
            spread=None,
            lower=None,
            upper=None,
            z=None,
            completeness=stats.completeness(metric, data.segment, ts),
        )
        for ts in hours
    ]
    actual, reference = _window_values(rows)
    delta = actual - reference if actual is not None and reference is not None else None
    summary = (
        f"{metric.name}: {actual:.4g} vs {reference:.4g} expected (delta {delta:+.4g}) over {len(hours)}h."
        if delta is not None
        else f"{metric.name}: no comparable window total over {len(hours)}h."
    )
    return BaselineCompareOutput(
        points=points, actual=actual, expected=reference, delta=delta, summary=summary
    )


def baseline_compare(
    data: BaselineCompareInput, *, con: duckdb.DuckDBPyConnection, registry: Registry
) -> BaselineCompareOutput:
    return _baseline_compare(
        data, _Stats(con, registry, data.start - 29 * DAY, data.end)
    )


type BaselineRows = list[tuple[BaselinePoint, _Sample | None, _Sample | None]]


def _window_values(rows: BaselineRows) -> tuple[float | None, float | None]:
    matched = [(a, e) for _, a, e in rows if a is not None and e is not None]
    return (
        _value(_pool(a for a, _ in matched)),
        _value(_pool(e for _, e in matched)),
    )


def _window_spread(rows: BaselineRows, *, ratio: bool) -> float:
    spreads = [point.spread for point, _, _ in rows if point.spread is not None]
    spread = sqrt(sum(s * s for s in spreads))
    return spread / len(spreads) if ratio and spreads else spread


def _weight(metric: MetricDef, weights: dict[str, float]) -> float:
    default = 1.0 if metric.name in {"contribution_margin", "gmv", "orders"} else 0.3
    return weights.get(metric.name, metric.impact_weight or default)


def _graph_weights(graph: GraphView | None) -> dict[str, float]:
    return (
        {
            str(node["id"]): float(node["impact_weight"])
            for node in graph.nodes
            if "id" in node and node.get("impact_weight") is not None
        }
        if graph is not None
        else {}
    )


def _impact_scale(stats: _Stats, ts: datetime) -> tuple[float, float]:
    values = []
    for name in ("contribution_margin", "orders"):
        metric = stats.registry.metrics.get(name)
        if metric is None or metric.status != "active":
            values.append(0.0)
            continue
        samples = stats.samples(metric, _seasonal_stamps((ts,)), {}).get((), {})
        history = [
            abs(value)
            for stamp, row in samples.items()
            if stamp < ts and (value := row.value) is not None
        ]
        values.append(mean(history) if history else 0.0)
    margin, orders = values
    return margin, margin / orders if orders else 0.0


def _anomaly(
    metric: MetricDef,
    key: SegmentKey,
    ts: datetime,
    kind: AnomalyKind,
    hours: int,
    actual: float,
    reference: float,
    z: float,
    completeness: float,
    weight: float,
    margin: float,
    per_order: float,
    *,
    total_z: float | None = None,
    share_now: float | None = None,
    share_expected: float | None = None,
) -> Anomaly:
    delta = actual - reference
    relative = _relative(delta, reference)
    if metric.unit == "eur":
        impact = abs(delta) / hours
    elif metric.unit == "count":
        impact = abs(delta) * per_order / hours
    elif metric.unit == "ratio":
        impact = margin * abs(relative)
    else:
        impact = 0.0
    scope = ",".join(f"{dim}={value}" for dim, value in key)
    return Anomaly(
        id=f"{ts.isoformat()}:{metric.name}:{kind}:{scope}",
        ts=ts,
        metric=metric.name,
        segment=dict(key),
        kind=kind,
        actual=actual,
        expected=reference,
        z=z,
        delta=delta,
        delta_pct=relative,
        window_hours=hours,
        total_z=total_z,
        share_now=share_now,
        share_expected=share_expected,
        impact_eur_per_hour=impact * weight,
        completeness=completeness,
        priority=abs(z) * max(weight, 0.05) * completeness,
    )


def _scan_output(anomalies: list[Anomaly], limit: int | None = None) -> ScanOutput:
    ranked = sorted(anomalies, key=lambda anomaly: (-anomaly.priority, anomaly.id))[
        :limit
    ]
    summary = f"{len(ranked)} of {len(anomalies)} anomalies returned."
    if ranked:
        summary += f" Top: {ranked[0].metric}, z {ranked[0].z:+.2f}, €{ranked[0].impact_eur_per_hour:.0f}/h."
    return ScanOutput(anomalies=ranked, summary=summary)


def scan_anomalies(
    data: ScanAnomaliesInput,
    *,
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: SentinelSettings | None = None,
    graph: GraphView | None = None,
) -> ScanOutput:
    settings = settings or SentinelSettings()
    ts = data.ts.astimezone(UTC)
    stats = _Stats(con, registry, ts - 29 * DAY, ts + HOUR, max_group_dims=2)
    weights = _graph_weights(graph)
    margin, per_order = _impact_scale(stats, ts)
    anomalies = []
    for metric in registry.active:
        for key, [(point, _, reference)] in stats.baselines(
            metric, (ts,), {}, metric.dimensions
        ).items():
            if (
                point.actual is None
                or point.expected is None
                or point.z is None
                or reference is None
            ):
                continue
            if (
                abs(point.z) < settings.z_threshold
                or (metric.unit == "count" and point.expected < 20)
                or (metric.unit == "eur" and reference.num < 200)
            ):
                continue
            anomalies.append(
                _anomaly(
                    metric,
                    key,
                    ts,
                    "point",
                    1,
                    point.actual,
                    point.expected,
                    point.z,
                    point.completeness,
                    _weight(metric, weights),
                    margin,
                    per_order,
                )
            )
    return _scan_output(anomalies, settings.top_k * 3)


def scan_divergent_segments(
    data: ScanDivergentSegmentsInput,
    *,
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: SentinelSettings | None = None,
    graph: GraphView | None = None,
) -> ScanOutput:
    settings = settings or SentinelSettings()
    ts = data.ts.astimezone(UTC)
    start = ts - (data.window_hours - 1) * HOUR
    hours = _hours(start, ts + HOUR)
    stats = _Stats(con, registry, start - 29 * DAY, ts + HOUR)
    weights = _graph_weights(graph)
    margin, per_order = _impact_scale(stats, ts)
    anomalies = []
    for metric in registry.active:
        native_share = metric.unit == "ratio" and metric.name.endswith("_share")
        if not native_share and metric.unit not in {"count", "eur"}:
            continue
        groups = stats.baselines(metric, hours, {}, metric.dimensions)
        total_rows = groups.get((), [])
        total, total_expected = _window_values(total_rows)
        if total is None or total_expected is None:
            continue
        total_z = _z(
            total, total_expected, _window_spread(total_rows, ratio=native_share)
        )
        for key, rows in groups.items():
            if (not key and not native_share) or not rows:
                continue
            ratio = any(a is not None and a.den is not None for _, a, _ in rows)
            if ratio and not native_share:
                continue
            actual, reference = _window_values(rows)
            if actual is None or reference is None:
                continue
            z = _z(actual, reference, _window_spread(rows, ratio=ratio))
            shared = (
                metric,
                key,
                ts,
                data.window_hours,
                actual,
                reference,
                z,
                min(p.completeness for p, _, _ in rows),
                _weight(metric, weights),
                margin,
                per_order,
            )
            if (
                key
                and not native_share
                and ((z <= -3 and total_z >= -1) or (z >= 3 and total_z <= 1))
            ):
                anomalies.append(
                    _anomaly(
                        *shared[:3],
                        "divergent_segment",
                        *shared[3:],
                        total_z=total_z,
                    )
                )
            if native_share:
                share_now, share_expected = actual, reference
            elif total and total_expected:
                share_now, share_expected = actual / total, reference / total_expected
            else:
                continue
            if abs(share_now - share_expected) * 100 >= settings.divergence_pp:
                anomalies.append(
                    _anomaly(
                        *shared[:3],
                        "share_shift",
                        *shared[3:],
                        total_z=total_z,
                        share_now=share_now,
                        share_expected=share_expected,
                    )
                )
    return _scan_output(anomalies)
