"""Compact model-facing JSON without changing the stored tool or graph evidence."""

import json
from typing import Any

_POINT_FIELDS = ("ts", "value", "expected", "lower", "upper", "completeness")


def _compact_lags(numbers: dict[str, Any], proposed_lag: int) -> None:
    evidence = numbers.get("evidence")
    if not isinstance(evidence, dict):
        return
    lags = evidence.get("lags")
    if not isinstance(lags, list) or len(lags) <= 2:
        return
    if not all(
        isinstance(row, dict)
        and isinstance(row.get("lag"), int)
        and isinstance(row.get("corr"), (float, int))
        for row in lags
    ):
        return
    nearby = [row for row in lags if abs(row["lag"] - proposed_lag) <= 6]
    peak = max(nearby, key=lambda row: abs(row["corr"]), default=None)
    evidence["tested_lags"] = len(evidence.pop("lags"))
    evidence["strongest_at_proposed_lag_plus_minus_6h"] = peak


def _compact(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _compact(item)
    elif isinstance(value, dict):
        edge = value.get("edge", value)
        if isinstance(edge, dict) and isinstance(edge.get("lag_hours"), int):
            numbers = value.get("numbers")
            if isinstance(numbers, dict):
                _compact_lags(numbers, edge["lag_hours"])
            if isinstance(evidence := edge.get("evidence"), dict):
                _compact_lags(evidence, edge["lag_hours"])
                if isinstance(numbers := evidence.get("numbers"), dict):
                    _compact_lags(numbers, edge["lag_hours"])

        points = value.get("points")
        if (
            isinstance(points, list)
            and len(points) > 24
            and all(
                isinstance(p, dict) and p.keys() == set(_POINT_FIELDS) for p in points
            )
        ):
            value["point_fields"] = _POINT_FIELDS
            value["points"] = [[p[key] for key in _POINT_FIELDS] for p in points]

        for item in value.values():
            _compact(item)


def compact_chat_context(content: str) -> str:
    if len(content) < 2048 or not content.startswith("{"):
        return content
    try:
        value, end = json.JSONDecoder().raw_decode(content)
    except ValueError:
        return content
    _compact(value)
    compact = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + content[end:]
    )
    return compact if len(compact) < len(content) else content
