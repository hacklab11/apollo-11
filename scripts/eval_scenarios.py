"""Run the scenario oracle against an isolated, stepped core."""

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import yaml

from eye.a2a_client import _FAILURES, A2AClient, AgentCardInfo
from eye.agents.cartographer import Cartographer
from eye.agents.detective import Detective
from eye.agents.sentinel import Sentinel
from eye.api.app import create_app
from eye.config import load_settings
from eye.contracts import (
    AddMetricRequest,
    Incident,
    SellerAlertAck,
    SpecialistRequest,
    SpecialistResponse,
    TrailStep,
)
from eye.graph.bootstrap import load_or_bootstrap
from eye.incidents.store import IncidentStore
from eye.llm.client import LLMClient, LLMUnavailable, configure_client
from eye.llm.runtime import run_agent
from eye.main import _reporter
from eye.orchestrator import HOUR, Orchestrator
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.sim.engine import Simulator
from eye.sim.scenarios import _TITLES, ScenarioManager
from eye.specialists.app import SPECIALISTS
from eye.specialists.app import answer as answer_specialist
from eye.specialists.tools import DataApiUnavailable, DataClient
from eye.tools.stats import expected

ROOT = Path(__file__).resolve().parents[1]

DEMO_METRICS = {
    "fee_per_order": AddMetricRequest(
        name="fee_per_order",
        description="Marketplace fee revenue in euros divided by units ordered.",
        unit="eur",
        # docs/specs/05 §4; same SQL as tests/agents/test_cartographer.py:FEE_SQL.
        sql="""SELECT ts, country, platform, category, seller_tier, channel,
SUM(fee_revenue) AS num, SUM(units) AS den FROM orders_hourly
GROUP BY ts, country, platform, category, seller_tier, channel""",
    )
}


def request_identity(request: dict) -> dict:
    return {
        "model": request["model"],
        "system": "".join(b["text"] for b in request["system"]),
        "tools": [t["name"] for t in request["tools"]],
        "messages": request["messages"],
    }


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda model: model.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


def append_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True) + "\n")


class EvalLLMClient(LLMClient):
    """Preserve request identities and prohibit replay's live fallback."""

    def __init__(self, settings: Any, trace: Path | None = None) -> None:
        super().__init__(settings)
        self.trace = trace
        self.replay_misses = 0

        self.total_calls = 0
        self.replayed_calls = 0

    async def create(self, request: dict) -> Any:
        identity = request_identity(request)
        key = digest(identity)
        if self.trace is not None:
            append_json(self.trace, {"key": key, "identity": identity})
        if self.settings.mode == "replay":
            path = Path(self.settings.cache_path)
            keys = (
                {json.loads(line)["key"] for line in path.read_text().splitlines()}
                if path.exists()
                else set()
            )
            if key not in keys:
                self.replay_misses += 1
                print(json.dumps({"event": "replay_miss", "key": key}))
                raise LLMUnavailable(f"Offline replay has no response for {key}")
        self.total_calls += 1
        response = await super().create(request)
        self.replayed_calls += self.settings.mode == "replay"
        return type(response).model_validate_json(
            json.dumps(response.model_dump(mode="json"), sort_keys=True)
        )


