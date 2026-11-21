"""Strict, non-executable identity grammar shared by attribution and validation."""

import re
from typing import Literal

from pydantic import BaseModel


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
