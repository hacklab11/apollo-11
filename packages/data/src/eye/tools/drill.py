"""Formula attribution and exact denominator-weighted shift-share."""

from datetime import datetime, timedelta
from math import log

import duckdb
from pydantic import BaseModel, Field, validate_call

from eye.contracts_v2 import Contribution
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.tools.identity import parse_identity, ratio_sums
from eye.tools.models import (
    BaselineCompareInput,
    CompareSegmentsInput,
    CompareSegmentsOutput,
    DrillDownInput,
    DrillDownOutput,
    DrillDownRow,
    FirstChangeInput,
    FirstChangeOutput,
    SegmentComparison,
)
from eye.tools.stats import (
    DAY,
    EPSILON,
    HOUR,
    WEEK,
    SegmentKey,
    _baseline_compare,
    _hours,
    _pool,
    _relative,
    _Sample,
    _Stats,
    _value,
    _window_spread,
    _z,
)


class AttributionResult(BaseModel):
    contributions: list[Contribution]
    delta: float
    totals: dict[str, float] = Field(default_factory=dict)
    summary: str = Field(max_length=300)


@validate_call(config={"arbitrary_types_allowed": True})
def decompose(
    engine: MetricEngine,
    metric: str,
    start: datetime,
    end: datetime,
    base_start: datetime | None = None,
    base_end: datetime | None = None,
    segment: dict[str, str] | None = None,
) -> AttributionResult:
    base_start = base_start or start - timedelta(weeks=1)
    base_end = base_end or end - timedelta(weeks=1)
    if end <= start or base_end <= base_start:
        raise ValueError("windows must have positive duration")
    identities = [parse_identity(i.text) for i in engine._registry.identities]
    identity = next((i for i in identities if i.target == metric), None)
    if identity is None:
        raise ValueError(f"no identity targets {metric}")
    names = {metric, *(t.name for t in identity.terms)}
    before = {n: engine.metric_window(n, base_start, base_end, segment) for n in names}
    after = {n: engine.metric_window(n, start, end, segment) for n in names}
    if any(v is None for v in [*before.values(), *after.values()]):
        raise ValueError("attribution requires data in both windows")
    before = {n: float(v) for n, v in before.items()}
    after = {n: float(v) for n, v in after.items()}
    delta = after[metric] - before[metric]
    note = ""
    if identity.kind == "product":
        if any(v <= 0 for v in [*before.values(), *after.values()]):
            raise ValueError("log attribution requires strictly positive values")
        change = log(after[metric] / before[metric])
        equal = abs(change) < 1e-6
        note = "; equal split (near-zero log change)" if equal else ""
    rows = []
    for term in identity.terms:
        if identity.kind == "sum":
            value = term.sign * (after[term.name] - before[term.name])
            share = value / delta if delta else 0.0
        else:
            share = (
                1 / len(identity.terms)
                if equal
                else term.exponent * log(after[term.name] / before[term.name]) / change
            )
            value = share * delta
        rows.append(
            Contribution(factor=term.name, kind="formula", delta=value, share=share)
        )
    rows.append(
        Contribution(
            factor="residual",
            kind="residual",
            delta=delta - sum(r.delta for r in rows),
            share=1 - sum(r.share for r in rows),
        )
    )
    return AttributionResult(
        contributions=rows,
        delta=delta,
        summary=f"{metric}: delta {delta:.6g}, {len(rows)} rows{note}"[:300],
    )


@validate_call(config={"arbitrary_types_allowed": True})
def mix_rate_split(
    engine: MetricEngine,
    ratio_metric: str,
    dim: str,
    start: datetime,
    end: datetime,
    base_start: datetime | None = None,
    base_end: datetime | None = None,
    segment: dict[str, str] | None = None,
) -> AttributionResult:
    base_start = base_start or start - timedelta(weeks=1)
    base_end = base_end or end - timedelta(weeks=1)
    if end <= start or base_end <= base_start:
        raise ValueError("windows must have positive duration")

    def pooled(a: datetime, b: datetime) -> dict[str, tuple[float, float]]:
        sums = ratio_sums(engine, ratio_metric, a, b, segment or {}, [dim], by_ts=False)
        return {
            key[0]: (float(points[a][0] or 0), float(points[a][1] or 0))
            for key, points in sums.items()
        }

    old, new = pooled(base_start, base_end), pooled(start, end)
    d0, d1 = sum(d for _, d in old.values()), sum(d for _, d in new.values())
    if d0 <= 0 or d1 <= 0:
        raise ValueError(
            "mix/rate requires positive denominator totals in both windows"
        )
    delta = sum(n for n, _ in new.values()) / d1 - sum(n for n, _ in old.values()) / d0
    rows = []
    totals = {"mix": 0.0, "rate": 0.0}
    for value in sorted(old.keys() | new.keys()):
        n0, den0 = old.get(value, (0, 0))
        n1, den1 = new.get(value, (0, 0))
        if (den0 == 0 and n0 != 0) or (den1 == 0 and n1 != 0):
            raise ValueError("nonzero numerator with zero denominator")
        r0, r1 = n0 / den0 if den0 else 0, n1 / den1 if den1 else 0
        for kind, effect in (
            ("mix", (den1 / d1 - den0 / d0) * r0),
            ("rate", den1 / d1 * (r1 - r0)),
        ):
            totals[kind] += effect
            rows.append(
                Contribution(
                    factor=value,
                    kind=kind,
                    delta=effect,
                    share=effect / delta if delta else 0,
                )
            )
    return AttributionResult(
        contributions=rows,
        delta=delta,
        totals=totals,
        summary=f"Mix {totals['mix']:.6g}, rate {totals['rate']:.6g}, delta {delta:.6g}",
    )


