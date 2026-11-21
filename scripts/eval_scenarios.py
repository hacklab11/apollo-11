"""Run the scenario oracle against an isolated, stepped core."""

import argparse
import asyncio
import json
import time
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import yaml

from eye.a2a_client import A2AClient
from eye.agents.detective import Detective
from eye.agents.sentinel import Sentinel
from eye.config import load_settings
from eye.contracts import Incident, TrailStep
from eye.graph.bootstrap import load_or_bootstrap
from eye.incidents.store import IncidentStore
from eye.llm.client import LLMClient, configure_client
from eye.main import _reporter
from eye.orchestrator import HOUR, Orchestrator
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.sim.engine import Simulator
from eye.sim.scenarios import _TITLES, ScenarioManager
from eye.tools.stats import expected

ROOT = Path(__file__).resolve().parents[1]


def load_oracle() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "tests/oracle/scenarios.yaml").read_text())


def _checks(spec: dict, items: list[dict], trails: list[dict]) -> list[dict]:
    results = []

    def check(name: str, passed: bool, actual: Any) -> None:
        results.append({"check": name, "passed": passed, "actual": actual})

    optional = spec.get("or_no_incident", False) and not items
    roots = [i["root_cause"] for i in items if i.get("root_cause")]
    hypotheses = [h for i in items for h in i["hypotheses"] if h["status"] != "refuted"]
    segments = [h["segment"] for h in roots + hypotheses]
    segments.extend(i["entry"]["segment"] for i in items)
    segments.extend(
        {"platform": actor}
        for i in items
        for actor in i["affected_actors"]
        if actor in {"web", "android", "ios"}
    )
    text = json.dumps(
        [
            {
                k: i[k]
                for k in ("title", "summary", "open_questions", "suggested_actions")
            }
            for i in items
        ]
    ).casefold()
    for key, wanted in spec.items():
        if key == "or_no_incident":
            continue
        actual: Any = None
        ok = False
        if key in {
            "entry_metrics_any",
            "verdict_any",
            "status_any",
            "root_node_any",
            "cause_type",
            "must_not_root_cause",
        }:
            field = {
                "entry_metrics_any": "metric",
                "verdict_any": "verdict",
                "status_any": "status",
                "root_node_any": "root_node",
                "cause_type": "cause_type",
                "must_not_root_cause": "cause_type",
            }[key]
            source = (
                [i["entry"] for i in items]
                if key == "entry_metrics_any"
                else roots
                if key in {"root_node_any", "cause_type", "must_not_root_cause"}
                else items
            )
            actual = [i[field] for i in source]
            ok = bool(
                set(actual) & set(wanted if isinstance(wanted, list) else [wanted])
            )
            if key == "must_not_root_cause":
                ok = bool(items) and not set(actual).intersection(wanted)
                if "payment provider failure" in wanted:
                    ok &= not any(
                        h["root_node"] == "payment_success_rate" for h in roots
                    )
        elif key == "segment_contains":
            actual = segments
            ok = all(
                all(any(s.get(dim) == value for s in segments) for value in values)
                for dim, values in wanted.items()
            )
        elif key in {
            "must_mention",
            "must_mention_any",
            "open_questions_mention",
            "suggested_actions_mention",
        }:
            actual = (
                text
                if key.startswith("must_")
                else json.dumps(
                    [i[key.removesuffix("_mention")] for i in items]
                ).casefold()
            )
            matches = [word.casefold() in actual for word in wanted]
            ok = any(matches) if key == "must_mention_any" else all(matches)
        elif key == "max_incidents_for_scenario":
            actual = len(items)
            ok = 0 < actual <= wanted
        elif key == "evidence_groups_min":
            actual = sorted({e["group"] for h in roots for e in h["evidence_for"]})
            ok = len(actual) >= wanted
        elif key in {"root_nodes_all", "contribution_shares", "contribution_kind"}:
            contributions = [c for i in items for c in i["contributions"]]
            actual = contributions
            if key == "root_nodes_all":
                names = {h["root_node"] for h in roots + hypotheses} | {
                    c["factor"] for c in contributions
                }
                actual = sorted(names)
                ok = set(wanted) <= names
            elif key == "contribution_kind":
                ok = any(c["kind"] == wanted for c in contributions)
            else:
                ok = all(
                    any(
                        c["factor"] == factor and bounds[0] <= c["share"] <= bounds[1]
                        for c in contributions
                    )
                    for factor, bounds in wanted.items()
                )
        elif key == "notified_sellers":
            actual = [
                s
                for t in trails
                if (t.get("output") or {}).get("received") is True
                for s in t["output"].get("seller_ids", [])
            ]
            ok = set(wanted) <= set(actual)
        elif key in {"loop_contains", "affected_sellers"}:
            outputs = [t.get("output") or {} for t in trails]
            actual = [
                v
                for output in outputs
                for v in (output.get("loop" if key == "loop_contains" else key) or [])
            ]
            ok = set(wanted) <= set(actual)
        elif key == "must_mention_scenario_seller":
            actual = [
                t["output"].get("scenario_seller")
                for t in trails
                if t.get("output") and t["output"].get("scenario_id") == wanted
            ]
            ok = bool(actual) and any(
                seller and seller.casefold() in text for seller in actual
            )
        elif key == "notification_action":
            actual = [
                t["output"].get("recommended_action", "")
                for t in trails
                if (t.get("output") or {}).get("received") is True
            ]
            ok = any(wanted.casefold() in action.casefold() for action in actual)
        elif key == "verdict_sequence":
            actual = [
                t["output"]["verdict"]
                for t in trails
                if t.get("output") and "verdict" in t["output"]
            ]
            remaining = iter(actual)
            ok = all(any(value == verdict for value in remaining) for verdict in wanted)
        elif key == "metric_attached":
            actual = [
                (t.get("output") or {})
                for t in trails
                if (t.get("output") or {}).get("action") == "attach"
            ]
            ok = any(
                value.get("anomaly", {}).get("metric") == wanted
                and value.get("attach_to_incident") in {i["id"] for i in items}
                for value in actual
            )
        elif key in {"first_change_at_inject", "first_hour_deferred"}:
            injections = [
                t["output"]["inject_ts"]
                for t in trails
                if (t.get("output") or {}).get("scenario_id") == wanted
            ]
            if key == "first_change_at_inject":
                actual = [h["first_change_ts"] for h in roots]
                ok = bool(injections) and any(
                    ts
                    and datetime.fromisoformat(ts)
                    == datetime.fromisoformat(injections[0])
                    for ts in actual
                )
            else:
                actual = [
                    t["output"]
                    for t in trails
                    if (t.get("output") or {}).get("action") == "defer"
                ]
                ok = bool(injections) and any(
                    datetime.fromisoformat(value["anomaly"]["ts"])
                    == datetime.fromisoformat(injections[0])
                    for value in actual
                )
        else:
            raise ValueError(f"Unknown oracle check: {key}")
        check(key, optional or ok, actual)
    return results


