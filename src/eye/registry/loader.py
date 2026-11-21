"""Load the metric registry from YAML and check it against the DuckDB schema."""

import ast
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import get_args

import duckdb
import yaml
from pydantic import BaseModel, Field

from eye.contracts import Dimension, MetricDef

CONFIG_DIR = Path("config")
DIMENSIONS: frozenset[str] = frozenset(get_args(Dimension))
HIDDEN_TABLES = frozenset({"scenario_log"})

_log = logging.getLogger(__name__)
_IDENT = r'"?[A-Za-z_]\w*"?'
_TABLE_REF = re.compile(rf"\b(?:FROM|JOIN)\s+((?:{_IDENT}\.)*{_IDENT})", re.IGNORECASE)
_CTE_NAME = re.compile(
    rf"({_IDENT})\s+AS\s*(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.IGNORECASE
)
_METRIC_NAME = re.compile(r"[a-z][a-z0-9_]*")
_FORMULA_NODES = frozenset(
    {ast.Expression, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Load}
)


class Identity(BaseModel):
    id: str
    text: str


class CausalSeed(BaseModel):
    src: str
    dst: str
    lag_hours: int = Field(ge=0)
    rationale: str


@dataclass(frozen=True)
class Registry:
    metrics: dict[str, MetricDef]
    # SQL metrics: tables read by the SQL; formula metrics: the union over inputs.
    source_tables: dict[str, tuple[str, ...]]
    has_den: dict[str, bool]
    identities: list[Identity]
    causal_seeds: list[CausalSeed]

    def get(self, name: str) -> MetricDef:
        try:
            return self.metrics[name]
        except KeyError:
            raise ValueError(f"unknown metric: {name}") from None

    @property
    def active(self) -> list[MetricDef]:
        return [m for m in self.metrics.values() if m.status == "active"]