class EvalA2AClient(A2AClient):
    """Snapshot external boundaries in the same fixture as LLM responses."""

    def __init__(self, settings: Any, mode: str, path: Path) -> None:
        super().__init__(settings)
        self.mode, self.path = mode, path
        self.replay_misses = 0
        self.local_data: DataClient | None = None
        self.local_settings: Any = None

    async def exchange(self, method: str, args: dict) -> Any:
        key = "a2a:" + digest({"method": method, "args": args})
        if self.mode == "replay":
            for line in (
                self.path.read_text().splitlines() if self.path.exists() else []
            ):
                entry = json.loads(line)
                if entry["key"] == key:
                    if "error" in entry:
                        raise ValueError(entry["error"]["message"])
                    return entry["response_json"]
            self.replay_misses += 1
            print(json.dumps({"event": "replay_miss", "kind": "a2a", "key": key}))
            raise LLMUnavailable(
                f"Offline replay has no A2A snapshot for {method}: {key}"
            )
        if method == "fetch_cards" and self.local_data is not None:
            try:
                await self._card("selleros", refresh=True)
            except _FAILURES as exc:
                self._unavailable("selleros", exc)
            cards = {"selleros": self._health["selleros"]}
            cards.update(
                {
                    module.NAME: AgentCardInfo(
                        name=module.NAME,
                        url=f"inprocess://{module.NAME}",
                        skills=list(module.SKILLS),
                        healthy=True,
                    )
                    for module in SPECIALISTS
                }
            )
            value = {name: card.model_dump(mode="json") for name, card in cards.items()}
        elif (
            method == "ask"
            and self.local_data is not None
            and args["agent"] in {module.NAME for module in SPECIALISTS}
        ):
            module = next(
                module for module in SPECIALISTS if module.NAME == args["agent"]
            )
            try:
                response, _ = await answer_specialist(
                    module, self.local_data, args["request"], self.local_settings
                )
            except Exception as exc:  # noqa: BLE001 -- mirror failed A2A tasks
                detail = (
                    str(exc)
                    if isinstance(exc, DataApiUnavailable)
                    else type(exc).__name__
                )
                message = f"{module.TITLE} failed: {detail}"
                if self.mode == "record":
                    append_json(
                        self.path,
                        {
                            "key": key,
                            "error": {"type": "ValueError", "message": message},
                        },
                    )
                raise ValueError(message) from exc
            value = response.model_dump(mode="json")
        elif method == "fetch_cards":
            value = {
                name: card.model_dump(mode="json")
                for name, card in (await super().fetch_cards()).items()
            }
        elif method == "ask":
            value = (await super().ask(args["agent"], args["request"])).model_dump(
                mode="json"
            )
        else:
            value = (await super().notify_seller_os(args["alert"])).model_dump(
                mode="json"
            )
        if self.mode == "record":
            append_json(self.path, {"key": key, "response_json": value})
        return value

    async def fetch_cards(self) -> dict[str, AgentCardInfo]:
        return {
            name: AgentCardInfo.model_validate(card)
            for name, card in (await self.exchange("fetch_cards", {})).items()
        }

    async def ask(self, agent: str, request: SpecialistRequest) -> SpecialistResponse:
        return SpecialistResponse.model_validate(
            await self.exchange("ask", {"agent": agent, "request": request})
        )

    async def notify_seller_os(self, alert: Any) -> SellerAlertAck:
        return SellerAlertAck.model_validate(
            await self.exchange("notify", {"alert": alert})
        )


def seeded_ids(seed: int, scenario: str, module: str) -> Iterator[Any]:
    n = 0
    while True:
        yield uuid5(NAMESPACE_URL, f"eye-eval:{seed}:{scenario}:{module}:{n}")
        n += 1


async def deterministic_agent(
    spec: Any, content: str, investigation_id: str, on_step: Any, **kwargs: Any
) -> Any:
    counts: dict[str, int] = {}

    async def stable_step(step: TrailStep) -> None:
        identity = digest(
            {
                "actor": step.actor,
                "action": step.action,
                "tool": step.tool,
                "input": step.input,
                "summary": step.output_summary,
            }
        )
        occurrence = counts.get(identity, 0)
        counts[identity] = occurrence + 1
        id_ = uuid5(NAMESPACE_URL, f"{investigation_id}:{identity}:{occurrence}")
        await on_step(step.model_copy(update={"id": f"stp_{id_.hex}"}))

    return await run_agent(spec, content, investigation_id, stable_step, **kwargs)


def load_oracle() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "tests/oracle/scenarios.yaml").read_text())


def stored_trails(store: IncidentStore) -> list[TrailStep]:
    return [
        TrailStep.model_validate_json(row[0])
        for row in store.db.execute("SELECT step FROM trail_steps").fetchall()
    ]


