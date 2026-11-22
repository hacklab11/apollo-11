"""Offline coverage of the scenario oracle checker."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from factories import CAMPAIGN_TS, make_incident_s7
from pydantic import BaseModel

from eye.config import A2aSettings, LlmSettings
from eye.contracts import AddMetricRequest, Contribution, Incident, Symptom, TrailStep
from eye.llm.client import LLMUnavailable

SPEC = spec_from_file_location(
    "eval_scenarios", Path(__file__).resolve().parents[1] / "scripts/eval_scenarios.py"
)
assert SPEC and SPEC.loader
harness = module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def oracle_entry() -> dict:
    entry = deepcopy(harness.load_oracle()["S7"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    return entry


def test_oracle_ids() -> None:
    oracle = harness.load_oracle()
    assert set(oracle) == {
        *(f"S{i}" for i in range(1, 9)),
        "I1",
        "I2",
        "coupon_day",
        *(f"D{i}" for i in range(1, 7)),
    }
    for entry in oracle.values():
        assert entry["play"] and entry["run_hours"] > 0 and entry["expect"]
        assert harness.evaluate(entry, [], [])
    assert next(iter(oracle)) == "D6"


@pytest.mark.parametrize("include_audit", [True, False])
def test_scenario_audit_supplies_injection_and_seller_evidence(
    include_audit: bool,
    tmp_path: Path,
) -> None:
    incident = make_incident_s7()
    root = incident.root_cause.model_copy(update={"first_change_ts": CAMPAIGN_TS})
    incident = incident.model_copy(
        update={
            "opened_ts": CAMPAIGN_TS,
            "summary": "Price mistake for S-42",
            "root_cause": root,
        }
    )
    state = harness.ScenarioState(
        id="S8",
        title="Price typo",
        active=True,
        injected_ts=CAMPAIGN_TS,
        cleared_ts=None,
    )
    audit = harness.scenario_audit(state, {"seller_id": "S-42"})
    defer = TrailStep(
        id="partial",
        investigation_id="sentinel:partial",
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="sentinel",
        action="decision",
        output_summary="Partial hour",
        output={
            "action": "defer",
            "anomaly": {"ts": CAMPAIGN_TS.isoformat(), "completeness": 0.5},
        },
    )
    entry = {
        "play": ["S8@0"],
        "expect": {
            "first_change_at_inject": "S8",
            "first_hour_deferred": "S8",
            "first_hour_completeness": {"scenario": "S8", "value": 0.5},
            "must_mention_scenario_seller": "S8",
        },
    }
    scenario_audits = [audit] if include_audit else []
    with harness.closing(harness.IncidentStore(tmp_path / "ops.duckdb")) as store:
        store.add_step(defer)
        results = harness.evaluate(
            entry, [incident], harness.stored_trails(store) + scenario_audits
        )
        assert store.trail("eval:scenarios") == []
        assert harness.stored_trails(store) == [defer]
    assert all(c["passed"] == include_audit for c in results)


@pytest.mark.parametrize(
    "provider,model,expected_provider,expected_model",
    [
        (None, None, "anthropic", "original"),
        (
            "openai_compatible",
            None,
            "openai_compatible",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        ),
        ("openai_compatible", "custom/model", "openai_compatible", "custom/model"),
        (None, "custom/model", "anthropic", "custom/model"),
    ],
)
def test_provider_and_model_overrides(
    provider: str | None, model: str | None, expected_provider: str, expected_model: str
) -> None:
    settings = LlmSettings(models={"default": "original", "sentinel": "original"})
    harness.override_llm(settings, provider, model)
    assert settings.provider == expected_provider
    assert settings.model_for("sentinel") == expected_model
    if provider == "openai_compatible":
        assert settings.base_url == "https://openrouter.ai/api/v1"
        assert settings.api_key_env == "OPENROUTER_API_KEY"


@pytest.mark.asyncio
async def test_openrouter_recording_replays_with_matching_provider_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def respond(request: harness.httpx.Request) -> harness.httpx.Response:
        calls.append(request)
        return harness.httpx.Response(
            200,
            json={
                "id": "offline",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    settings = LlmSettings(mode="record", cache_path=tmp_path / "fixture.jsonl")
    harness.override_llm(settings, "openai_compatible", None)
    request = {
        "model": settings.model_for("sentinel"),
        "max_tokens": 1024,
        "system": [],
        "tools": [],
        "messages": [],
    }
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test")
    client = harness.EvalLLMClient(settings)
    async with harness.httpx.AsyncClient(
        transport=harness.httpx.MockTransport(respond)
    ) as http:
        client._http_client = http
        expected = await client.create(request)
    entry = harness.json.loads(settings.cache_path.read_text())
    assert entry["key"] == harness.digest(harness.request_identity(request, settings))
    assert entry["key"] != harness.digest(harness.request_identity(request))
    monkeypatch.delenv("OPENROUTER_API_KEY")
    settings.mode = "replay"
    replay = harness.EvalLLMClient(settings)
    assert await replay.create(request) == expected
    assert replay.replay_misses == 0
    assert replay.replayed_calls == 1
    assert len(calls) == 1


def test_recording_replaces_old_responses_without_touching_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixture.jsonl"
    path.write_text("old response\n")
    harness.prepare_recording(path, "replay")
    assert path.read_text() == "old response\n"
    harness.prepare_recording(path, "record")
    assert path.read_text() == ""
    harness.append_json(path, {"key": "new", "response_json": {}})
    assert "old response" not in path.read_text()


@pytest.mark.parametrize("name", ["S8", "coupon_day", "D6"])
def test_story_scenarios_are_available_in_simulator(name: str) -> None:
    entry = harness.load_oracle()[name]
    assert {
        command.rsplit("@", 1)[0] for command in entry["play"]
    } <= harness._TITLES.keys()


@pytest.mark.asyncio
async def test_unavailable_scenarios_fail_clearly() -> None:
    entry = {**harness.load_oracle()["D6"], "play": ["X1@0", "X2@10"]}
    result = await harness.run_scenario("D6", entry, object())
    assert result["checks"] == [
        {
            "check": "simulator_available",
            "passed": False,
            "actual": "scenario X1 not available in the simulator; scenario X2 not available in the simulator",
        }
    ]
    assert result["llm_calls"] == 0


def test_story_oracles() -> None:
    for name in ("S8", "D6"):
        entry = harness.load_oracle()[name]
        assert entry["play"] == ["S8@0"]
        assert entry["scenario_params"] == {"S8": {"typo_delay_hours": 10}}
        assert "Black Friday" not in entry["expect"]["must_mention"]
        assert "stockout" in entry["expect"]["must_not_root_cause"]
        assert entry["expect"]["symptom_nodes_all"] == ["buybox_share", "in_stock_rate"]
        assert entry["expect"]["metric_attached"] == {
            "metric": "fee_per_order",
            "segment": {"category": "electronics", "seller_tier": "small_3P"},
        }
    assert (
        harness.load_oracle()["D6"]["on_incident_open"] == "add_metric(fee_per_order)"
    )


@pytest.mark.parametrize("innocent", [True, False])
def test_coupon_only_oracle_checks_reports_before_the_typo(innocent: bool) -> None:
    entry = deepcopy(harness.load_oracle()["coupon_day"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    incident = make_incident_s7().model_copy(
        update={
            "opened_ts": CAMPAIGN_TS + harness.HOUR,
            "summary": "coupon-day-electronics is a planned campaign",
            "status": "innocent" if innocent else "open",
            "verdict": "INNOCENT" if innocent else "CONFIRMED",
        }
    )
    checks = {c["check"]: c for c in harness.evaluate(entry, [incident], [])}
    assert checks["expect.status_any"]["passed"] is innocent
    assert checks["expect.verdict_any"]["passed"] is innocent
    assert checks["expect.must_mention"]["passed"]


def test_eval_audit_distinguishes_campaign_trigger_from_price_fault() -> None:
    state = harness.ScenarioState(
        id="S8",
        title="Price typo",
        active=True,
        injected_ts=CAMPAIGN_TS,
        cleared_ts=None,
    )
    audit = harness.scenario_audit(state, {"typo_delay_hours": 10, "seller_id": "S-24"})
    assert audit.output["trigger_ts"] == CAMPAIGN_TS.isoformat()
    assert audit.output["inject_ts"] == (CAMPAIGN_TS + 10 * harness.HOUR).isoformat()
    assert audit.output["scenario_seller"] == "S-24"
    assert state.injected_ts == CAMPAIGN_TS


@pytest.mark.parametrize(
    "opened_hour,passed", [(9, False), (10, True), (11, True), (12, False)]
)
def test_detection_deadline_starts_at_the_delayed_typo(
    opened_hour: int, passed: bool
) -> None:
    entry = {
        "play": ["S8@0"],
        "scenario_params": {"S8": {"typo_delay_hours": 10}},
        "start_ts": CAMPAIGN_TS.isoformat(),
        "run_hours": 20,
        "max_ticks_to_detect": 2,
        "expect": {"entry_metrics_any": ["aov"]},
    }
    incident = demo_incident(hour=opened_hour)
    checks = {c["check"]: c for c in harness.evaluate(entry, [incident], [])}
    assert checks["max_ticks_to_detect"]["passed"] is passed


def test_matching_uses_report_and_window_not_just_entry() -> None:
    entry = deepcopy(harness.load_oracle()["S3"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    good = make_incident_s7()
    root = good.root_cause.model_copy(
        update={"root_node": "first_party_margin", "segment": {"country": "DE"}}
    )
    incident = good.model_copy(
        update={
            "opened_ts": CAMPAIGN_TS,
            "entry": good.entry.model_copy(update={"metric": "first_party_margin"}),
            "summary": "DE search-ranker v7.2 reduced relevance",
            "root_cause": root,
            "hypotheses": [root],
        }
    )
    unrelated = good.model_copy(
        update={
            "id": "unrelated",
            "title": "Budget spent",
            "summary": "Budget spent",
            "suggested_actions": [],
            "open_questions": [],
            "investigation_ids": ["budget-investigation"],
            "entry": good.entry.model_copy(update={"metric": "ad_spend"}),
            "root_cause": root.model_copy(update={"cause_type": "pacing_bug"}),
            "hypotheses": [],
        }
    )
    outside = incident.model_copy(
        update={"id": "outside", "opened_ts": CAMPAIGN_TS - harness.HOUR}
    )
    results = {
        c["check"]: c
        for c in harness.evaluate(entry, [incident, unrelated, outside], [])
    }
    assert results["expect.entry_metrics_any"]["actual"] == ["first_party_margin"]
    assert not results["expect.entry_metrics_any"]["passed"]
    assert results["expect.verdict_any"]["actual"] == ["CONFIRMED"]
    assert results["expect.root_node_any"]["actual"] == ["first_party_margin"]
    assert results["expect.max_incidents_for_scenario"]["actual"] == 1


def test_matching_chooses_best_candidate_and_counts_matches() -> None:
    good = make_incident_s7()
    weaker = good.model_copy(update={"id": "weaker", "verdict": "HYPOTHESIS"})
    results = {
        c["check"]: c for c in harness.evaluate(oracle_entry(), [weaker, good], [])
    }
    assert results["expect.verdict_any"]["actual"] == ["CONFIRMED"]
    assert results["expect.max_incidents_for_scenario"]["actual"] == 2


def test_matching_cause_in_report_and_coupon_window() -> None:
    good = make_incident_s7()
    incident = good.model_copy(
        update={
            "summary": "Unplanned price_change caused this",
            "root_cause": None,
            "hypotheses": [],
            "opened_ts": CAMPAIGN_TS + 11 * harness.HOUR,
        }
    )
    entry = {
        "play": ["S8@0"],
        "scenario_params": {"S8": {"typo_delay_hours": 10}},
        "run_hours": 20,
        "start_ts": CAMPAIGN_TS.isoformat(),
        "expect": {
            "cause_type": "price_change",
            "entry_metrics_any": ["aov"],
            "max_incidents_for_scenario": 1,
        },
    }
    before = incident.model_copy(
        update={"id": "before", "opened_ts": CAMPAIGN_TS + harness.HOUR}
    )
    results = {c["check"]: c for c in harness.evaluate(entry, [before, incident], [])}
    assert results["expect.entry_metrics_any"]["actual"] == [incident.entry.metric]
    assert results["expect.max_incidents_for_scenario"]["actual"] == 1


@pytest.mark.asyncio
async def test_replay_miss_never_calls_sdk(tmp_path: Path) -> None:
    client = harness.EvalLLMClient(
        LlmSettings(mode="replay", cache_path=tmp_path / "absent.jsonl"),
        tmp_path / "requests.jsonl",
    )
    with pytest.raises(LLMUnavailable, match="Offline replay"):
        await client.create(
            {"model": "test", "system": [], "tools": [], "messages": []}
        )
    assert client._sdk is None
    assert client.replay_misses == 1
    assert client.total_calls == client.replayed_calls == 0


@pytest.mark.asyncio
async def test_request_trace_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response(BaseModel):
        content: str = "response"

    async def create(self: object, request: dict) -> Response:
        return Response()

    monkeypatch.setattr(harness.LLMClient, "create", create)
    request = {
        "model": "test",
        "system": [],
        "tools": [],
        "messages": [{"role": "user", "content": "sensitive"}],
    }
    client = harness.EvalLLMClient(LlmSettings())
    await client.create(request)
    assert client.trace is None
    trace = tmp_path / "trace.jsonl"
    await harness.EvalLLMClient(LlmSettings(), trace).create(request)
    assert "sensitive" in trace.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["live", "replay"])
async def test_llm_run_totals_include_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    class Response(BaseModel):
        content: str = "response"

    async def create(self: object, request: dict) -> Response:
        return Response()

    monkeypatch.setattr(harness.LLMClient, "create", create)
    request = {"model": "test", "system": [], "tools": [], "messages": []}
    cache = tmp_path / "fixture.jsonl"
    cache.write_text(
        harness.json.dumps({"key": harness.digest(harness.request_identity(request))})
        + "\n"
    )
    client = harness.EvalLLMClient(
        LlmSettings(mode=mode, cache_path=cache), tmp_path / "requests.jsonl"
    )
    await client.create(request)
    await client.create(request)
    assert client.total_calls == 2
    assert client.replayed_calls == (2 if mode == "replay" else 0)
    assert client.status()["calls_per_hour"] == 0


def demo_incident(id_: str = "demo", hour: int = 10) -> Incident:
    incident = make_incident_s7()
    return incident.model_copy(
        update={
            "id": id_,
            "opened_ts": CAMPAIGN_TS + hour * harness.HOUR,
            "status": "open",
            "investigation_ids": [f"inv_{id_}"],
            "entry": incident.entry.model_copy(update={"metric": "aov"}),
        }
    )


def demo_actions() -> object:
    entry = deepcopy(harness.load_oracle()["D6"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    return harness.ConditionalActions(
        entry, harness.DEMO_METRICS, SimpleNamespace(clear=Mock())
    )


def seller_ack(incident: Incident, received: bool = True) -> TrailStep:
    return TrailStep(
        id="ack",
        investigation_id=incident.investigation_ids[0],
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="selleros",
        action="a2a_call",
        output_summary="SellerOS ack",
        output={"received": received},
    )


@pytest.mark.asyncio
async def test_conditional_actions_match_first_open_incident_and_fire_once() -> None:
    actions = demo_actions()
    core = SimpleNamespace(
        register_metric=AsyncMock(),
    )
    await actions.poll(core, [], [])
    await actions.poll(core, [demo_incident("before", 9)], [])
    await actions.poll(
        core, [demo_incident().model_copy(update={"status": "deferred"})], []
    )
    core.register_metric.assert_not_awaited()
    first, later = demo_incident(), demo_incident("later", 11)
    await actions.poll(core, [later, first], [])
    core.register_metric.assert_awaited_once_with(harness.DEMO_METRICS["fee_per_order"])
    assert actions.incident_id == first.id
    assert actions.completed == {"on_incident_open"}
    actions.scenarios.clear.assert_not_called()
    await actions.poll(
        core, [first, later], [seller_ack(first, False), seller_ack(later)]
    )
    actions.scenarios.clear.assert_not_called()
    await actions.poll(core, [first, later], [seller_ack(first)])
    await actions.poll(core, [first, later], [seller_ack(first)])
    actions.scenarios.clear.assert_called_once_with("S8")
    core.register_metric.assert_awaited_once()
    assert actions.completed == {"on_incident_open", "on_selleros_ack"}


@pytest.mark.asyncio
async def test_conditional_action_failure_is_not_marked_complete() -> None:
    actions = demo_actions()
    core = SimpleNamespace(
        register_metric=AsyncMock(side_effect=ValueError("registration rejected")),
    )
    incident = demo_incident()
    with pytest.raises(ValueError, match="registration rejected"):
        await actions.poll(core, [incident], [seller_ack(incident)])
    assert not actions.completed
    actions.scenarios.clear.assert_not_called()


@pytest.mark.asyncio
async def test_conditional_clear_requires_a_simulator_manager() -> None:
    entry = {"play": ["S8@0"], "on_incident_open": "clear(S8)", "expect": {}}
    actions = harness.ConditionalActions(entry, {})
    with pytest.raises(RuntimeError, match="simulator scenario manager"):
        await actions.poll(SimpleNamespace(), [demo_incident()], [])
    assert not actions.completed


@pytest.mark.asyncio
async def test_conditional_actions_parse_other_targets() -> None:
    metric = AddMetricRequest(
        name="another_metric",
        description="Another metric",
        sql="SELECT ts, units AS num FROM orders_hourly",
    )
    actions = harness.ConditionalActions(
        {
            "expect": {"entry_metrics_any": ["aov"]},
            "on_incident_open": " clear( S4 ) ",
            "on_selleros_ack": "add_metric(another_metric)",
        },
        {"another_metric": metric},
        SimpleNamespace(clear=Mock()),
    )
    core = SimpleNamespace(register_metric=AsyncMock())
    incident = demo_incident()
    await actions.poll(core, [incident], [])
    actions.scenarios.clear.assert_called_once_with("S4")
    core.register_metric.assert_not_awaited()
    await actions.poll(core, [incident], [seller_ack(incident)])
    core.register_metric.assert_awaited_once_with(metric)


@pytest.mark.parametrize(
    "action", ["unknown(S8)", "clear(S8); clear(S8)", "add_metric(unknown_metric)"]
)
def test_invalid_conditional_actions_fail_clearly(action: str) -> None:
    with pytest.raises(ValueError):
        harness.ConditionalActions({"on_incident_open": action}, harness.DEMO_METRICS)


def test_demo_sql_matches_documented_cartographer_example() -> None:
    assert (
        harness.DEMO_METRICS["fee_per_order"].sql
        == """SELECT ts, country, platform, category, seller_tier, channel,
