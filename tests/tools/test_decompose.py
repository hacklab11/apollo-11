import pytest

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