def _compare(
    data: DrillDownInput, con: duckdb.DuckDBPyConnection, registry: Registry
) -> dict[SegmentKey, tuple[SegmentComparison, _Sample | None, _Sample | None]]:
    start, end = data.start, data.end
    base_start, base_end = data.base_start or start - WEEK, data.base_end or end - WEEK
    hours, base_hours = _hours(start, end), _hours(base_start, base_end)
    metric = registry.get(data.metric)
    stats = _Stats(con, registry, min(start - 29 * DAY, base_start), max(end, base_end))
    dims = (data.dim,)
    samples = stats.samples(
        metric, tuple(sorted(set(hours) | set(base_hours))), data.segment, dims
    )
    baselines = stats.baselines(metric, hours, data.segment, dims)
    out = {}
    for key, rows in sorted(samples.items()):
        current = _pool(rows[t] for t in hours if t in rows)
        baseline = _pool(rows[t] for t in base_hours if t in rows)
        actual, expected = _value(current), _value(baseline)
        delta = (
            actual - expected if actual is not None and expected is not None else None
        )
        spread = _window_spread(
            baselines.get(key, []),
            ratio=current is not None and current.den is not None,
        )
        out[key] = (
            SegmentComparison(
                value=dict(key).get(data.dim, ""),
                actual=actual,
                expected=expected,
                delta=delta,
                delta_pct=_relative(delta, expected)
                if delta is not None and expected
                else None,
                z=_z(actual, expected, spread)
                if actual is not None and expected is not None
                else None,
            ),
            current,
            baseline,
        )
    return out


def _contribution(
    actual: _Sample | None,
    expected: _Sample | None,
    total_actual: _Sample | None,
    total_expected: _Sample | None,
) -> float | None:
    if (
        actual is None
        or expected is None
        or total_actual is None
        or total_expected is None
    ):
        return None
    if total_actual.den is None:
        return actual.num - expected.num
    if total_actual.den and total_expected.den:
        return actual.num / total_actual.den - expected.num / total_expected.den
    return None


def drill_down(
    data: DrillDownInput, *, con: duckdb.DuckDBPyConnection, registry: Registry
) -> DrillDownOutput:
    compared = _compare(data, con, registry)
    scope = tuple(sorted(data.segment.items()))
    total = compared.pop(scope, None)
    ranked = []
    for row, current, baseline in compared.values():
        share = None
        if (
            total is not None
            and total[0].delta is not None
            and abs(total[0].delta) > EPSILON
        ):
            contribution = _contribution(current, baseline, total[1], total[2])
            if contribution is not None:
                share = contribution / total[0].delta
        ranked.append(
            DrillDownRow(**row.model_dump(), share_of_total_delta=share, focus=False)
        )
    ranked.sort(key=lambda row: (-abs(row.share_of_total_delta or 0), row.value))
    covered = 0.0
    for row in ranked:
        if (
            covered < 0.8
            and (share := row.share_of_total_delta) is not None
            and share > 0
        ):
            row.focus = True
            covered += share
    focus = [row for row in ranked if row.focus]
    summary = (
        f"{data.metric} by {data.dim}: {len(ranked)} segments, {len(focus)} in focus."
    )
    if focus:
        summary += f" {focus[0].value} leads; focused segments cover {covered:.0%} of the delta."
    return DrillDownOutput(rows=ranked, summary=summary[:300])


def compare_segments(
    data: CompareSegmentsInput, *, con: duckdb.DuckDBPyConnection, registry: Registry
) -> CompareSegmentsOutput:
    compared = _compare(data, con, registry)

    def select(value: str) -> SegmentComparison:
        key = tuple(sorted({**data.segment, data.dim: value}.items()))
        if key in compared:
            return compared[key][0]
        return SegmentComparison(
            value=value, actual=None, expected=None, delta=None, delta_pct=None, z=None
        )

    a, b = select(data.value_a), select(data.value_b)
    parts = [
        f"{row.value}: {row.delta_pct:+.1%}"
        if row.delta_pct is not None
        else f"{row.value}: no comparable change"
        for row in (a, b)
    ]
    return CompareSegmentsOutput(
        a=a, b=b, summary=f"{data.metric} — {', '.join(parts)}."[:300]
    )


def first_change(
    data: FirstChangeInput, *, con: duckdb.DuckDBPyConnection, registry: Registry
) -> FirstChangeOutput:
    stats = _Stats(con, registry, data.start - 29 * DAY, data.end)
    changes: dict[str, datetime | None] = {}
    for metric in data.metrics:
        result = _baseline_compare(
            BaselineCompareInput(
                name=metric, start=data.start, end=data.end, segment=data.segment
            ),
            stats,
        )
        changes[metric] = next(
            (
                a.ts
                for a, b in zip(result.points, result.points[1:], strict=False)
                if b.ts - a.ts == HOUR
                and a.z is not None
                and abs(a.z) >= 3
                and b.z is not None
                and abs(b.z) >= 3
            ),
            None,
        )
    detected = {metric: ts for metric, ts in changes.items() if ts is not None}
    summary = f"{len(detected)} of {len(changes)} metrics had 2 consecutive hours with |z| ≥ 3."
    if detected:
        metric = min(detected, key=lambda name: (detected[name], name))
        summary += f" First: {metric} at {detected[metric].isoformat()}."
    return FirstChangeOutput(changes=changes, summary=summary)
