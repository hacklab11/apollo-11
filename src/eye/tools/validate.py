"""Deterministic graph evidence; scenario hours are supplied by the orchestrator."""

from datetime import datetime, timedelta
from itertools import product
from math import isfinite

import numpy as np
from pydantic import BaseModel, Field, validate_call

from eye.registry.engine import MetricEngine
from eye.tools.identity import parse_identity, ratio_sums


class ValidationResult(BaseModel):
    passed: bool
    evidence: dict
    summary: str = Field(max_length=300)


class CorrelationResult(BaseModel):
    best_lag: int | None
    corr: float | None
    n: int
    evidence: dict
    summary: str = Field(max_length=300)


def _window(
    engine: MetricEngine, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime]:
    if end is None:
        with engine._con.cursor() as cur:
            latest = cur.execute("SELECT max(ts) FROM partition_status").fetchone()[0]
        if latest is None:
            raise ValueError("cannot infer validation window without simulated history")
        end = latest + timedelta(hours=1)
    start = start or end - timedelta(days=14)
    if start >= end:
        raise ValueError("start must precede end")
    return start, end


def _points(
    engine: MetricEngine,
    name: str,
    start: datetime,
    end: datetime,
    segment: dict[str, str],
    excluded: set[datetime],
) -> dict[datetime, float]:
    return {
        p.ts: p.value
        for s in engine.query_metric(name, start, end, segment)
        for p in s.points
        if p.ts not in excluded
        and p.completeness >= 0.9
        and p.value is not None
        and isfinite(p.value)
    }


def _result(
    residuals: list[float], skipped: int, groups: list[dict], *, identity: bool
) -> ValidationResult:
    median = float(np.median(residuals)) if residuals else None
    p95 = float(np.percentile(residuals, 95)) if residuals else None
    passed = bool(residuals) and (
        median < 0.005 and p95 < 0.02 if identity else max(residuals) < 0.005
    )
    if identity:
        passed = passed and all(g["passed"] for g in groups if g["n"])
    return ValidationResult(
        passed=passed,
        evidence={
            "median_residual": median,
            "p95_residual": p95,
            "n": len(residuals),
            "skipped": skipped,
            "groups": groups,
        },
        summary=f"{len(residuals)} observations, {skipped} skipped; median {median}, p95 {p95}; passed={passed}"[
            :300
        ],
    )