def prepare_recording(path: Path, mode: str) -> None:
    if mode == "record":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


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
        elif key == "must_not_root_node":
            actual = [h["root_node"] for h in roots]
            ok = bool(items) and not set(actual).intersection(wanted)
        elif key == "symptom_nodes_all":
            actual = sorted({s["node"] for i in items for s in i["symptoms"]})
            ok = bool(items) and set(wanted) <= set(actual)
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
            actual = {
                "found": [
                    word for word, found in zip(wanted, matches, strict=True) if found
                ],
                "missing": [
                    word
                    for word, found in zip(wanted, matches, strict=True)
                    if not found
                ],
            }
        elif key == "max_incidents_for_scenario":
            actual = len(items)
            ok = 0 < actual <= wanted
        elif key == "deferral_required":
            actual = [
                t["output"]["anomaly"]
                for t in trails
                if t["actor"] == "sentinel"
                and (t.get("output") or {}).get("action") == "defer"
                and wanted.items()
                <= t["output"].get("anomaly", {}).get("segment", {}).items()
                and t["output"].get("anomaly", {}).get("completeness", 1) < 0.9
            ]
            ok = bool(actual) or any(
                i["status"] == "deferred"
                and wanted.items() <= i["entry"]["segment"].items()
                for i in items
            )
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
            metric = wanted["metric"] if isinstance(wanted, dict) else wanted
            segment = wanted.get("segment", {}) if isinstance(wanted, dict) else {}
            ok = any(
                value.get("anomaly", {}).get("metric") == metric
                and segment.items()
                <= value.get("anomaly", {}).get("segment", {}).items()
                and value.get("attach_to_incident") in {i["id"] for i in items}
                for value in actual
            )
        elif key in {
            "first_change_at_inject",
            "first_hour_deferred",
            "first_hour_completeness",
        }:
            injections = [
                t["output"]["inject_ts"]
                for t in trails
                if (t.get("output") or {}).get("scenario_id")
                == (wanted["scenario"] if isinstance(wanted, dict) else wanted)
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
                    and (
                        key != "first_hour_completeness"
                        or value["anomaly"].get("completeness") == wanted["value"]
                    )
                    for value in actual
                )
        else:
            raise ValueError(f"Unknown oracle check: {key}")
        check(key, (optional and key != "deferral_required") or ok, actual)
    return results


def matching_incidents(
    entry: dict,
    raw: list[dict],
    steps: list[dict],
    spec: dict,
    scenario: str | None = None,
) -> list[dict]:
    start = datetime.fromisoformat(entry["start_ts"]) if "start_ts" in entry else None
    commands = [
        (p.rsplit("@", 1)[0], int(p.rsplit("@", 1)[1]))
        for p in entry.get("play", [])
        if not p.startswith("clear(")
    ]
    if scenario is None:
        scenario = next(
            (name for name, _ in commands if not name.startswith("I")),
            commands[0][0] if commands else None,
        )
    offset = next((hour for name, hour in commands if name == scenario), 0)
    until = min(
        (hour for name, hour in commands if hour > offset),
        default=entry.get("run_hours", float("inf")),
    )
    candidates = []
    for item in raw:
        if item.get("merged_into") is not None:
            continue
        opened = datetime.fromisoformat(item["opened_ts"])
        if start and not start + offset * HOUR <= opened < start + until * HOUR:
            continue
        report = json.dumps(
            {
                "report": {
                    key: item[key]
                    for key in (
                        "title",
                        "summary",
                        "suggested_actions",
                        "open_questions",
                    )
                },
                "trail": [
                    t
                    for t in steps
                    if t["investigation_id"] in item["investigation_ids"]
                ],
            },
            sort_keys=True,
        ).casefold()
        cause = spec.get("cause_type")
        tokens = [*spec.get("must_mention", []), *([cause] if cause else [])]
        mentioned = any(
            re.search(r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)", report)
            for word in tokens
        )
        cause_match = cause and any(
            h.get("cause_type") == cause
            for h in [item.get("root_cause") or {}, *item["hypotheses"]]
            if h.get("status") != "refuted"
        )
        if (
            not (spec.get("entry_metrics_any") or tokens or cause)
            or item["entry"]["metric"] in spec.get("entry_metrics_any", [])
            or mentioned
            or cause_match
        ):
            candidates.append(item)
    return candidates


class ConditionalActions:
    def __init__(self, entry: dict, metrics: dict[str, AddMetricRequest]) -> None:
        self.entry, self.metrics = entry, metrics
        self.actions: dict[str, tuple[str, str]] = {}
        self.completed: set[str] = set()
        self.incident_id: str | None = None
        for trigger in ("on_incident_open", "on_selleros_ack"):
            if trigger not in entry:
                continue
            action = re.fullmatch(
                r"\s*(add_metric|clear)\(\s*([A-Za-z][A-Za-z0-9_]*)\s*\)\s*",
                entry[trigger],
            )
            if action is None:
                raise ValueError(f"Invalid conditional action: {entry[trigger]}")
            method, target = action.groups()
            if method == "add_metric" and target not in metrics:
                raise ValueError(f"No metric SQL defined for {target}")
            self.actions[trigger] = method, target

    async def poll(
        self, core: Any, incidents: list[Incident], trails: list[TrailStep]
    ) -> None:
        if not self.actions:
            return
        raw = [i.model_dump(mode="json") for i in incidents]
        steps = [t.model_dump(mode="json") for t in trails]
        if self.incident_id is None:
            candidates = [
                i
                for i in matching_incidents(
                    self.entry, raw, steps, self.entry["expect"]
                )
                if i["status"] in {"open", "persisting"}
            ]
            if candidates:
                self.incident_id = min(
                    candidates, key=lambda i: (i["opened_ts"], i["id"])
                )["id"]
        incident = next((i for i in raw if i["id"] == self.incident_id), None)
        if incident is None:
            return
        acknowledged = any(
            t["actor"] == "selleros"
            and t["action"] == "a2a_call"
            and t["investigation_id"] in incident["investigation_ids"]
            and (t.get("output") or {}).get("received") is True
            for t in steps
        )
        for trigger, (method, target) in self.actions.items():
            if trigger in self.completed or (
                trigger == "on_selleros_ack" and not acknowledged
            ):
                continue
            if method == "add_metric":
                await core.register_metric(self.metrics[target])
            else:
                await core.set_scenario(target, active=False)
            self.completed.add(trigger)


