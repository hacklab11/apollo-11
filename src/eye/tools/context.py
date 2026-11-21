"""Read pipeline metadata, operational events, and planned effects."""

import json
from collections import defaultdict
from datetime import datetime
from typing import Literal

import duckdb

from eye.config import SentinelSettings
from eye.registry.loader import Registry
from eye.tools.models import (
    BaselineCompareInput,
    CheckFreshnessInput,
    CheckFreshnessOutput,
    Event,
    FreshnessPoint,
    ListEventsInput,
    ListEventsOutput,
    PlannedEffectCheckInput,
    PlannedEffectCheckOutput,
)
from eye.tools.stats import HOUR, _bare_tables, _hours, _partitions, baseline_compare


def check_freshness(
    data: CheckFreshnessInput,
    *,
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: SentinelSettings | None = None,
) -> CheckFreshnessOutput:
    settings = settings or SentinelSettings()
    if data.metric is not None:
        metric = registry.get(data.metric)
        if data.segment.keys() - set(metric.dimensions):
            raise ValueError(
                f"{metric.name}: invalid dimensions for slice {data.segment}"
            )
        tables = _bare_tables(registry.source_tables[data.metric])
    else:
        tables = _bare_tables(data.tables)
    country_filter = data.segment.get("country")
    counts: dict[tuple[datetime, str], list[int]] = defaultdict(lambda: [0, 0])
    late = set()
    for table, country, ts, got, expected, loaded_at in _partitions(
        con, tables, data.start, data.end
    ):
        if country_filter is not None and country != country_filter:
            continue
        sums = counts[ts, table]
        sums[0] += got or 0
        sums[1] += expected or 0
        if loaded_at is not None and loaded_at - ts > 2 * HOUR:
            late.add(ts)
    points = []
    for ts in _hours(data.start, data.end):
        completeness = min(
            (
                min(1.0, got / expected)
                for table in tables
                if (sums := counts.get((ts, table)))
                and (expected := sums[1]) > 0
                and (got := sums[0]) >= 0
            ),
            default=1.0,
        )
        points.append(FreshnessPoint(ts=ts, completeness=completeness))
    incomplete = sum(point.completeness < settings.min_completeness for point in points)
    verdict: Literal["ok", "late", "incomplete"] = (
        "incomplete" if incomplete else "late" if late else "ok"
    )
    return CheckFreshnessOutput(
        points=points,
        late_hours=sorted(late),
        verdict=verdict,
        summary=f"Freshness {verdict}: {len(points)}h, {incomplete} incomplete and {len(late)} late.",
    )


def _change_event(
    id_: str,
    ts: datetime,
    system: str,
    kind: str,
    planned: bool | None,
    scope: str | None,
    description: str,
    effect: str | None,
) -> Event:
    return Event(
        id=id_,
        ts=ts,
        source="change_log",
        system=system,
        kind=kind,
        planned=bool(planned),
        scope=(json.loads(scope) if scope else None) or {},
        description=description,
        expected_effect=(json.loads(effect) if effect else None) or {},
    )


def list_events(
    data: ListEventsInput, *, con: duckdb.DuckDBPyConnection
) -> ListEventsOutput:
    with con.cursor() as cur:
        changes = cur.execute(
            "SELECT id, ts, system, kind, planned, scope, description, expected_effect "
            "FROM change_log WHERE ts >= ? AND ts < ?",
            [data.start, data.end],
        ).fetchall()
        external = cur.execute(
            "SELECT id, ts, provider, status, description FROM external_status "
            "WHERE ts >= ? AND ts < ?",
            [data.start, data.end],
        ).fetchall()
    events = [_change_event(*row) for row in changes]
    events.extend(
        Event(
            id=id_,
            ts=ts,
            source="external_status",
            system=provider,
            kind="external_status",
            planned=False,
            status=status,
            description=description,
        )
        for id_, ts, provider, status, description in external
    )
    filtered = []
    for event in events:
        if data.system is not None and event.system != data.system:
            continue
        if data.kind is not None and event.kind != data.kind:
            continue
        if data.planned is not None and event.planned != data.planned:
            continue
        country = event.scope.get("country")
        if data.country is not None and country is not None and country != data.country:
            if not isinstance(country, list) or data.country not in country:
                continue
        filtered.append(event)
    filtered.sort(key=lambda event: (event.ts, event.source, event.id))
    planned = sum(event.planned for event in filtered)
    external_count = sum(event.source == "external_status" for event in filtered)
    return ListEventsOutput(
        events=filtered,
        summary=f"{len(filtered)} events: {planned} planned, {external_count} external status updates.",
    )


def planned_effect_check(
    data: PlannedEffectCheckInput, *, con: duckdb.DuckDBPyConnection, registry: Registry
) -> PlannedEffectCheckOutput:
    with con.cursor() as cur:
        row = cur.execute(
            "SELECT id, ts, system, kind, planned, scope, description, expected_effect "
            "FROM change_log WHERE id = ?",
            [data.event_id],
        ).fetchone()
    event = _change_event(*row) if row else None
    if event is None or not event.planned or data.metric not in event.expected_effect:
        raise ValueError(
            f"no planned effect for {data.metric} in event {data.event_id}"
        )
    effect = event.expected_effect[data.metric]
    percentage = isinstance(effect, str) and effect.strip().endswith("%")
    planned = float(effect.strip()[:-1]) / 100 if percentage else float(effect)
    baseline = baseline_compare(
        BaselineCompareInput(
            name=data.metric, start=data.start, end=data.end, segment=data.segment
        ),
        con=con,
        registry=registry,
    )
    observed = baseline.delta
    if percentage:
        observed = (
            baseline.actual / baseline.expected - 1
            if baseline.actual is not None and baseline.expected
            else None
        )
    share = 0.0
    verdict: Literal["matches", "exceeds", "below", "opposite"]
    if observed is None:
        verdict = "below"
    elif observed * planned < 0:
        verdict = "opposite"
    else:
        if observed * planned > 0:
            share = min(1.0, planned / observed)
        if abs(observed - planned) <= 0.25 * abs(planned):
            verdict = "matches"
        else:
            verdict = "exceeds" if abs(observed) > abs(planned) else "below"
    fmt = "+.1%" if percentage else "+.4g"
    actual = format(observed, fmt) if observed is not None else "unavailable"
    return PlannedEffectCheckOutput(
        planned_effect=planned,
        observed_effect=observed,
        explained_share=share,
        verdict=verdict,
        summary=f"{data.metric}: {verdict}; planned {planned:{fmt}}, observed {actual}; {share:.0%} explained.",
    )