def formula_inputs(expression: str) -> list[str]:
    """Metric names used by a formula; only names, + - * / and parentheses are allowed."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ValueError(f"invalid formula expression: {expression!r}") from None
    names: list[ast.Name] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node)
        elif type(node) not in _FORMULA_NODES:
            raise ValueError(
                f"formula may only use metric names, + - * / and parentheses: {expression!r}"
            )
    names.sort(key=lambda n: n.col_offset)
    return list(dict.fromkeys(n.id for n in names))


def source_tables(sql: str) -> tuple[str, ...]:
    """Tables after FROM or JOIN in order of first use, schema-qualified as written.

    Unqualified names that match a CTE are not physical tables and are skipped.
    """
    ctes = {name.replace('"', "").lower() for name in _CTE_NAME.findall(sql)}
    names = (ref.replace('"', "").lower() for ref in _TABLE_REF.findall(sql))
    return tuple(dict.fromkeys(name for name in names if name not in ctes))


def load_registry(
    con: duckdb.DuckDBPyConnection, config_dir: str | Path = CONFIG_DIR
) -> Registry:
    """Parse and check the YAML files; metrics with no data yet become `pending`."""
    config_dir = Path(config_dir)
    metrics = _read_metrics(config_dir / "metrics.yaml")
    tables: dict[str, tuple[str, ...]] = {}
    has_den: dict[str, bool] = {}
    with con.cursor() as cur:
        existing = {
            name
            for schema, table in cur.execute(
                "SELECT schema_name, table_name FROM duckdb_tables()"
            ).fetchall()
            for name in (table, f"{schema}.{table}")
        }

    for metric in metrics.values():
        if metric.kind != "sql":
            continue
        assert metric.sql is not None
        tables[metric.name] = source_tables(metric.sql)
        if absent := [t for t in tables[metric.name] if t not in existing]:
            metrics[metric.name] = _pending(
                metric, f"missing tables: {', '.join(absent)}"
            )
            continue
        has_den[metric.name] = _check_columns(con, metric)
        if reason := _missing_data(con, metric, tables[metric.name]):
            metrics[metric.name] = _pending(metric, reason)

    # Inputs are resolved in dependency order, so a formula over a pending metric is pending too.
    for name in _formula_order(metrics):
        metric = metrics[name]
        assert metric.expression is not None
        inputs = formula_inputs(metric.expression)
        tables[name] = tuple(
            dict.fromkeys(table for i in inputs for table in tables[i])
        )
        if pending := [i for i in inputs if metrics[i].status == "pending"]:
            metrics[name] = _pending(metric, f"inputs pending: {', '.join(pending)}")

    identities = [
        Identity.model_validate(item)
        for item in _read_list(config_dir / "identities.yaml")
    ]
    for identity in identities:
        target, sep, expression = identity.text.partition("=")
        if not sep:
            raise ValueError(f"identity {identity.id} has no '='")
        for name in [target.strip(), *_METRIC_NAME.findall(expression)]:
            if name not in metrics:
                raise ValueError(f"identity {identity.id}: unknown metric {name}")

    seeds = [
        CausalSeed.model_validate(item)
        for item in _read_list(config_dir / "causal_seeds.yaml")
    ]
    for seed in seeds:
        for name in (seed.src, seed.dst):
            if name not in metrics:
                raise ValueError(
                    f"causal seed {seed.src}->{seed.dst}: unknown metric {name}"
                )

    return Registry(metrics, tables, has_den, identities, seeds)


def _read_list(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError(f"{path}: expected a YAML list")
    return raw


def _read_metrics(path: Path) -> dict[str, MetricDef]:
    metrics: dict[str, MetricDef] = {}
    for item in _read_list(path):
        metric = MetricDef.model_validate(item)
        if metric.name in metrics:
            raise ValueError(f"duplicate metric name: {metric.name}")
        if len(set(metric.dimensions)) != len(metric.dimensions):
            raise ValueError(f"{metric.name}: duplicate dimensions")
        if metric.kind == "sql":
            if not metric.sql or metric.expression:
                raise ValueError(f"{metric.name}: kind sql needs sql and no expression")
            # Any mention is refused, so quoting or qualifying the name cannot get past it.
            if hidden := [t for t in HIDDEN_TABLES if t in metric.sql.lower()]:
                raise ValueError(f"{metric.name}: may not read {', '.join(hidden)}")
        elif not metric.expression or metric.sql:
            raise ValueError(f"{metric.name}: kind formula needs expression and no sql")
        metrics[metric.name] = metric

    for metric in metrics.values():
        if metric.kind != "formula":
            continue
        assert metric.expression is not None
        inputs = formula_inputs(metric.expression)
        if unknown := [name for name in inputs if name not in metrics]:
            raise ValueError(f"{metric.name}: unknown metrics {', '.join(unknown)}")
        allowed = set.intersection(*(set(metrics[i].dimensions) for i in inputs))
        if extra := [d for d in metric.dimensions if d not in allowed]:
            raise ValueError(
                f"{metric.name}: dimensions {', '.join(extra)} are not shared by all inputs"
            )
    return metrics


def _formula_order(metrics: dict[str, MetricDef]) -> list[str]:
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        metric = metrics[name]
        if metric.kind != "formula" or name in order:
            return
        if name in visiting:
            raise ValueError(f"formula cycle through {name}")
        visiting.add(name)
        assert metric.expression is not None
        for i in formula_inputs(metric.expression):
            visit(i)
        visiting.discard(name)
        order.append(name)

    for name in metrics:
        visit(name)
    return order


def _check_columns(con: duckdb.DuckDBPyConnection, metric: MetricDef) -> bool:
    """Check the SQL contract (ts, dims..., num[, den]); return whether den is present."""
    with con.cursor() as cur:
        try:
            described = cur.execute(
                f"DESCRIBE SELECT * FROM ({metric.sql}) m"
            ).fetchall()
        except duckdb.Error as exc:
            raise ValueError(f"{metric.name}: invalid SQL: {exc}") from None
    columns = [row[0] for row in described]
    expected = ["ts", *metric.dimensions, "num"]
    if columns not in (expected, [*expected, "den"]):
        raise ValueError(
            f"{metric.name}: SQL returns columns {columns}, expected {expected} [+ den]"
        )
    if not str(described[0][1]).startswith("TIMESTAMP"):
        raise ValueError(
            f"{metric.name}: ts must be a timestamp, got {described[0][1]}"
        )
    return len(columns) == len(expected) + 1


def _missing_data(
    con: duckdb.DuckDBPyConnection, metric: MetricDef, tables: tuple[str, ...]
) -> str | None:
    """Dry-run the latest hour; a reason means the simulator does not fill the inputs yet."""
    with con.cursor() as cur:
        empty = [
            t
            for t in tables
            if cur.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() is None
        ]
        if empty:
            return f"no rows in {', '.join(empty)}"
        latest = cur.execute(f"SELECT max(ts) FROM ({metric.sql}) m").fetchone()[0]
        if latest is None:
            return "SQL returns no rows"
        group = ", ".join(metric.dimensions)
        rows = cur.execute(
            f"SELECT ts{', ' + group if group else ''}, SUM(num) AS num "
            f"FROM ({metric.sql}) m WHERE ts >= ? AND ts < ? "
            f"GROUP BY ALL",
            [latest, latest + timedelta(hours=1)],
        ).fetchall()
    if all(row[-1] is None for row in rows):
        return "num is NULL in the latest hour"
    return None


def _pending(metric: MetricDef, reason: str) -> MetricDef:
    _log.warning(
        json.dumps({"event": "metric_pending", "metric": metric.name, "reason": reason})
    )
    return metric.model_copy(update={"status": "pending"})
