import json
from pathlib import Path

import pytest

from eye.contracts_v2 import Edge, GraphView, MetricDef
from eye.graph.store import GraphStore


def _metric(name: str) -> MetricDef:
    return MetricDef(
        name=name,
        description=name,
        unit="eur",
        domain="finance",
        kind="sql",
        sql="SELECT 1",
        dimensions=[],
    )


def _edge(src: str, dst: str, type_: str = "formula", **kw: object) -> Edge:
    defaults = {"status": "validated", "confidence": 1.0, "evidence": {}}
    return Edge.model_validate(
        {
            "id": f"edg_{type_}_{src}_{dst}",
            "src": src,
            "dst": dst,
            "type": type_,
            "created_by": "bootstrap",
            **defaults,
            **kw,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    """contribution_margin = revenue - cost; driver -> revenue (causal); cost <-> driver."""
    graph = GraphStore(tmp_path / "graph.json", "hash")
    for name in ("contribution_margin", "revenue", "cost", "driver", "seller_hhi"):
        graph.add_metric(_metric(name))
    identity = "contribution_margin = revenue - cost"
    graph.add_edge(
        _edge(
            "revenue",
            "contribution_margin",
            identity=identity,
            evidence={"mean_share": 0.8},
        )
    )
    graph.add_edge(
        _edge(
            "cost",
            "contribution_margin",
            identity=identity,
            evidence={"mean_share": 0.4},
        )
    )
    seed = {"type_": "causal_hypothesis", "status": "hypothesis", "confidence": 0.5}
    graph.add_edge(_edge("driver", "revenue", **seed))
    graph.add_edge(_edge("cost", "driver", **seed))
    graph.add_edge(_edge("driver", "cost", **seed))
    return graph


def test_impact_weights_follow_spec_rules(store: GraphStore) -> None:
    assert store.impact_weight("contribution_margin") == 1.0
    assert store.impact_weight("revenue") == pytest.approx(0.8)
    assert store.impact_weight("cost") == pytest.approx(0.4)
    # max(0.5 * 0.5 * revenue, 0.5 * 0.5 * cost) = 0.2
    assert store.impact_weight("driver") == pytest.approx(0.2)
    assert store.impact_weight("seller_hhi") == 0.3


def test_product_terms_share_the_target_weight(store: GraphStore) -> None:
    store.add_metric(_metric("orders"))
    store.add_metric(_metric("per_order"))
    identity = "per_order = contribution_margin / orders"
    for src in ("contribution_margin", "orders"):
        store.add_edge(_edge(src, "per_order", identity=identity))
    assert store.impact_weight("orders") == 1.0
    assert store.impact_weight("per_order") == 1.0


def test_ecosystem_floor_reaches_its_causes(store: GraphStore) -> None:
    seed = {"type_": "causal_hypothesis", "status": "hypothesis", "confidence": 0.5}
    store.add_metric(_metric("listing_fee"))
    store.add_edge(_edge("listing_fee", "seller_hhi", **seed))
    assert store.impact_weight("listing_fee") == pytest.approx(0.25 * 0.3)


def test_rejected_product_term_loses_weight(store: GraphStore) -> None:
    store.add_metric(_metric("orders"))
    store.add_metric(_metric("per_order"))
    identity = "per_order = contribution_margin / orders"
    for src in ("contribution_margin", "orders"):
        store.add_edge(_edge(src, "per_order", identity=identity))
    store.set_edge_status("edg_formula_orders_per_order", "rejected", {})
    assert store.impact_weight("orders") == 0.0
    assert store.impact_weight("per_order") == 1.0


def test_rejected_edges_carry_no_weight(store: GraphStore) -> None:
    edge_id = "edg_formula_cost_contribution_margin"
    store.set_edge_status(edge_id, "rejected", {"why": "residual"})
    # cost is now reached only through driver: 0.25 * (0.25 * revenue 0.8)
    assert store.impact_weight("cost") == pytest.approx(0.05)
    rejected = next(e for e in store.edges() if e.id == edge_id)
    assert rejected.status == "rejected"
    assert rejected.evidence == {"mean_share": 0.4, "why": "residual"}


def test_navigation(store: GraphStore) -> None:
    assert [e.src for e in store.in_edges("contribution_margin")] == ["revenue", "cost"]
    assert [e.dst for e in store.out_edges("driver", {"causal_hypothesis"})] == [
        "revenue",
        "cost",
    ]
    assert store.out_edges("driver", {"formula"}) == []
    assert store.neighbors_upstream("contribution_margin") == ["cost", "revenue"]
    # nearest first, alphabetical within one hop distance
    assert store.neighbors_upstream("contribution_margin", depth=2) == [
        "cost",
        "revenue",
        "driver",
    ]
    assert store.path_exists("driver", "contribution_margin")
    assert not store.path_exists("contribution_margin", "driver")
    assert store.cycles_through("driver") == [["driver", "cost"]]
    assert store.cycles_through("seller_hhi") == []


def test_view_and_state(store: GraphStore) -> None:
    store.set_state("revenue", "anomaly")
    full = store.view()
    assert {n["id"] for n in full.nodes} == {
        "contribution_margin",
        "revenue",
        "cost",
        "driver",
        "seller_hhi",
    }
    node = next(n for n in full.nodes if n["id"] == "revenue")
    assert node == {
        "id": "revenue",
        "label": "Revenue",
        "domain": "finance",
        "unit": "eur",
        "state": "anomaly",
        "impact_weight": 0.8,
    }
    near = store.view("revenue", depth=1)
    assert {n["id"] for n in near.nodes} == {"revenue", "contribution_margin", "driver"}
    assert {e.id for e in near.edges} == {
        "edg_formula_revenue_contribution_margin",
        "edg_causal_hypothesis_driver_revenue",
    }
    with pytest.raises(ValueError):
        store.set_state("revenue", "on_fire")  # type: ignore[arg-type]


def test_every_change_saves_and_notifies(store: GraphStore) -> None:
    seen: list[GraphView] = []
    store.on_change(seen.append)
    store.set_state("cost", "watch")
    store.set_edge_status("edg_causal_hypothesis_driver_revenue", "validated", {})
    store.add_metric(_metric("extra"))
    store.add_edge(_edge("extra", "revenue", "causal_hypothesis", confidence=0.9))
    assert len(seen) == 4
    assert any(e.src == "extra" for e in seen[-1].edges)
    saved = GraphStore.load(store.path)
    assert saved.view() == store.view()
    assert json.loads(store.path.read_text())["registry_hash"] == "hash"


def test_add_edge_needs_known_metrics(store: GraphStore) -> None:
    with pytest.raises(ValueError, match="unknown metric"):
        store.add_edge(_edge("ghost", "revenue"))
    with pytest.raises(ValueError, match="unknown edge"):
        store.set_edge_status("edg_missing", "validated", {})
