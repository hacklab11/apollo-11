"""Strict, non-executable identity grammar shared by attribution and validation."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from eye.registry.engine import MetricEngine, Sums


class IdentityTerm(BaseModel):
    name: str
    exponent: Literal[-1, 1] = 1
    sign: Literal[-1, 1] = 1


class ParsedIdentity(BaseModel):
    target: str
    kind: Literal["sum", "product"]
    terms: list[IdentityTerm]

    def evaluate(self, values: dict[str, float]) -> float:
        if self.kind == "sum":
            return sum(t.sign * values[t.name] for t in self.terms)
        result = 1.0
        for term in self.terms:
            result *= values[term.name] ** term.exponent
        return result


def parse_identity(text: str) -> ParsedIdentity:
    """Accept a flat sum/difference or a left-associative product/quotient."""
    name = r"[a-z][a-z0-9_]*"
    match = re.fullmatch(rf"\s*({name})\s*=\s*({name}(?:\s*[+*/-]\s*{name})*)\s*", text)
    if match is None:
        raise ValueError("identity must be target = names joined by + -, or * / only")
    target, expression = match.groups()
    tokens = re.findall(rf"{name}|[+*/-]", expression)
    operators = tokens[1::2]
    additive = any(op in "+-" for op in operators)
    if additive and any(op in "*/" for op in operators):
        raise ValueError("identity cannot mix additive and multiplicative operators")
    terms = [IdentityTerm(name=tokens[0])]
    for op, metric in zip(operators, tokens[2::2], strict=True):
        terms.append(
            IdentityTerm(
                name=metric,
                sign=-1 if op == "-" else 1,
                exponent=-1 if op == "/" else 1,
            )
        )
    return ParsedIdentity(
        target=target, kind="sum" if additive else "product", terms=terms
    )


def ratio_sums(
    engine: MetricEngine,
    name: str,
    start: datetime,
    end: datetime,
    segment: dict[str, str],
    group: list[str],
    *,
    by_ts: bool,
) -> Sums:
    metric = engine._metric(name, [*segment, *group])
    if engine._registry.has_den.get(name):
        return engine._sums(metric, start, end, segment, group, by_ts=by_ts)
    if metric.kind != "formula" or not metric.expression:
        raise ValueError("ratio requires numerator and denominator")
    identity = parse_identity(f"{name} = {metric.expression}")
    if len(identity.terms) != 2 or identity.terms[1].exponent != -1:
        raise ValueError("ratio formula must be numerator / denominator")
    num, den = [engine._registry.get(t.name) for t in identity.terms]
    if (
        engine._registry.has_den.get(num.name)
        or engine._registry.has_den.get(den.name)
        or num.kind != "sql"
        or den.kind != "sql"
    ):
        raise ValueError("ratio formula components must be additive SQL metrics")
    a = engine._sums(num, start, end, segment, group, by_ts=by_ts)
    b = engine._sums(den, start, end, segment, group, by_ts=by_ts)
    return {
        key: {
            ts: (a.get(key, {}).get(ts, (0, None))[0], pair[0])
            for ts, pair in points.items()
        }
        for key, points in b.items()
    }