SUM(fee_revenue) AS num, SUM(units) AS den FROM orders_hourly
GROUP BY ts, country, platform, category, seller_tier, channel"""
    )


@pytest.mark.asyncio
async def test_s8_registers_its_expected_demo_metric_on_open() -> None:
    entry = deepcopy(harness.load_oracle()["S8"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    actions = harness.ConditionalActions(
        entry, harness.DEMO_METRICS, SimpleNamespace(clear=Mock())
    )
    core = SimpleNamespace(register_metric=AsyncMock())
    await actions.poll(core, [demo_incident()], [])
    core.register_metric.assert_awaited_once_with(
        harness.DEMO_METRICS[entry["expect"]["metric_attached"]["metric"]]
    )


@pytest.mark.parametrize("confirmed", [True, False])
def test_verdict_sequence_uses_only_matching_investigation_snapshots(
    confirmed: bool,
) -> None:
    incident = make_incident_s7()
    step = TrailStep(
        id="health",
        investigation_id="sentinel",
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="sentinel",
        action="tool_call",
        tool="check_freshness",
        output_summary="Fresh",
        output={"verdict": "ok"},
    )
    hypothesis = step.model_copy(
        update={
            "id": "hypothesis",
            "investigation_id": incident.investigation_ids[0],
            "actor": "orchestrator",
            "action": "decision",
            "tool": None,
            "output": {"verdict": "HYPOTHESIS"},
        }
    )
    confirmation = hypothesis.model_copy(
        update={
            "id": "confirmation",
            "output": {"verdict": "CONFIRMED"},
            "investigation_id": incident.investigation_ids[0]
            if confirmed
            else "unrelated",
        }
    )
    result = harness.evaluate(
        {"expect": {"verdict_sequence": ["HYPOTHESIS", "CONFIRMED"]}},
        [incident],
        [step, hypothesis, confirmation],
    )[0]
    assert result["passed"] == confirmed
    assert result["actual"] == (
        ["HYPOTHESIS", "CONFIRMED"] if confirmed else ["HYPOTHESIS"]
    )


@pytest.mark.parametrize("name", ["S8", "D6"])
def test_early_unrelated_alert_is_not_attributed_to_coupon_day(name: str) -> None:
    entry = deepcopy(harness.load_oracle()[name])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    incident = make_incident_s7()
    incident = incident.model_copy(
        update={
            "opened_ts": CAMPAIGN_TS,
            "root_cause": None,
            "hypotheses": [],
            "summary": "Toys ad revenue share changed",
            "entry": incident.entry.model_copy(
                update={"metric": "ad_revenue", "segment": {"category": "toys"}}
            ),
        }
    )
    checks = {c["check"]: c for c in harness.evaluate(entry, [incident], [])}
    assert checks["also.coupon_day.status_any"] == {
        "check": "also.coupon_day.status_any",
        "passed": True,
        "actual": [],
    }


def test_mentions_report_only_found_and_missing_tokens() -> None:
    incident = make_incident_s7().model_copy(
        update={
            "title": "",
            "summary": "Coupon day is planned",
            "suggested_actions": [],
            "open_questions": [],
        }
    )
    result = harness.evaluate(
        {"expect": {"must_mention": ["coupon", "planned", "SellerOS"]}}, [incident], []
    )[0]
    assert result["actual"] == {"found": ["coupon", "planned"], "missing": ["SellerOS"]}
    assert not result["passed"]


@pytest.mark.parametrize(
    "nodes,passed",
    [(["buybox_share", "in_stock_rate"], True), (["buybox_share"], False), ([], False)],
)
def test_structured_symptoms_required(nodes: list[str], passed: bool) -> None:
    incident = make_incident_s7().model_copy(
        update={
            "symptoms": [
                Symptom(node=node, first_change_ts=CAMPAIGN_TS + harness.HOUR)
                for node in nodes
            ],
            "summary": "buybox_share and in_stock_rate are symptoms",
        }
    )
    entry = {"expect": {"symptom_nodes_all": ["buybox_share", "in_stock_rate"]}}
    result = harness.evaluate(entry, [incident], [])[0]
    assert result["passed"] == passed
    assert result["actual"] == sorted(nodes)
    assert not harness.evaluate(entry, [], [])[0]["passed"]


@pytest.mark.asyncio
async def test_live_response_mapping_order_matches_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response(BaseModel):
        content: dict

    async def create(self: object, request: dict) -> Response:
        return Response(content={"segment": {"country": "DE", "channel": "search"}})

    monkeypatch.setattr(harness.LLMClient, "create", create)
    client = harness.EvalLLMClient(LlmSettings(), tmp_path / "requests.jsonl")
    response = await client.create(
        {"model": "test", "system": [], "tools": [], "messages": []}
    )
    assert list(response.content["segment"]) == ["channel", "country"]


@pytest.mark.asyncio
async def test_a2a_snapshot_replays_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cards(self: object) -> dict:
        return {
            "discovery": harness.AgentCardInfo(
                name="discovery", url="http://localhost:8200/discovery", healthy=False
            )
        }

    monkeypatch.setattr(harness.A2AClient, "fetch_cards", cards)
    fixture = tmp_path / "fixture.jsonl"
    recorded = harness.EvalA2AClient(A2aSettings(), "record", fixture)
    expected = await recorded.fetch_cards()
    await recorded.aclose()

    async def forbidden(self: object) -> dict:
        raise AssertionError("replay reached network boundary")

    monkeypatch.setattr(harness.A2AClient, "fetch_cards", forbidden)
    replayed = harness.EvalA2AClient(A2aSettings(), "replay", fixture)
    try:
        assert await replayed.fetch_cards() == expected
        assert replayed.replay_misses == 0
    finally:
        await replayed.aclose()


@pytest.mark.asyncio
async def test_local_specialist_uses_bound_eval_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    async def answer(
        module: object, client: object, request: object, settings: object
    ) -> tuple:
        seen.append((module.NAME, client, request, settings))
        return harness.SpecialistResponse(
            agent=module.NAME, answered=True, findings=[]
        ), []

    async def forbidden(*args: object) -> None:
        raise AssertionError("remote specialist reached")

    monkeypatch.setattr(harness, "answer_specialist", answer)
    monkeypatch.setattr(harness.A2AClient, "ask", forbidden)
    client = harness.EvalA2AClient(A2aSettings(), "live", tmp_path / "fixture.jsonl")
    client.local_data, client.local_settings = object(), object()
    try:
        request = harness.SpecialistRequest(
            investigation_id="eval",
            question="Why?",
            metric="orders",
            segment={},
            window_start_ts=CAMPAIGN_TS,
            window_end_ts=CAMPAIGN_TS + harness.HOUR,
            baseline_start_ts=CAMPAIGN_TS - harness.HOUR,
            baseline_end_ts=CAMPAIGN_TS,
            context="eval",
        )
        result = await client.ask("discovery", request)
        assert result.answered
        assert seen == [
            ("discovery", client.local_data, request, client.local_settings)
        ]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["live", "record"])
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("private exception detail"),
        harness.DataApiUnavailable("core data API unavailable"),
    ],
)
async def test_local_specialist_failure_records_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, error: Exception
) -> None:
    async def answer(*args: object) -> None:
        raise error

    async def forbidden(*args: object) -> None:
        raise AssertionError("replay reached specialist")

    monkeypatch.setattr(harness, "answer_specialist", answer)
    monkeypatch.setattr(harness.A2AClient, "ask", forbidden)
    request = harness.SpecialistRequest(
        investigation_id="eval",
        question="Why?",
        metric="orders",
        segment={},
        window_start_ts=CAMPAIGN_TS,
        window_end_ts=CAMPAIGN_TS + harness.HOUR,
        baseline_start_ts=CAMPAIGN_TS - harness.HOUR,
        baseline_end_ts=CAMPAIGN_TS,
        context="eval",
    )
    module = next(
        module for module in harness.SPECIALISTS if module.NAME == "discovery"
    )
    detail = (
        str(error)
        if isinstance(error, harness.DataApiUnavailable)
        else type(error).__name__
    )
    expected = f"{module.TITLE} failed: {detail}"
    path = tmp_path / "fixture.jsonl"
    client = harness.EvalA2AClient(A2aSettings(), mode, path)
    client.local_data, client.local_settings = object(), object()
    try:
        with pytest.raises(ValueError) as caught:
            await client.ask("discovery", request)
        assert str(caught.value) == expected
    finally:
        await client.aclose()
    if mode == "live":
        assert not path.exists()
        return
    entry = harness.json.loads(path.read_text())
    assert entry["error"] == {"type": "ValueError", "message": expected}
    assert "private exception detail" not in path.read_text()
    monkeypatch.setattr(harness, "answer_specialist", forbidden)
    replay = harness.EvalA2AClient(A2aSettings(), "replay", path)
    try:
        with pytest.raises(ValueError) as caught:
            await replay.ask("discovery", request)
        assert str(caught.value) == expected
        assert replay.replay_misses == 0
    finally:
        await replay.aclose()


def test_seeded_identity_streams() -> None:
    def values(seed: int) -> list:
        stream = harness.seeded_ids(seed, "S3", "detective")
        return [next(stream) for _ in range(3)]

    assert values(42) == values(42)
    assert len(set(values(42))) == 3
    assert values(42) != values(43)


@pytest.mark.parametrize(
    "segment,passed",
    [
        ({"category": "electronics", "seller_tier": "small_3P"}, True),
        ({"category": "electronics", "seller_tier": "large_3P"}, False),
        ({"category": "electronics"}, False),
        ({"category": "electronics", "seller_tier": "1P"}, False),
    ],
)
def test_attached_metric_requires_small_3p_electronics(
    segment: dict, passed: bool
) -> None:
    incident = make_incident_s7()
    step = TrailStep(
        id="attached-metric",
        investigation_id=incident.investigation_ids[0],
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="sentinel",
        action="decision",
        output_summary="Attached fee_per_order to the existing incident",
        output={
            "action": "attach",
            "attach_to_incident": incident.id,
            "anomaly": {"metric": "fee_per_order", "segment": segment},
        },
    )
    entry = {
        "expect": {
            "metric_attached": harness.load_oracle()["D6"]["expect"]["metric_attached"]
        }
    }
    assert harness.evaluate(entry, [incident], [step])[0]["passed"] == passed
    assert not harness.evaluate(entry, [incident], [])[0]["passed"]


def test_refuted_hypothesis_does_not_supply_root_or_segment() -> None:
    incident = make_incident_s7()
    entry = {
        "expect": {
            "root_nodes_all": ["payment_success_rate"],
            "segment_contains": {"psp": ["PSP-2"]},
        }
    }
    results = harness.evaluate(entry, [incident], [])
    assert not any(c["passed"] for c in results)


def test_contributions_and_trail_checks() -> None:
    incident = make_incident_s7().model_copy(
        update={
            "contributions": [
                Contribution(
                    factor="marketing_cost", kind="planned", delta=-55, share=0.55
                )
            ]
        }
    )
    entry = {
        "expect": {
            "root_nodes_all": ["marketing_cost"],
            "contribution_kind": "planned",
            "contribution_shares": {"marketing_cost": [0.45, 0.65]},
            "affected_sellers": ["S-03"],
            "notified_sellers": ["S-03"],
            "loop_contains": ["buybox_share"],
        }
    }
    step = TrailStep(
        id="eval",
        investigation_id="inv_s7",
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="orchestrator",
        action="decision",
        output_summary="1 notified seller",
        output={
            "affected_sellers": ["S-03"],
            "seller_ids": ["S-03"],
            "received": True,
            "loop": ["buybox_share"],
        },
    )
    assert all(c["passed"] for c in harness.evaluate(entry, [incident], [step]))
    assert not all(c["passed"] for c in harness.evaluate(entry, [incident], []))


def test_correct_report_passes() -> None:
    results = harness.evaluate(oracle_entry(), [make_incident_s7()], [])
    assert all(c["passed"] for c in results), results
    assert (
        next(c["actual"] for c in results if c["check"] == "max_ticks_to_detect") == 1
    )


@pytest.mark.parametrize(
    "field,value,check",
    [
        ("verdict", "HYPOTHESIS", "verdict_any"),
        ("summary", "No useful report", "must_mention"),
        ("suggested_actions", [], "suggested_actions_mention"),
        ("status", "resolved", "status_any"),
    ],
)
def test_failed_checks_include_actual(field: str, value: object, check: str) -> None:
    entry = oracle_entry()
    entry["expect"]["status_any"] = ["open"]
    incident = make_incident_s7().model_copy(update={field: value})
    results = harness.evaluate(entry, [incident], [])
    result = next(c for c in results if c["check"] == f"expect.{check}")
    assert not result["passed"]
    assert "actual" in result


def test_empty_business_scenario_fails() -> None:
    results = harness.evaluate(oracle_entry(), [], [])
    assert not any(c["passed"] for c in results if not c["check"].startswith("also."))


@pytest.mark.parametrize("name", ["I1", "coupon_day"])
def test_innocent_and_deferred_may_have_no_incident(name: str) -> None:
    assert all(
        c["passed"] for c in harness.evaluate(harness.load_oracle()[name], [], [])
    )


def test_i2_requires_real_deferral_evidence_even_without_incident() -> None:
    entry = harness.load_oracle()["I2"]
    assert not all(c["passed"] for c in harness.evaluate(entry, [], []))
    anomaly = make_incident_s7().entry.model_copy(
        update={"segment": {"country": "FR"}, "completeness": 0.4}
    )
    step = TrailStep(
        id="defer",
        investigation_id="sentinel",
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="sentinel",
        action="decision",
        output_summary="Incomplete FR partition",
        output={"action": "defer", "anomaly": anomaly.model_dump(mode="json")},
    )
    assert all(c["passed"] for c in harness.evaluate(entry, [], [step]))
    wrong = step.model_copy(
        update={
            "output": {"action": "ignore", "anomaly": anomaly.model_dump(mode="json")}
        }
    )
    assert not all(c["passed"] for c in harness.evaluate(entry, [], [wrong]))


@pytest.mark.parametrize(
    "verdict,country,passed",
    [
        ("DATA_ISSUE", "FR", True),
        ("CONFIRMED", "FR", False),
        ("DATA_ISSUE", "DE", False),
    ],
)
def test_resolved_data_issue_supplies_matching_deferral_evidence(
    verdict: str, country: str, passed: bool
) -> None:
    incident = make_incident_s7()
    incident = incident.model_copy(
        update={
            "status": "resolved",
            "verdict": verdict,
            "entry": incident.entry.model_copy(
                update={"segment": {"country": country}}
            ),
        }
    )
    results = harness.evaluate(
        {"expect": {"deferral_required": {"country": "FR"}}}, [incident], []
    )
    assert results[0]["passed"] == passed


def test_duplicate_incident_fails() -> None:
    incident = make_incident_s7()
    duplicate = incident.model_copy(update={"id": "duplicate"})
    results = harness.evaluate(oracle_entry(), [incident, duplicate], [])
    count = next(
        c for c in results if c["check"] == "expect.max_incidents_for_scenario"
    )
    assert count == {
        "check": "expect.max_incidents_for_scenario",
        "passed": False,
        "actual": 2,
    }


@pytest.mark.parametrize("name", ["S7", "D5"])
def test_campaign_false_alarm_before_checkout_is_not_dropped(name: str) -> None:
    entry = deepcopy(harness.load_oracle()[name])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    good = make_incident_s7()
    early = good.model_copy(
        update={
            "id": "campaign-alert",
            "opened_ts": CAMPAIGN_TS,
            "summary": "black-friday-2026 campaign traffic rose",
            "entry": good.entry.model_copy(update={"metric": "sessions"}),
            "root_cause": good.root_cause.model_copy(
                update={
                    "cause_type": "traffic_change",
                    "statement": "black-friday-2026 campaign drove traffic",
                }
            ),
            "status": "open",
        }
    )
    cleared = early.model_copy(
        update={"id": "cleared", "status": "innocent", "verdict": "INNOCENT"}
    )
    results = {
        c["check"]: c for c in harness.evaluate(entry, [good, cleared, early], [])
    }
    assert not results["also.I1.status_any"]["passed"]
    assert "open" in results["also.I1.status_any"]["actual"]


@pytest.mark.parametrize(
    "name,campaign_id", [("S7", "black-friday-2026"), ("S8", "coupon-day-electronics")]
)
def test_campaign_mention_in_non_campaign_root_is_not_attribution(
    name: str, campaign_id: str
) -> None:
    entry = deepcopy(harness.load_oracle()[name])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    good = make_incident_s7()
    scenario, hour = next(p for p in entry["play"] if p.startswith("S")).rsplit("@", 1)
    offset = int(hour) + entry.get("scenario_params", {}).get(scenario, {}).get(
        "typo_delay_hours", 0
    )
    good = good.model_copy(
        update={
            "opened_ts": CAMPAIGN_TS + offset * harness.HOUR,
            "entry": good.entry.model_copy(
                update={"metric": entry["expect"]["entry_metrics_any"][0]}
            ),
            "root_cause": good.root_cause.model_copy(
                update={
                    "cause_type": "release",
                    "statement": f"Checkout outage during {campaign_id}",
                }
            ),
        }
    )
    results = {c["check"]: c for c in harness.evaluate(entry, [good], [])}
    campaign = "I1" if name == "S7" else "coupon_day"
    assert results[f"also.{campaign}.status_any"]["passed"]
    assert results[f"also.{campaign}.status_any"]["actual"] == []


@pytest.mark.parametrize("fault", ["S1", "S3"])
def test_simultaneous_campaign_false_alarm_is_not_a_chosen_fault_report(
    fault: str,
) -> None:
    entry = deepcopy(harness.load_oracle()["D1"])
    entry.update(deepcopy(harness.load_oracle()[fault]))
    entry["play"] = ["I1@0", f"{fault}@0"]
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    spec = entry["expect"]
    good = make_incident_s7()
    segment = {key: values[0] for key, values in spec["segment_contains"].items()}
    good = good.model_copy(
        update={
            "opened_ts": CAMPAIGN_TS,
            "summary": " ".join(spec["must_mention"]),
            "entry": good.entry.model_copy(
                update={"metric": spec["entry_metrics_any"][0], "segment": segment}
            ),
            "root_cause": good.root_cause.model_copy(
                update={
                    "cause_type": spec["cause_type"],
                    "root_node": spec["root_node_any"][0],
                    "segment": segment,
                }
            ),
        }
    )
    alarm = good.model_copy(
        update={
            "id": "campaign-alarm",
            "summary": "black-friday-2026 traffic surge",
            "entry": good.entry.model_copy(update={"metric": "sessions"}),
            "root_cause": good.root_cause.model_copy(
                update={
                    "cause_type": "traffic_change",
                    "root_node": "sessions",
                    "statement": "black-friday-2026 caused the traffic surge",
                }
            ),
        }
    )
    results = {c["check"]: c for c in harness.evaluate(entry, [good, alarm], [])}
    assert results["expect.cause_type"]["actual"] == [spec["cause_type"]]
    assert results["also.I1.status_any"]["actual"] == ["open"]
    assert not results["also.I1.status_any"]["passed"]


def test_detection_deadline_uses_the_best_report_candidate() -> None:
    good = make_incident_s7()
    wrong = good.model_copy(
        update={
            "id": "early-wrong",
            "verdict": "HYPOTHESIS",
            "root_cause": None,
            "summary": "Wrong explanation",
        }
    )
    late = good.model_copy(update={"opened_ts": CAMPAIGN_TS + 5 * harness.HOUR})
    results = {
        c["check"]: c for c in harness.evaluate(oracle_entry(), [wrong, late], [])
    }
    assert results["expect.verdict_any"]["actual"] == ["CONFIRMED"]
    assert results["max_ticks_to_detect"]["actual"] == 3
    assert not results["max_ticks_to_detect"]["passed"]


@pytest.mark.parametrize("name,offset", [("S2", 18), ("S4", 70), ("S5", 80)])
@pytest.mark.parametrize("case", ["on_time", "late", "missing"])
def test_combined_run_scores_each_subscenario_deadline(
    name: str, offset: int, case: str
) -> None:
    entry = deepcopy(harness.load_oracle()["D4"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    spec = entry["also"][name]
    incident = make_incident_s7()
    hour = offset + (spec["max_ticks_to_detect"] if case == "late" else 0)
    incident = incident.model_copy(
        update={
            "opened_ts": CAMPAIGN_TS + hour * harness.HOUR,
            "entry": incident.entry.model_copy(
                update={"metric": spec["expect"]["entry_metrics_any"][0]}
            ),
        }
    )
    results = {
        c["check"]: c
        for c in harness.evaluate(entry, [] if case == "missing" else [incident], [])
    }
    check = results[f"also.{name}.max_ticks_to_detect"]
    assert check["passed"] == (case == "on_time")
    assert check["actual"] == (None if case == "missing" else hour - offset + 1)


def test_unknown_check_errors() -> None:
    with pytest.raises(ValueError, match="Unknown oracle check"):
        harness.evaluate({"expect": {"typo": True}}, [], [])


def test_forbidden_root_and_insufficient_evidence_fail() -> None:
    incident = make_incident_s7()
    root = incident.root_cause.model_copy(
        update={"cause_type": "traffic_change", "evidence_for": []}
    )
    incident = incident.model_copy(update={"root_cause": root})
    results = harness.evaluate(oracle_entry(), [incident], [])
    assert not next(
        c["passed"] for c in results if c["check"] == "expect.must_not_root_cause"
    )
    assert not next(
        c["passed"] for c in results if c["check"] == "expect.evidence_groups_min"
    )


def test_reports_cannot_supply_each_others_checks() -> None:
    good = make_incident_s7()
    first = good.model_copy(update={"verdict": "HYPOTHESIS"})
    second = good.model_copy(update={"summary": "Nothing relevant", "id": "other"})
    assert not all(
        c["passed"] for c in harness.evaluate(oracle_entry(), [first, second], [])
    )