def evaluate(
    entry: dict, incidents: list[Incident], trails: list[TrailStep]
) -> list[dict]:
    """Pure checker; choose one coherent incident, never mix unrelated reports."""
    raw = [i.model_dump(mode="json") for i in incidents if i.merged_into is None]
    steps = [t.model_dump(mode="json") for t in trails]

    def group(spec: dict, prefix: str) -> list[dict]:
        spec = spec.get("expect", spec)
        candidates = [
            i
            for i in raw
            if not spec.get("entry_metrics_any")
            or i["entry"]["metric"] in spec["entry_metrics_any"]
        ]
        if spec.get("segment_contains"):
            candidates = [
                i
                for i in candidates
                if any(
                    all(
                        h.get("segment", {}).get(dim) in values
                        for dim, values in spec["segment_contains"].items()
                    )
                    for h in [i["entry"], i.get("root_cause") or {}, *i["hypotheses"]]
                )
            ]
        choices = [
            _checks(
                spec,
                [i],
                [t for t in steps if t["investigation_id"] in i["investigation_ids"]],
            )
            for i in candidates
        ] or [_checks(spec, [], [])]
        chosen = max(choices, key=lambda checks: sum(c["passed"] for c in checks))
        for result in chosen:
            if result["check"] == "max_incidents_for_scenario":
                result.update(
                    actual=len(candidates),
                    passed=0 < len(candidates) <= spec["max_incidents_for_scenario"],
                )
            result["check"] = prefix + result["check"]
        return chosen

    results = group(entry, "expect.")
    for name, spec in entry.get("also", {}).items():
        if name == "I1":
            other = [
                i
                for i in incidents
                if i.verdict == "INNOCENT"
                or (i.root_cause and i.root_cause.cause_type == "planned_event")
                or "black-friday-2026" in i.summary.casefold()
            ]
            nested = evaluate({"expect": spec.get("expect", spec)}, other, trails)
            for result in nested:
                result["check"] = result["check"].replace("expect.", f"also.{name}.", 1)
            results.extend(nested)
        else:
            results.extend(group(spec, f"also.{name}."))
    if "max_ticks_to_detect" in entry:
        metrics = entry["expect"].get("entry_metrics_any", [])
        start = (
            datetime.fromisoformat(entry["start_ts"]) if "start_ts" in entry else None
        )
        hours = [
            int(p.rsplit("@", 1)[1])
            for p in entry["play"]
            if not p.startswith("clear") and not p.startswith("I1")
        ]
        offset = min(hours, default=0)
        ticks = (
            min(
                (
                    (i.opened_ts - start) // HOUR - offset + 1
                    for i in incidents
                    if i.entry.metric in metrics
                ),
                default=None,
            )
            if start
            else None
        )
        results.append(
            {
                "check": "max_ticks_to_detect",
                "passed": ticks is not None
                and 0 < ticks <= entry["max_ticks_to_detect"],
                "actual": ticks,
            }
        )
    return results