def evaluate(
    entry: dict, incidents: list[Incident], trails: list[TrailStep]
) -> list[dict]:
    """Pure checker; choose one coherent incident, never mix unrelated reports."""
    raw = [i.model_dump(mode="json") for i in incidents if i.merged_into is None]
    steps = [t.model_dump(mode="json") for t in trails]

    def group(spec: dict, prefix: str, scenario: str | None = None) -> list[dict]:
        deadline = spec.get("max_ticks_to_detect")
        spec = spec.get("expect", spec)
        candidates = matching_incidents(entry, raw, steps, spec, scenario)
        sentinel_steps = [t for t in steps if t["actor"] == "sentinel"]
        choices = [
            (
                i,
                _checks(
                    spec,
                    [i],
                    [
                        t
                        for t in steps
                        if t["investigation_id"] in i["investigation_ids"]
                    ]
                    + sentinel_steps,
                ),
            )
            for i in candidates
        ] or [(None, _checks(spec, [], sentinel_steps))]
        incident, chosen = max(
            choices, key=lambda choice: sum(c["passed"] for c in choice[1])
        )
        for result in chosen:
            if result["check"] == "max_incidents_for_scenario":
                result.update(
                    actual=len(candidates),
                    passed=0 < len(candidates) <= spec["max_incidents_for_scenario"],
                )
            result["check"] = prefix + result["check"]
        if deadline is not None:
            start = (
                datetime.fromisoformat(entry["start_ts"])
                if "start_ts" in entry
                else None
            )
            offsets = [
                int(p.rsplit("@", 1)[1])
                for p in entry.get("play", [])
                if p.rsplit("@", 1)[0] == scenario
                or (scenario is None and p.startswith("S"))
            ]
            ticks = (
                (datetime.fromisoformat(incident["opened_ts"]) - start) // HOUR
                - min(offsets, default=0)
                + 1
                if incident is not None and start
                else None
            )
            chosen.append(
                {
                    "check": ("" if prefix == "expect." else prefix)
                    + "max_ticks_to_detect",
                    "passed": ticks is not None and 0 < ticks <= deadline,
                    "actual": ticks,
                }
            )
        return chosen

    results = group(entry, "expect.")
    for name, spec in entry.get("also", {}).items():
        if name in {"I1", "I3"}:
            campaign_id = (
                "black-friday-2026" if name == "I1" else "coupon-day-electronics"
            )
            start = (
                datetime.fromisoformat(entry["start_ts"])
                if "start_ts" in entry
                else None
            )
            first_fault = min(
                (
                    int(p.rsplit("@", 1)[1])
                    for p in entry.get("play", [])
                    if p.startswith("S")
                ),
                default=0,
            )
            other = [
                i
                for i in incidents
                if i.verdict == "INNOCENT"
                or (i.root_cause and i.root_cause.cause_type == "planned_event")
                or (i.root_cause is None and campaign_id in i.summary.casefold())
                or (
                    start is not None
                    and start <= i.opened_ts < start + first_fault * HOUR
                )
            ]
            campaign_spec = spec.get("expect", spec)
            nested = _checks(
                campaign_spec, [i.model_dump(mode="json") for i in other], steps
            )
            for result in nested:
                if result["check"] in {"status_any", "verdict_any"} and other:
                    result["passed"] = all(
                        value in campaign_spec[result["check"]]
                        for value in result["actual"]
                    )
                result["check"] = f"also.{name}." + result["check"]
            results.extend(nested)
        else:
            results.extend(group(spec, f"also.{name}.", name))
    return results


