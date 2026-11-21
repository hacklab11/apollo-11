import json
from pathlib import Path

import pytest

from eye.graph.bootstrap import bootstrap, load_or_bootstrap, registry_hash
from eye.graph.store import GraphStore
from eye.registry.engine import MetricEngine
from eye.tools.identity import parse_identity
from eye.tools.stats import _graph_weights

RATIO_METRICS = ("search_ctr", "cart_rate", "feed_ctr", "checkout_conversion", "aov")


def _shape(store: GraphStore) -> list[tuple]:
    """Graph structure without float evidence: DuckDB sums differ in the last bits."""
    return [
        (e.id, e.src, e.dst, e.type, e.status, e.dimension, e.identity, e.confidence)
        for e in store.edges()
    ]


def _weights(store: GraphStore) -> dict[str, float]:
    return {m.name: m.impact_weight for m in store.metrics()}


@pytest.mark.parametrize("identity_id", [f"F{i}" for i in range(1, 9)])
def test_formula_edges_for_every_identity(
    graph: GraphStore, graph_engine: MetricEngine, identity_id: str
) -> None:
    registry = graph_engine._registry
    identity = next(i for i in registry.identities if i.id == identity_id)
    parsed = parse_identity(identity.text)
    edges = [e for e in graph.in_edges(parsed.target, {"formula"})]
    edges = [e for e in edges if e.evidence["identity_id"] == identity_id]
    assert sorted(e.src for e in edges) == sorted(t.name for t in parsed.terms)
    assert all(e.identity == identity.text for e in edges)
    names = [parsed.target, *(t.name for t in parsed.terms)]
    if pending := [n for n in names if registry.get(n).status == "pending"]:
        assert all(e.status == "ambiguous" for e in edges)
        pytest.xfail(f"Simulator does not populate identity inputs yet: {pending}")
    assert {e.status for e in edges} == {"validated"}
    assert all(e.evidence["residual_pct"] < 0.5 for e in edges)


def test_breakdown_edges_for_every_dimension(
    graph: GraphStore, graph_engine: MetricEngine
) -> None:
    registry = graph_engine._registry
    for metric in registry.metrics.values():
        edges = {e.dimension: e for e in graph.out_edges(metric.name, {"breakdown"})}
        assert sorted(edges) == sorted(metric.dimensions), metric.name
        assert all(e.src == e.dst == metric.name for e in edges.values())
        expected = "ambiguous" if metric.status == "pending" else "validated"
        assert {e.status for e in edges.values()} <= {expected}, metric.name
    for name in RATIO_METRICS:
        for edge in graph.out_edges(name, {"breakdown"}):
            assert edge.status == "validated", edge.id
            assert edge.evidence["residual_pct"] < 0.5


def test_seeds_are_unrefined_hypotheses(
    graph: GraphStore, graph_engine: MetricEngine
) -> None:
    seeds = [e for e in graph.edges() if e.type == "causal_hypothesis"]
    assert len(seeds) == len(graph_engine._registry.causal_seeds)
    assert {(e.status, e.confidence, e.created_by) for e in seeds} == {
        ("hypothesis", 0.5, "seed")
    }
    edge = next(e for e in seeds if e.id == "edg_causal_search_conversion_orders")
    assert {"corr", "best_lag", "n_points", "rationale"} <= set(edge.evidence)
    assert edge.evidence["n_points"] > 0


def test_cycles_through_in_stock_rate(graph: GraphStore) -> None:
    cycles = graph.cycles_through("in_stock_rate")
    assert ["in_stock_rate", "buybox_share"] in cycles
    assert all(c[0] == "in_stock_rate" and len(c) <= 6 for c in cycles)


def test_graph_json_round_trip(graph: GraphStore, graph_path: Path) -> None:
    raw = json.loads(graph_path.read_text())
    assert raw["registry_hash"] == graph.registry_hash
    loaded = GraphStore.load(graph_path)
    assert loaded.metrics() == graph.metrics()
    assert loaded.edges() == graph.edges()
    assert {e.id: e.status for e in loaded.edges()} == {
        e.id: e.status for e in graph.edges()
    }
    assert loaded.view() == graph.view()


def test_load_or_bootstrap_uses_registry_hash(
    graph: GraphStore, graph_engine: MetricEngine, tmp_path: Path
) -> None:
    path = tmp_path / "graph.json"
    graph.path, original = path, graph.path
    try:
        graph.save()
    finally:
        graph.path = original
    cached = load_or_bootstrap(graph_engine, path)
    assert cached.edges() == graph.edges()

    raw = json.loads(path.read_text())
    raw["registry_hash"] = "stale"
    raw["edges"] = []
    path.write_text(json.dumps(raw))
    rebuilt = load_or_bootstrap(graph_engine, path)
    assert rebuilt.registry_hash == registry_hash(graph_engine._registry)
    assert _shape(rebuilt) == _shape(graph)
    assert GraphStore.load(path).registry_hash == rebuilt.registry_hash


def test_bootstrap_is_deterministic(
    graph: GraphStore, graph_engine: MetricEngine
) -> None:
    again = bootstrap(graph_engine, None)
    assert _shape(again) == _shape(graph)
    assert _weights(again) == pytest.approx(_weights(graph))


def test_impact_weights(graph: GraphStore) -> None:
    assert graph.impact_weight("contribution_margin") == 1.0
    assert graph.impact_weight("orders") > 0
    for name in ("small_seller_gmv_share", "active_small_sellers", "seller_hhi"):
        assert graph.impact_weight(name) >= 0.3


def test_scan_tools_read_graph_weights(graph: GraphStore) -> None:
    weights = _graph_weights(graph.view())
    assert weights == {m.name: graph.impact_weight(m.name) for m in graph.metrics()}
