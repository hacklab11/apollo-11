import json
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from factories import ChatResponse, MetricDefinitionResponse, StatusResponse, TruthRow
from pydantic import TypeAdapter, ValidationError

from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Anomaly,
    Event,
    GraphView,
    Incident,
    MetricDef,
    ScenarioState,
    SentinelDecision,
    Series,
    TrailStep,
)

MOCK_MODELS: dict[str, Any] = {
    "status.json": StatusResponse,
    "graph.json": GraphView,
    "metrics.json": list[MetricDef],
    "metric_orders.json": Series,
    "metric_def_orders.json": MetricDefinitionResponse,
    "add_metric_result.json": AddMetricResult,
    "anomalies.json": list[SentinelDecision],
    "incidents.json": list[Incident],
    "incident_s7.json": Incident,
    "incident_s6.json": Incident,
    "trail_s7.json": list[TrailStep],
    "scenarios.json": list[ScenarioState],
    "chat_response.json": ChatResponse,
    "truth.json": list[TruthRow],
}


@pytest.mark.parametrize(("filename", "model"), MOCK_MODELS.items())
def test_mock_validates(repo_root: Path, filename: str, model: Any) -> None:
    TypeAdapter(model).validate_json(
        (repo_root / "tests/fixtures/web" / filename).read_bytes()
    )


def test_mock_file_inventory(repo_root: Path) -> None:
    assert {path.name for path in (repo_root / "tests/fixtures/web").iterdir()} == {
        *MOCK_MODELS,
        "events.jsonl",
    }


def test_event_stream_validates(repo_root: Path) -> None:
    lines = (
        (repo_root / "tests/fixtures/web/events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    events = [Event.model_validate_json(line) for line in lines]
    assert events
    assert events == sorted(events, key=lambda event: event.sim_ts)
    payload_models = {
        "anomaly": Anomaly,
        "sentinel_decision": SentinelDecision,
        "trail_step": TrailStep,
        "incident_upserted": Incident,
    }
    for event in events:
        if event.type in payload_models:
            payload_models[event.type].model_validate(event.payload)

    assert any(
        event.type == "trail_step" and event.payload["actor"] == "selleros"
        for event in events
    )
    assert {
        event.payload["id"] for event in events if event.type == "incident_upserted"
    } == {"inc_s6", "inc_s7"}


def test_orders_chart_matches_s7_anomaly(repo_root: Path) -> None:
    mocks = repo_root / "tests/fixtures/web"
    incident = Incident.model_validate_json((mocks / "incident_s7.json").read_bytes())
    series = Series.model_validate_json((mocks / "metric_orders.json").read_bytes())
    assert series.metric == incident.entry.metric
    assert len(series.points) == 48
    assert all(
        right.ts - left.ts == timedelta(hours=1)
        for left, right in pairwise(series.points)
    )
    point = next(p for p in series.points if p.ts == incident.entry.ts)
    assert point.value == incident.entry.actual
    assert point.expected == incident.entry.expected
    assert point.value < point.lower < point.expected < point.upper
    assert incident.entry.delta_pct == pytest.approx(
        incident.entry.delta / incident.entry.expected
    )


def test_graph_edges_reference_mock_metrics(repo_root: Path) -> None:
    mocks = repo_root / "tests/fixtures/web"
    metrics = json.loads((mocks / "metrics.json").read_bytes())
    assert all("sql" not in metric for metric in metrics)
    graph = GraphView.model_validate_json((mocks / "graph.json").read_bytes())
    nodes = {node["id"] for node in graph.nodes}
    assert len(graph.nodes) == len(nodes) == 12
    assert nodes == {metric["name"] for metric in metrics}
    assert {edge.type for edge in graph.edges} == {
        "formula",
        "breakdown",
        "causal_hypothesis",
    }
    for edge in graph.edges:
        assert edge.src in nodes and edge.dst in nodes
        if edge.type == "breakdown":
            assert edge.src == edge.dst and edge.dimension is not None


def test_incident_detail_and_trail_agree(repo_root: Path) -> None:
    mocks = repo_root / "tests/fixtures/web"
    incident = Incident.model_validate_json((mocks / "incident_s7.json").read_bytes())
    trail = TypeAdapter(list[TrailStep]).validate_json(
        (mocks / "trail_s7.json").read_bytes()
    )
    assert [step.n for step in trail] == list(range(1, 11))
    assert {step.investigation_id for step in trail} == set(incident.investigation_ids)
    step_ids = {step.id for step in trail}
    assert all(
        evidence.step_id in step_ids
        for hypothesis in incident.hypotheses
        for evidence in [*hypothesis.evidence_for, *hypothesis.evidence_against]
    )


@pytest.mark.parametrize(
    ("sql", "expression"),
    [(None, None), ("SELECT * FROM orders_hourly", "orders / sessions")],
)
def test_add_metric_requires_exactly_one_source(
    sql: str | None, expression: str | None
) -> None:
    with pytest.raises(ValidationError, match="exactly one of sql or expression"):
        AddMetricRequest(
            name="orders_per_session",
            description="Orders per session",
            sql=sql,
            expression=expression,
        )
