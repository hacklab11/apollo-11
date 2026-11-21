"""Deterministic graph bootstrap (06 §2 steps 1-4); LLM refinement of seeds is T10."""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from eye.contracts import Edge
from eye.graph.store import GRAPH_PATH, GraphStore
from eye.registry.engine import MetricEngine
from eye.registry.loader import CONFIG_DIR, CausalSeed, Identity, Registry
from eye.tools.identity import parse_identity
from eye.tools.validate import (
    _window,
    lagged_correlation,
    validate_breakdown,
    validate_identity,
)

REGISTRY_FILES = ("metrics.yaml", "identities.yaml", "causal_seeds.yaml")
SEED_CONFIDENCE = 0.5
SEED_MAX_LAG = 96

_log = logging.getLogger(__name__)


def registry_hash(registry: Registry, config_dir: str | Path = CONFIG_DIR) -> str:
    """Hash of the registry YAML files plus the pending metrics.

    Pending metrics have no data to validate against, so a graph built while some
    were pending is rebuilt once they become active.
    """
    digest = hashlib.sha256()
    for name in REGISTRY_FILES:
        digest.update(name.encode())
        digest.update((Path(config_dir) / name).read_bytes())
    pending = sorted(m.name for m in registry.metrics.values() if m.status == "pending")
    digest.update(json.dumps(pending).encode())
    return digest.hexdigest()


def load_or_bootstrap(
    engine: MetricEngine,
    path: Path = GRAPH_PATH,
    config_dir: str | Path = CONFIG_DIR,
    scenario_hours: list[datetime] | None = None,
) -> GraphStore:
    expected = registry_hash(engine._registry, config_dir)
    if path.exists():
        store = GraphStore.load(path)
        if store.registry_hash == expected:
            return store
        _log.info(json.dumps({"event": "graph_stale", "path": str(path)}))
    return bootstrap(engine, path, config_dir, scenario_hours)


def bootstrap(
    engine: MetricEngine,
    path: Path | None = GRAPH_PATH,
    config_dir: str | Path = CONFIG_DIR,
    scenario_hours: list[datetime] | None = None,
) -> GraphStore:
    registry = engine._registry
    # Built without a path so the file is written once, not after every edge.
    store = GraphStore(None, registry_hash(registry, config_dir))
    for metric in registry.metrics.values():
        store.add_metric(metric)
    for identity in registry.identities:
        for edge in _formula_edges(engine, identity, scenario_hours):
            store.add_edge(edge)
    for metric in registry.metrics.values():
        for dim in metric.dimensions:
            store.add_edge(_breakdown_edge(engine, metric.name, dim, scenario_hours))
    for seed in registry.causal_seeds:
        store.add_edge(_seed_edge(engine, seed, scenario_hours))
    store.path = path
    store.save()
    return store


def _pending(registry: Registry, names: list[str]) -> list[str]:
    return [n for n in names if registry.get(n).status == "pending"]


def _formula_edges(
    engine: MetricEngine, identity: Identity, scenario_hours: list[datetime] | None
) -> list[Edge]:
    parsed = parse_identity(identity.text)
    names = [parsed.target, *(t.name for t in parsed.terms)]
    shares: dict[str, float] = {}
    if pending := _pending(engine._registry, names):
        status, evidence = "ambiguous", {"pending_metrics": pending}
    else:
        result = validate_identity(engine, identity.text, scenario_hours=scenario_hours)
        median = result.evidence["median_residual"]
        p95 = result.evidence["p95_residual"]
        evidence = {
            "residual_pct": None if median is None else median * 100,
            "p95_residual_pct": None if p95 is None else p95 * 100,
            "n_points": result.evidence["n"],
            "skipped": result.evidence["skipped"],
        }
        if result.passed:
            status = "validated"
        else:
            status = "rejected"
            _log.warning(
                json.dumps(
                    {
                        "event": "seed_identity_rejected",
                        "identity": identity.id,
                        **evidence,
                    }
                )
            )
        if parsed.kind == "sum":
            shares = _mean_shares(engine, parsed.target, names[1:])
    return [
        Edge(
            id=f"edg_formula_{identity.id}_{term.name}",
            src=term.name,
            dst=parsed.target,
            type="formula",
            status=status,
            identity=identity.text,
            confidence=1.0 if status == "validated" else 0.0,
            evidence={
                "identity_id": identity.id,
                **evidence,
                **({"mean_share": shares[term.name]} if term.name in shares else {}),
            },
            created_by="bootstrap",
        )
        for term in parsed.terms
    ]


def _mean_shares(
    engine: MetricEngine,
    target: str,
    components: list[str],
) -> dict[str, float]:
    """|component mean| / |target mean| over the validation window (06 §4)."""
    start, end = _window(engine, None, None, [target, *components])
    total = engine.metric_window(target, start, end)
    if not total:
        return {}
    return {
        name: abs(value) / abs(total)
        for name in components
        if (value := engine.metric_window(name, start, end)) is not None
    }


def _breakdown_edge(
    engine: MetricEngine, metric: str, dim: str, scenario_hours: list[datetime] | None
) -> Edge:
    if pending := _pending(engine._registry, [metric]):
        status, confidence, evidence = "ambiguous", 0.0, {"pending_metrics": pending}
    else:
        try:
            result = validate_breakdown(
                engine, metric, dim, scenario_hours=scenario_hours
            )
        except ValueError as error:
            status, confidence, evidence = "ambiguous", 0.0, {"error": str(error)}
        else:
            residual = result.evidence["median_residual"]
            status = "validated" if result.passed else "rejected"
            confidence = 1.0 if result.passed else 0.0
            evidence = {
                "residual_pct": None if residual is None else residual * 100,
                "n_points": result.evidence["n"],
                "skipped": result.evidence["skipped"],
            }
    return Edge(
        id=f"edg_breakdown_{metric}_{dim}",
        src=metric,
        dst=metric,
        type="breakdown",
        status=status,
        dimension=dim,
        confidence=confidence,
        evidence=evidence,
        created_by="bootstrap",
    )


def _seed_edge(
    engine: MetricEngine, seed: CausalSeed, scenario_hours: list[datetime] | None
) -> Edge:
    evidence: dict = {"rationale": seed.rationale}
    if pending := _pending(engine._registry, [seed.src, seed.dst]):
        evidence["pending_metrics"] = pending
    else:
        result = lagged_correlation(
            engine, seed.src, seed.dst, SEED_MAX_LAG, scenario_hours=scenario_hours
        )
        evidence |= {
            "corr": result.corr,
            "best_lag": result.best_lag,
            "n_points": result.n,
        }
    return Edge(
        id=f"edg_causal_{seed.src}_{seed.dst}",
        src=seed.src,
        dst=seed.dst,
        type="causal_hypothesis",
        status="hypothesis",
        lag_hours=seed.lag_hours,
        confidence=SEED_CONFIDENCE,
        evidence=evidence,
        created_by="seed",
    )