async def run_scenario(name: str, entry: dict, args: argparse.Namespace) -> dict:
    started = time.monotonic()
    unavailable = [
        command.rsplit("@", 1)[0]
        for command in entry["play"]
        if not command.startswith("clear(") and command.rsplit("@", 1)[0] not in _TITLES
    ]
    if unavailable:
        reason = "; ".join(
            f"scenario {scenario} not available in the simulator"
            for scenario in sorted(unavailable, key=lambda name: name != "S8")
        )
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
    settings.orchestrator.max_concurrent_investigations = 1
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
    prepare_recording(record, settings.llm.mode)
    trace = (
        ROOT / f"data/eval/{name}-{settings.llm.mode}-requests.jsonl"
        if getattr(args, "trace", False)
        else None
    )
    if trace is not None:
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text("")
    llm = EvalLLMClient(settings.llm, trace)
    configure_client(llm)
    with TemporaryDirectory(prefix="eye-eval-") as directory, ExitStack() as stack:
        for module in (
            "eye.agents.sentinel",
            "eye.agents.detective",
            "eye.agents.skeptic",
            "eye.agents.cartographer",
            "eye.specialists.app",
        ):
            stack.enter_context(
                patch.object(import_module(module), "run_agent", deterministic_agent)
            )
        for module in (
            "eye.orchestrator",
            "eye.incidents.store",
            "eye.agents.detective",
            "eye.agents.cartographer",
            "eye.llm.runtime",
            "eye.main",
            "eye.a2a_client",
        ):
            stream = seeded_ids(args.seed, name, module)
            stack.enter_context(patch(f"{module}.uuid4", side_effect=stream.__next__))
        path = Path(directory)
        sim = stack.enter_context(
            closing(Simulator(settings, db_path=path / "world.duckdb"))
        )
        with sim.store.cursor() as setup:
            setup.execute("SET threads = 1")
        sim.selleros_export.path = path / "sellers.json"
        sim.backfill()
        connection = stack.enter_context(sim.store.cursor())
        registry = load_registry(connection)
        engine = MetricEngine(connection, registry)
        graph = load_or_bootstrap(engine, path / "graph.json", ROOT / "config")
        a2a = EvalA2AClient(settings.a2a, settings.llm.mode, record)
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
            add_metric=Cartographer(engine, graph, llm=settings.llm).add_metric,
            invalidate=engine.invalidate,
        )
        world_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(core, connection, registry, graph, engine)
            ),
            base_url="http://eval",
        )
        a2a.local_data = DataClient(world_http, "http://eval/data")
        a2a.local_settings = settings

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
        actions = ConditionalActions(entry, DEMO_METRICS)
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
                for decision in core.decisions:
                    if decision.anomaly.ts == core.sim_ts:
                        inv = f"sentinel:{core.sim_ts.isoformat()}"
                        await core.record_step(
                            TrailStep(
                                id=f"decision:{core.tick}:{decision.anomaly.id}",
                                investigation_id=inv,
                                n=max((t.n for t in store.trail(inv)), default=0) + 1,
                                real_ts=datetime.now(UTC),
                                actor="sentinel",
                                action="decision",
                                output_summary=decision.reason[:500],
                                output=decision.model_dump(mode="json"),
                            )
                        )
                if actions.actions:
                    await actions.poll(core, store.all(), stored_trails(store))
        finally:
            await core.close()
            await world_http.aclose()
        incidents = store.all()
        trails = stored_trails(store)
        checks = evaluate(entry, incidents, trails)
        replay_misses = llm.replay_misses + a2a.replay_misses
        if replay_misses:
            checks.append(
                {
                    "check": "offline_replay_available",
                    "passed": False,
                    "actual": replay_misses,
                }
            )
        return {
            "scenario": name,
            "checks": checks,
            "ticks_to_detect": next(
                (c["actual"] for c in checks if c["check"] == "max_ticks_to_detect"),
                None,
            ),
            "llm_calls": llm.total_calls,
            "replayed_calls": llm.replayed_calls,
            "conditional_actions": sorted(actions.completed),
            "replay_misses": replay_misses,
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
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Opt in to sensitive full request traces under data/eval",
    )
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
            "scenario | check | result | actual | ticks to detect | LLM calls (including replayed) | tokens (including replayed) | seconds"
        )
        for check in result["checks"]:
            print(
                f"{name} | {check['check']} | {'PASS' if check['passed'] else 'FAIL'} | {json.dumps(check['actual'])} | {result['ticks_to_detect']} | {result['llm_calls']} | {result['tokens']} | {result['seconds']}"
            )
        print(f"replay_miss count: {result.get('replay_misses', 0)}")
        passed &= all(c["passed"] for c in result["checks"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
