"""Formula attribution and exact denominator-weighted shift-share."""

from datetime import datetime, timedelta
from math import log

from pydantic import BaseModel, Field

from eye.contracts import Contribution
from eye.registry.engine import MetricEngine
from eye.tools.identity import parse_identity, ratio_sums


class AttributionResult(BaseModel):
    contributions: list[Contribution]
    delta: float
    totals: dict[str, float] = Field(default_factory=dict)
    summary: str = Field(max_length=300)


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