async def run_scenario(name: str, entry: dict, args: argparse.Namespace) -> dict:
    started = time.monotonic()
    unavailable = [
        command.rsplit("@", 1)[0]
        for command in entry["play"]
        if not command.startswith("clear(") and command.rsplit("@", 1)[0] not in _TITLES
    ]
    if unavailable:
        reason = f"scenario {unavailable[0]} not available in the simulator"
        return {
            "scenario": name,
            "checks": [
                {"check": "simulator_available", "passed": False, "actual": reason}
            ],
            "ticks_to_detect": None,
            "llm_calls": 0,
            "tokens": 0,
            "seconds": round(time.monotonic() - started, 2),
            "incidents": [],
            "trails": [],
        }
    settings = load_settings(ROOT / "config/settings.yaml")
    settings.sim.seed = args.seed
    settings.sim.autostart = False
    record = args.record or (
        args.record_dir / f"{name.lower()}.jsonl"
        if args.record_dir
        else ROOT / f"tests/fixtures/llm/{name.lower()}.jsonl"
    )
    settings.llm.mode = (
        "record"
        if args.mode == "live" and (args.record or args.record_dir)
        else args.mode
    )
    settings.llm.cache_path = record
    llm = LLMClient(settings.llm)
    configure_client(llm)
    with TemporaryDirectory(prefix="eye-eval-") as directory, ExitStack() as stack:
        path = Path(directory)
        sim = stack.enter_context(
            closing(Simulator(settings, db_path=path / "world.duckdb"))
        )
        sim.selleros_export.path = path / "sellers.json"
        sim.backfill()
        connection = stack.enter_context(sim.store.cursor())
        registry = load_registry(connection)
        engine = MetricEngine(connection, registry)
        graph = load_or_bootstrap(engine, path / "graph.json", ROOT / "config")
        a2a = A2AClient(settings.a2a)
        investigate = Detective(
            connection,
            registry,
            graph,
            a2a=a2a,
            settings=settings.detective,
            freshness=settings.sentinel,
            llm=settings.llm,
        ).investigate
        store = stack.enter_context(
            closing(
                IncidentStore(
                    path / "ops.duckdb",
                    path_exists=graph.path_exists,
                    notify=a2a.notify_seller_os,
                    settings=settings.reporter,
                )
            )
        )

        def z_lookup(metric: str, segment: dict, ts: datetime) -> tuple[float, float]:
            value = expected(metric, ts, segment, con=connection, registry=registry)
            return value.z if value.z is not None else float("inf"), value.completeness

        core = Orchestrator(
            settings,
            sim,
            Sentinel(
                connection,
                registry,
                settings=settings.sentinel,
                llm=settings.llm,
                graph=graph,
            ),
            store,
            llm,
            z_lookup,
            scenarios=ScenarioManager(sim),
            scenario_titles=_TITLES,
            a2a=a2a,
            investigate=investigate,
            invalidate=engine.invalidate,
        )

        async def recorded_investigate(anomaly: Any, on_step: Any) -> Any:
            result = await investigate(anomaly, on_step)
            await on_step(
                TrailStep(
                    id=f"stp_{uuid4().hex}",
                    investigation_id=result.investigation_id,
                    n=result.steps_used + 1,
                    real_ts=datetime.now(UTC),
                    actor="orchestrator",
                    action="decision",
                    output_summary=f"Investigation verdict: {result.verdict}",
                    output=result.model_dump(mode="json"),
                )
            )
            return result

        core.investigate = recorded_investigate

        async def notify(alert: Any) -> Any:
            ack = await a2a.notify_seller_os(alert)
            item = store.get(alert.incident_id)
            if item and item.investigation_ids:
                investigation_id = item.investigation_ids[-1]
                await core.record_step(
                    TrailStep(
                        id=f"stp_{uuid4().hex}",
                        investigation_id=investigation_id,
                        n=max((t.n for t in store.trail(investigation_id)), default=0)
                        + 1,
                        real_ts=datetime.now(UTC),
                        actor="selleros",
                        action="a2a_call",
                        output_summary=f"SellerOS received={ack.received}; sellers={len(alert.seller_ids)}",
                        output={
                            "received": ack.received,
                            "seller_ids": alert.seller_ids,
                            "recommended_action": alert.recommended_action,
                        },
                    )
                )
            return ack

        store.notify = notify
        store.reporter = _reporter(llm, settings, core).write
        entry = {**entry, "start_ts": sim.now.isoformat()}
        try:
            for hour in range(entry["run_hours"]):
                for command in entry["play"]:
                    action, at = command.rsplit("@", 1)
                    if int(at) == hour:
                        clear = action.startswith("clear(")
                        await core.set_scenario(
                            action[6:-1] if clear else action, active=not clear
                        )
                await core.step_once()
        finally:
            await core.close()
        incidents = store.all()
        trails = [
            TrailStep.model_validate_json(row[0])
            for row in store.db.execute("SELECT step FROM trail_steps").fetchall()
        ]
        checks = evaluate(entry, incidents, trails)
        usage = llm.status()
        return {
            "scenario": name,
            "checks": checks,
            "ticks_to_detect": next(
                (c["actual"] for c in checks if c["check"] == "max_ticks_to_detect"),
                None,
            ),
            "llm_calls": usage["calls_per_hour"],
            "tokens": llm.tokens_in + llm.tokens_out,
            "seconds": round(time.monotonic() - started, 2),
            "incidents": [i.model_dump(mode="json") for i in incidents],
            "trails": [t.model_dump(mode="json") for t in trails],
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--scenario")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--mode", choices=["live", "replay"], default="replay")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--record-dir", type=Path)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    oracle = load_oracle()
    if args.scenario and args.scenario not in oracle:
        parser.error("unknown scenario")
    if args.all and args.record:
        parser.error("use --record-dir with --all")
    output = ROOT / "data/eval"
    output.mkdir(parents=True, exist_ok=True)
    passed = True
    for name in oracle if args.all else [args.scenario]:
        result = await run_scenario(name, oracle[name], args)
        (output / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(
            "scenario | check | result | actual | ticks to detect | LLM calls | tokens | seconds"
        )
        for check in result["checks"]:
            print(
                f"{name} | {check['check']} | {'PASS' if check['passed'] else 'FAIL'} | {json.dumps(check['actual'])} | {result['ticks_to_detect']} | {result['llm_calls']} | {result['tokens']} | {result['seconds']}"
            )
        passed &= all(c["passed"] for c in result["checks"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
