from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from eye.registry.engine import MetricEngine
from eye.tools.drill import decompose
from eye.tools.identity import parse_identity


@pytest.mark.parametrize("text", ["y = a * b / c", "y = a + b - c", "y = a"])
def test_identity_parser(text: str) -> None:
    parsed = parse_identity(text)
    assert parsed.target == "y"
    assert parsed.terms[0].name == "a"
    assert parsed.evaluate({"a": 6, "b": 2, "c": 3}) == pytest.approx(
        4 if "*" in text else 5 if "+" in text else 6
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "y == a",
        "y = 2 * a",
        "y = a ** b",
        "y = a(b)",
        "y = a.b",
        "y = (a + b)",
        "y = a + b * c",
        "y = -a",
        "y = a; import os",
        "y = a = b",
    ],
)
def test_identity_rejects_unsupported_syntax(text: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        parse_identity(text)


def test_s5_attribution_and_empty() -> None:
    start = datetime(2026, 1, 8, tzinfo=UTC)
    old = {"fee_revenue": 1000, "marketing_cost": 100, "platform_discounts": 50}
    new = {"fee_revenue": 1020, "marketing_cost": 300, "platform_discounts": 200}
    old["contribution_margin"] = 850
    new["contribution_margin"] = 520

    class Engine(MetricEngine):
        def __init__(self) -> None:
            pass

        _registry = SimpleNamespace(
            identities=[
                SimpleNamespace(
                    text="contribution_margin = fee_revenue - marketing_cost - platform_discounts"
                )
            ]
        )

        def metric_window(
            self, name: str, a: datetime, b: datetime, segment: dict | None
        ) -> float | None:
            return (new if a >= start else old)[name]

    engine = Engine()
    result = decompose(engine, "contribution_margin", start, start + timedelta(hours=1))
    negative = sorted(result.contributions, key=lambda r: r.delta)[:2]
    assert {r.factor for r in negative} == {"marketing_cost", "platform_discounts"}
    assert sum(r.share for r in result.contributions) == pytest.approx(1)
    with pytest.raises(ValueError, match="no identity"):
        decompose(engine, "missing", start, start + timedelta(hours=1))
    new["fee_revenue"] = None
    with pytest.raises(ValueError, match="requires data"):
        decompose(engine, "contribution_margin", start, start + timedelta(hours=1))
