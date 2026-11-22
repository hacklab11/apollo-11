"""Typed metric graph: networkx storage, persistence, navigation and impact weights."""

import json
import os
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal, get_args

import networkx as nx

from eye.contracts_v2 import Edge, EdgeStatus, EdgeType, GraphView, MetricDef
from eye.tools.identity import parse_identity

GRAPH_PATH = Path("data/graph.json")
NodeState = Literal["normal", "watch", "anomaly", "incident"]
TOP_METRIC = "contribution_margin"
# Small-seller health is a long-term business risk the short-term margin formula does not show.
ECOSYSTEM_METRICS = frozenset(
    {"small_seller_gmv_share", "active_small_sellers", "seller_hhi"}
)
ECOSYSTEM_FLOOR = 0.3
MAX_WEIGHT_PASSES = 10
_WALK_TYPES: frozenset[EdgeType] = frozenset({"formula", "causal_hypothesis"})


class GraphStore:
    def __init__(self, path: Path | None = GRAPH_PATH, registry_hash: str = "") -> None:
        self.path = path
        self.registry_hash = registry_hash
        self._graph = nx.MultiDiGraph()
        self._listeners: list[Callable[[GraphView], None]] = []

    @classmethod
    def load(cls, path: Path = GRAPH_PATH) -> "GraphStore":
        raw = json.loads(path.read_text(encoding="utf-8"))
        store = cls(path, raw["registry_hash"])
        for node in raw["nodes"]:
            state = node.pop("state")
            store._graph.add_node(
                node["name"], metric=MetricDef.model_validate(node), state=state
            )
        for edge in raw["edges"]:
            store._put_edge(Edge.model_validate(edge))
        return store

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "registry_hash": self.registry_hash,
            "nodes": [
                {**data["metric"].model_dump(mode="json"), "state": data["state"]}
                for _, data in self._graph.nodes(data=True)
            ],
            "edges": [e.model_dump(mode="json") for e in self.edges()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def on_change(self, callback: Callable[[GraphView], None]) -> None:
        self._listeners.append(callback)

    def add_metric(self, m: MetricDef) -> None:
        state = (
            self._graph.nodes[m.name]["state"] if m.name in self._graph else "normal"
        )
        self._graph.add_node(m.name, metric=m.model_copy(), state=state)
        self._changed()

    def add_edge(self, e: Edge) -> None:
        for name in (e.src, e.dst):
            if name not in self._graph:
                raise ValueError(f"unknown metric: {name}")
        if (old := self._find(e.id)) is not None:
            self._graph.remove_edge(old.src, old.dst, key=old.id)
        self._put_edge(e.model_copy(deep=True))
        self._changed()

    def set_edge_status(self, edge_id: str, status: EdgeStatus, evidence: dict) -> None:
        if status not in get_args(EdgeStatus):
            raise ValueError(f"unknown edge status: {status}")
        edge = self._find(edge_id)
        if edge is None:
            raise ValueError(f"unknown edge: {edge_id}")
        edge.status = status
        edge.evidence = {**edge.evidence, **evidence}
        self._changed()

    def set_state(self, node: str, state: NodeState) -> None:
        if state not in get_args(NodeState):
            raise ValueError(f"unknown node state: {state}")
        self._node(node)["state"] = state
        self._changed(weights=False)

    def metric(self, node: str) -> MetricDef:
        return self._node(node)["metric"]

    def metrics(self) -> list[MetricDef]:
        return [data["metric"] for _, data in self._graph.nodes(data=True)]

    def edges(self) -> list[Edge]:
        return [e for *_, e in self._graph.edges(keys=True, data="edge")]

    def in_edges(self, node: str, types: set[EdgeType] | None = None) -> list[Edge]:
        self._node(node)
        return [
            e
            for *_, e in self._graph.in_edges(node, keys=True, data="edge")
            if types is None or e.type in types
        ]

    def out_edges(self, node: str, types: set[EdgeType] | None = None) -> list[Edge]:
        self._node(node)
        return [
            e
            for *_, e in self._graph.out_edges(node, keys=True, data="edge")
            if types is None or e.type in types
        ]

    def neighbors_upstream(self, node: str, depth: int = 1) -> list[str]:
        """Causes and components within `depth` hops, nearest first; rejected edges are ignored."""
        walk = self._walk_graph()
        self._node(node)
        seen, found, queue = {node}, [], deque([(node, 0)])
        while queue:
            current, level = queue.popleft()
            if level == depth:
                continue
            for parent in sorted(walk.predecessors(current)):
                if parent not in seen:
                    seen.add(parent)
                    found.append(parent)
                    queue.append((parent, level + 1))
        return found

    def path_exists(self, src: str, dst: str) -> bool:
        self._node(src)
        self._node(dst)
        return nx.has_path(self._walk_graph(), src, dst)

    def cycles_through(self, node: str, max_len: int = 6) -> list[list[str]]:
        """Simple cycles on causal and formula edges, each starting at `node`."""
        self._node(node)
        cycles = []
        for cycle in nx.simple_cycles(self._walk_graph(), length_bound=max_len):
            if node in cycle:
                start = cycle.index(node)
                cycles.append(cycle[start:] + cycle[:start])
        return sorted(cycles, key=lambda c: (len(c), c))

    def view(self, node: str | None = None, depth: int | None = None) -> GraphView:
        """Whole graph, or the nodes within `depth` hops (default 1) of `node` either way."""
        if node is None:
            names = set(self._graph)
        else:
            self._node(node)
            undirected = self._walk_graph().to_undirected(as_view=True)
            names = set(
                nx.single_source_shortest_path_length(
                    undirected, node, cutoff=1 if depth is None else depth
                )
            )
        nodes = [
            {
                "id": m.name,
                "label": m.name.replace("_", " ").capitalize(),
                "domain": m.domain,
                "unit": m.unit,
                "state": self._graph.nodes[m.name]["state"],
                "impact_weight": m.impact_weight,
            }
            for m in self.metrics()
            if m.name in names
        ]
        edges = [e for e in self.edges() if e.src in names and e.dst in names]
        return GraphView(nodes=nodes, edges=edges)

    def impact_weight(self, metric: str) -> float:
        return self.metric(metric).impact_weight

    def recompute_impact_weights(self) -> None:
        """Weights per 06 §4, propagated from the top metric until stable (max 10 passes).

        Every term of a product identity has elasticity 1 with respect to the others
        (`cm = cpo * orders` as well as `cpo = cm / orders`), so all its terms share the
        largest weight among them; a sum component gets its mean share of the target.
        """
        # Floors seed the walk so causes of ecosystem metrics inherit their weight.
        weights = {
            name: ECOSYSTEM_FLOOR if name in ECOSYSTEM_METRICS else 0.0
            for name in self._graph
        }
        if TOP_METRIC in weights:
            weights[TOP_METRIC] = 1.0
        live = [e for e in self.edges() if e.status != "rejected" and e.src != e.dst]
        products: dict[str, set[str]] = {}
        for edge in live:
            if edge.type == "formula" and edge.identity:
                parsed = parse_identity(edge.identity)
                if parsed.kind == "product":
                    products.setdefault(edge.identity, {edge.dst}).add(edge.src)
        for _ in range(MAX_WEIGHT_PASSES):
            changed = False
            for name, weight in _passes(weights, live, products):
                if name != TOP_METRIC and weight > weights[name] + 1e-12:
                    weights[name] = weight
                    changed = True
            if not changed:
                break
        for name, weight in weights.items():
            self._graph.nodes[name]["metric"].impact_weight = round(weight, 6)

    def _find(self, edge_id: str) -> Edge | None:
        return next((e for e in self.edges() if e.id == edge_id), None)

    def _put_edge(self, e: Edge) -> None:
        self._graph.add_edge(e.src, e.dst, key=e.id, edge=e)

    def _node(self, node: str) -> dict:
        try:
            return self._graph.nodes[node]
        except KeyError:
            raise ValueError(f"unknown metric: {node}") from None

    def _walk_graph(self) -> nx.DiGraph:
        walk = nx.DiGraph()
        walk.add_nodes_from(self._graph)
        walk.add_edges_from(
            (e.src, e.dst)
            for e in self.edges()
            if e.type in _WALK_TYPES and e.status != "rejected" and e.src != e.dst
        )
        return walk

    def _changed(self, *, weights: bool = True) -> None:
        if weights:
            self.recompute_impact_weights()
        self.save()
        if self._listeners:
            view = self.view()
            for callback in self._listeners:
                callback(view)


def _passes(
    weights: dict[str, float], edges: Iterable[Edge], products: dict[str, set[str]]
) -> Iterable[tuple[str, float]]:
    for edge in edges:
        target = weights[edge.dst]
        if edge.type == "causal_hypothesis":
            yield edge.src, edge.confidence * 0.5 * target
        elif edge.type == "formula" and edge.identity not in products:
            yield edge.src, edge.evidence.get("mean_share", 1.0) * target
    for terms in products.values():
        known = [t for t in terms if t in weights]
        top = max((weights[t] for t in known), default=0.0)
        for term in known:
            yield term, top