@validate_call(config={"arbitrary_types_allowed": True})
def validate_identity(
    engine: MetricEngine,
    identity: str,
    start: datetime | None = None,
    end: datetime | None = None,
    segments: list[dict[str, str]] | None = None,
    scenario_hours: list[datetime] | None = None,
) -> ValidationResult:
    start, end = _window(engine, start, end)
    parsed = parse_identity(identity)
    names = {parsed.target, *(t.name for t in parsed.terms)}
    shared = set.intersection(*(set(engine._registry.get(n).dimensions) for n in names))
    excluded = set(scenario_hours or [])
    selections = [{}]
    if segments is None:
        dims = sorted(shared)
        values = []
        for dim in dims:
            values.append(
                sorted(
                    {
                        s.segment[dim]
                        for s in engine.query_metric(
                            parsed.target, start, end, group_by=[dim]
                        )
                    }
                )
            )
        selections += (
            [dict(zip(dims, vs, strict=True)) for vs in product(*values)]
            if dims
            else []
        )
    else:
        selections += [s for s in segments if s]
    residuals, groups, skipped = [], [], 0
    for segment in selections:
        if not set(segment) <= shared:
            raise ValueError("identity segments must use shared dimensions")
        data = {n: _points(engine, n, start, end, segment, excluded) for n in names}
        stamps = set.intersection(*(set(v) for v in data.values()))
        local = []
        skipped += max(0, int((end - start).total_seconds() // 3600) - len(stamps))
        for ts in sorted(stamps):
            lhs = data[parsed.target][ts]
            try:
                rhs = parsed.evaluate({n: data[n][ts] for n in names})
                residual = (
                    abs(lhs - rhs) / abs(lhs)
                    if lhs
                    else (0.0 if rhs == 0 else float("inf"))
                )
            except (ZeroDivisionError, OverflowError):
                skipped += 1
                continue
            local.append(residual)
        residuals.extend(local)
        groups.append(
            {
                "segment": segment,
                "n": len(local),
                "max_residual": max(local) if local else None,
                "passed": bool(local)
                and float(np.median(local)) < 0.005
                and float(np.percentile(local, 95)) < 0.02,
            }
        )
    return _result(residuals, skipped, groups, identity=True)


@validate_call(config={"arbitrary_types_allowed": True})
def validate_breakdown(
    engine: MetricEngine,
    metric: str,
    dim: str,
    start: datetime | None = None,
    end: datetime | None = None,
    scenario_hours: list[datetime] | None = None,
) -> ValidationResult:
    start, end = _window(engine, start, end)
    definition = engine._metric(metric, [dim])
    excluded = set(scenario_hours or [])
    total = _points(engine, metric, start, end, {}, excluded)
    series = engine.query_metric(metric, start, end, group_by=[dim])
    valid = {
        p.ts
        for s in series
        for p in s.points
        if p.completeness < 0.9 or p.value is None
    }
    sums: dict[datetime, float] = {}
    if (
        engine._registry.has_den.get(metric)
        or definition.unit == "ratio"
        or (definition.expression and "/" in definition.expression)
    ):
        raw = ratio_sums(engine, metric, start, end, {}, [dim], by_ts=True)
        nd: dict[datetime, list[float]] = {}
        for points in raw.values():
            for ts, (num, den) in points.items():
                pair = nd.setdefault(ts, [0.0, 0.0])
                pair[0] += num or 0
                pair[1] += den or 0
        sums = {ts: n / d for ts, (n, d) in nd.items() if d}
    else:
        for s in series:
            for p in s.points:
                if p.value is not None:
                    sums[p.ts] = sums.get(p.ts, 0) + p.value
    residuals = [
        abs(v - sums[ts]) / abs(v) if v else (0.0 if sums[ts] == 0 else float("inf"))
        for ts, v in total.items()
        if ts in sums and ts not in valid
    ]
    return _result(
        residuals,
        int((end - start).total_seconds() // 3600) - len(residuals),
        [],
        identity=False,
    )


@validate_call(config={"arbitrary_types_allowed": True})
def lagged_correlation(
    engine: MetricEngine,
    src: str,
    dst: str,
    max_lag: int = 96,
    start: datetime | None = None,
    end: datetime | None = None,
    segment: dict[str, str] | None = None,
    scenario_hours: list[datetime] | None = None,
) -> CorrelationResult:
    if max_lag < 0:
        raise ValueError("max_lag must be nonnegative")
    start, end = _window(engine, start, end)
    excluded = set(scenario_hours or [])

    def differences(name: str) -> dict[datetime, float]:
        raw = _points(
            engine,
            name,
            start - timedelta(days=7, hours=1),
            end,
            segment or {},
            excluded,
        )
        seasonal = {}
        for ts, value in raw.items():
            history = [
                raw[ts - timedelta(days=d)]
                for d in range(1, 8)
                if ts - timedelta(days=d) in raw
            ]
            if history and (median := float(np.median(history))) != 0:
                seasonal[ts] = value / median
        return {
            ts: value - seasonal[ts - timedelta(hours=1)]
            for ts, value in seasonal.items()
            if ts >= start and ts - timedelta(hours=1) in seasonal
        }

    a, b = differences(src), differences(dst)
    evidence = []
    for lag in range(max_lag + 1):
        pairs = [
            (v, b[ts + timedelta(hours=lag)])
            for ts, v in a.items()
            if ts + timedelta(hours=lag) in b
        ]
        if len(pairs) < 3:
            continue
        x, y = np.array(pairs).T
        if np.std(x) == 0 or np.std(y) == 0:
            continue
        corr = float(np.corrcoef(x, y)[0, 1])
        if isfinite(corr):
            evidence.append({"lag": lag, "corr": corr, "n": len(pairs)})
    best = max(evidence, key=lambda row: abs(row["corr"])) if evidence else None
    return CorrelationResult(
        best_lag=best["lag"] if best else None,
        corr=best["corr"] if best else None,
        n=best["n"] if best else 0,
        evidence={"lags": evidence, "scenario_hours": len(excluded)},
        summary=f"Best lag {best['lag']}h, corr {best['corr']:.6g}, n={best['n']}"
        if best
        else "0 usable correlations",
    )
