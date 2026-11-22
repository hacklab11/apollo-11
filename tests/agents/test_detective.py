import json
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import duckdb
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_runtime import Budget, InMemoryKillSwitch
from eye.agents.detective import Detective, _Submission
from eye.config import DetectiveSettings, LlmSettings
from eye.contracts_v2 import (
    Anomaly,
    Contribution,
    Edge,
    Evidence,
    Hypothesis,
    TrailStep,
    Verdict,
)
from eye.graph.store import GraphStore
from eye.registry.loader import Registry, load_registry
from eye.sim.store import Store
from eye.warehouse.client import DataUnavailable

START = datetime(2026, 11, 27, 3, tzinfo=UTC)
HOUR = timedelta(hours=1)
END = START + 2 * HOUR


@pytest.fixture(autouse=True)
def model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "eye.agents.detective.LlmSettings",
        lambda: LlmSettings(
            prices={"detective": {"input_per_mtok": 0, "output_per_mtok": 0}}
        ),
    )


@pytest.fixture
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    with closing(Store(":memory:")) as store, store.cursor() as con:
        yield con


def seed(con: duckdb.DuckDBPyConnection, case: str) -> None:
    # The scenario injector is independent of these tests; seed its observable signatures.
    con.execute(
        """
        CREATE TEMP TABLE hours AS SELECT ts, ts >= ? AS broken
        FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ, INTERVAL 1 HOUR) AS t(ts)
        """,
        [START, START - timedelta(weeks=4), END + HOUR],
    )
    if case == "s3":
        con.execute("""
            INSERT INTO search_hourly
            SELECT ts, country, platform, 'electronics', 1024,
                   CASE WHEN broken AND country = 'DE' THEN 256 ELSE 512 END,
                   CASE WHEN broken AND country = 'DE' THEN 0.4375 ELSE 0.875 END
            FROM hours, unnest(['DE','FR']) c(country),
                 unnest(['android','ios','web']) p(platform);
            INSERT INTO orders_hourly
            SELECT ts, country, platform, category, 'E01', 'S01', '1P', 'search',
                   CASE WHEN relevance < 0.5 THEN 64 ELSE 128 END,
                   CASE WHEN relevance < 0.5 THEN 1280 ELSE 2560 END, 0, 0, 0, 0
            FROM search_hourly;
            INSERT INTO change_log VALUES
              ('ranker', '2026-11-27T03:00:00Z', 'search-ranker', 'release', false,
               '{"country":"DE"}', 'search-ranker v7.2', NULL),
              ('fr-ui', '2026-11-27T03:00:00Z', 'search-ui', 'release', false,
               '{"country":"FR"}', 'search-ui v3.0', NULL),
              ('android', '2026-11-27T01:00:00Z', 'app-android', 'release', false,
               '{}', 'Android app release', NULL);
            """)
    else:
        con.execute("""
            INSERT INTO checkout_hourly
            SELECT ts, 'DE', platform, 8192,
                   CASE WHEN broken AND platform <> 'ios' THEN 512 ELSE 4096 END,
                   CASE WHEN broken AND platform <> 'ios' THEN 480 ELSE 3840 END
            FROM hours, unnest(['web','android','ios']) p(platform);
            INSERT INTO payments_hourly
            SELECT ts, country, platform, 'PSP-2', payment_attempts,
                   payment_attempts * 0.9375, CASE WHEN ts >= '2026-11-27T02:00:00Z' THEN 820 ELSE 200 END
            FROM checkout_hourly;
            INSERT INTO client_errors_hourly
            SELECT ts, platform,
                   CASE WHEN broken THEN 'v5.8' ELSE 'v5.7' END,
                   'checkout', 'TypeError', CASE WHEN broken THEN 3584 ELSE 0 END
            FROM hours, unnest(['web','android']) p(platform);
            INSERT INTO app_components VALUES
              ('web','checkout','checkout-web','v5.8'),
              ('android','checkout','checkout-web','v5.8'),
              ('ios','checkout','native-ios','v3.2');
            INSERT INTO orders_hourly
            SELECT ts, country, platform, 'electronics', 'E01', 'S01', '1P', 'search',
                   successes, successes * 32, 0, 0, 0, 0 FROM payments_hourly;
            INSERT INTO change_log VALUES
              ('black-friday', '2026-11-27T00:00:00Z', 'marketing', 'campaign', true,
                '{}', 'Black Friday campaign', '{"gmv":"+35%","orders":"+35%"}'),
              ('checkout', '2026-11-27T03:00:00Z', 'checkout-web', 'release', false,
                '{"platform":["web","android"]}', 'checkout-web v5.8', NULL),
              ('android', '2026-11-27T01:00:00Z', 'app-android', 'release', false,
                '{"platform":"android"}', 'v9.14 rollout (20%)', NULL);
            INSERT INTO external_status VALUES
              ('psp2','2026-11-27T02:00:00Z','PSP-2','degraded','Elevated latency');
            """)


def anomaly(case: str) -> Anomaly:
    value, expected = (0.0625, 0.125) if case == "s3" else (288000, 691200)
    return Anomaly(
        id=case,
        ts=END - HOUR,
        metric="search_conversion" if case == "s3" else "gmv",
        segment={"country": "DE"},
        kind="divergent_segment",
        actual=value,
        expected=expected,
        delta=value - expected,
        delta_pct=value / expected - 1,
        z=-10,
        window_hours=2,
        impact_eur_per_hour=0 if case == "s3" else 201600,
        completeness=1,
        priority=10,
    )


def make_graph(registry: Registry, case: str) -> GraphStore:
    graph = GraphStore(None)
    for metric in registry.metrics.values():
        graph.add_metric(metric)
    pairs = (
        [("search_relevance", "search_conversion"), ("search_ctr", "search_conversion")]
        if case == "s3"
        else [
            ("orders", "gmv"),
            ("cart_to_checkout_rate", "orders"),
            ("payment_success_rate", "orders"),
            ("client_error_rate", "cart_to_checkout_rate"),
        ]
    )
    for src, dst in pairs:
        graph.add_edge(
            Edge(
                id=f"{src}:{dst}",
                src=src,
                dst=dst,
                type="causal_hypothesis",
                status="hypothesis",
                confidence=0.5,
                evidence={},
                created_by="seed",
            )
        )
    return graph


def hypothesis() -> Hypothesis:
    return Hypothesis(
        id="H1",
        statement="ranker release",
        cause_type="release",
        root_node="search_relevance",
        segment={"country": "DE"},
        first_change_ts=START,
        explained_share=0.9,
        status="supported",
        evidence_for=[
            Evidence(
                group="data",
                source="baseline_compare",
                step_id="baseline_compare",
                summary="Conversion fell by half.",
            ),
            Evidence(
                group="data",
                source="compare_segments",
                step_id="compare_segments",
                summary="DE fell while FR was steady.",
            ),
        ],
    )


def submission(root: Hypothesis, verdict: Verdict = "CONFIRMED") -> _Submission:
    return _Submission(
        verdict=verdict,
        root_cause=root,
        hypotheses=[root],
        summary="Ranker release.",
        contributions=[
            Contribution(factor="search_relevance", kind="rate", delta=-0.05, share=0.8)
        ],
        open_questions=[],
        suggested_actions=[],
    )


def scripted(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict]]
) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = sum(isinstance(m, ModelResponse) for m in messages)
        name, args = calls[min(index, len(calls) - 1)]
        if name == "submit":
            name = next(
                t.name for t in info.output_tools if t.name.endswith("_Submission")
            )
        return ModelResponse(parts=[ToolCallPart(name, args)])

    model = FunctionModel(respond, model_name="detective")
    monkeypatch.setattr("eye.agents.detective.build_model", lambda *_: model)
    return model


@pytest.mark.asyncio
async def test_data_unavailable_retries_and_preserves_board(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)

    def unavailable(*args: object) -> None:
        raise DataUnavailable("Warehouse request failed")

    monkeypatch.setattr("eye.agents.detective._WindowTools.baseline", unavailable)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = sum(isinstance(message, ModelResponse) for message in messages)
        if turn == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "update_hypotheses",
                        {"hypotheses": [hypothesis().model_dump(mode="json")]},
                    )
                ]
            )
        if turn == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("baseline_compare", {"metric": "search_conversion"})
                ]
            )
        assert any(
            isinstance(part, ToolReturnPart)
            and part.outcome == "retried"
            and part.content == "Warehouse request failed"
            for message in messages
            for part in message.parts
        )
        name = next(t.name for t in info.output_tools if t.name.endswith("GaveUp"))
        return ModelResponse(parts=[ToolCallPart(name, {"reason": "Data unavailable"})])

    result = await Detective(
        world,
        registry,
        make_graph(registry, "s3"),
        model=FunctionModel(respond, model_name="detective"),
    ).investigate(anomaly("s3"), AsyncMock())
    assert result.stop_reason == "budget"
    assert result.verdict == "HYPOTHESIS"
    assert result.hypotheses[0].statement == hypothesis().statement


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError, KeyError, DataUnavailable])
async def test_failed_tool_calls_are_recorded(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    seed(world, "s3")
    registry = load_registry(world)

    def fail(*args: object) -> None:
        raise error("Tool failed")

    monkeypatch.setattr("eye.agents.detective._WindowTools.baseline", fail)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(message, ModelResponse) for message in messages):
            return ModelResponse(
                parts=[
                    ToolCallPart("baseline_compare", {"metric": "search_conversion"})
                ]
            )
        name = next(t.name for t in info.output_tools if t.name.endswith("GaveUp"))
        return ModelResponse(parts=[ToolCallPart(name, {"reason": "Tool failed"})])

    trail = AsyncMock()
    await Detective(
        world,
        registry,
        make_graph(registry, "s3"),
        model=FunctionModel(respond, model_name="detective"),
    ).investigate(anomaly("s3"), trail)
    steps = [call.args[0] for call in trail.await_args_list]
    failed = [step for step in steps if step.tool == "baseline_compare"]
    assert len(failed) == 1
    assert failed[0].action == "tool_call"
    assert failed[0].input == {"metric": "search_conversion"}
    assert failed[0].output is None
    assert failed[0].output_summary == f"{error.__name__}: tool baseline_compare failed"
    assert [step.n for step in steps] == list(range(1, len(steps) + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending,expected", [("gave_up", "budget"), ("needs_input", "error")]
)
async def test_explicit_end_states_preserve_board(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
    expected: str,
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "update_hypotheses",
                        {"hypotheses": [hypothesis().model_dump(mode="json")]},
                    )
                ]
            )
        suffix = "GaveUp" if ending == "gave_up" else "NeedsInput"
        name = next(t.name for t in info.output_tools if t.name.endswith(suffix))
        return ModelResponse(
            parts=[
                ToolCallPart(
                    name,
                    {"reason": "Insufficient evidence"}
                    if ending == "gave_up"
                    else {"question": "Which release?"},
                )
            ]
        )

    result = await Detective(
        world,
        registry,
        make_graph(registry, "s3"),
        model=FunctionModel(respond, model_name="detective"),
    ).investigate(anomaly("s3"), AsyncMock())
    assert result.stop_reason == expected
    assert result.verdict == "HYPOTHESIS"
    assert result.hypotheses[0].statement == hypothesis().statement
    assert result.steps_used == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop", ["loop", "steps", "budget", "killed", "invalid", "empty"]
)
async def test_guarded_stops(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if stop == "empty":
            return ModelResponse(parts=[])
        if stop == "invalid":
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        next(
                            t.name
                            for t in info.output_tools
                            if t.name.endswith("_Submission")
                        ),
                        {"summary": "missing fields"},
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "update_hypotheses",
                    {
                        "hypotheses": []
                        if stop == "loop"
                        else [
                            hypothesis()
                            .model_copy(update={"id": str(calls)})
                            .model_dump(mode="json")
                        ]
                    },
                )
            ]
        )

    kill = InMemoryKillSwitch()
    if stop == "killed":
        kill.kill_system("test shutdown")
    budget = (
        Budget(tokens=0, cost_usd=1, deadline=datetime.now(UTC) + HOUR)
        if stop == "budget"
        else None
    )
    result = await Detective(
        world,
        registry,
        make_graph(registry, "s3"),
        settings=DetectiveSettings(max_steps=5),
        model=FunctionModel(respond, model_name="detective"),
        kill_switch=kill,
    ).investigate(anomaly("s3"), AsyncMock(), budget=budget)
    assert result.verdict == "HYPOTHESIS"
    assert result.stop_reason == (
        "error" if stop in {"killed", "invalid", "empty"} else "budget"
    )
    assert calls <= 5


@pytest.mark.asyncio
async def test_tools_cite_exact_calls(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    answer = submission(hypothesis(), "HYPOTHESIS")
    answer.root_cause.evidence_for = [
        Evidence(
            group="change_log",
            source="list_events",
            step_id=ref,
            summary="Release observed",
        )
        for ref in ("ref-1", "ref-2", "list_events")
    ]
    scripted(
        monkeypatch,
        [
            ("list_events", {"planned": False}),
            ("list_events", {"planned": True}),
            ("submit", answer.model_dump(mode="json")),
        ],
    )
    steps: list[TrailStep] = []

    async def trail(step: TrailStep) -> None:
        steps.append(step)

    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), trail
    )
    tool_steps = [s for s in steps if s.action == "tool_call"]
    assert result.root_cause is not None
    assert [e.step_id for e in result.root_cause.evidence_for] == [
        tool_steps[0].id,
        tool_steps[1].id,
        "",
    ]


@pytest.mark.asyncio
async def test_validated_output_and_tool_repair(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    root = hypothesis()
    root.evidence_for[1].group = "change_log"
    root.evidence_for[0].step_id = "ref-1"
    root.evidence_for[1].step_id = "ref-2"
    answer = submission(root)
    scripted(
        monkeypatch,
        [
            ("baseline_compare", {"metric": "nonexistent"}),
            (
                "baseline_compare",
                {"metric": "search_conversion", "segment": {"country": "DE"}},
            ),
            ("list_events", {}),
            ("submit", {"summary": "incomplete"}),
            ("submit", answer.model_dump(mode="json")),
        ],
    )
    usage = []
    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), AsyncMock(), on_usage=usage.append
    )
    assert result.verdict == "CONFIRMED"
    assert result.stop_reason == "confirmed"
    assert result.steps_used == 5
    assert result.root_cause and all(e.step_id for e in result.root_cause.evidence_for)
    assert usage[0].requests == 5


@pytest.mark.asyncio
async def test_provider_failure_becomes_error(world: duckdb.DuckDBPyConnection) -> None:
    seed(world, "s3")
    registry = load_registry(world)

    def unavailable(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(503, "detective")

    result = await Detective(
        world,
        registry,
        make_graph(registry, "s3"),
        model=FunctionModel(unavailable, model_name="detective"),
    ).investigate(anomaly("s3"), AsyncMock())
    assert result.stop_reason == "error"
    assert result.verdict == "HYPOTHESIS"
    assert "503" in result.open_questions[0]


@pytest.mark.asyncio
async def test_missing_credentials_become_error(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    monkeypatch.delenv("DETECTIVE_TEST_KEY", raising=False)
    result = await Detective(
        world,
        registry,
        make_graph(registry, "s3"),
        llm=LlmSettings(provider="openai_compatible", api_key_env="DETECTIVE_TEST_KEY"),
    ).investigate(anomaly("s3"), AsyncMock())
    assert result.stop_reason == "error"
    assert result.verdict == "HYPOTHESIS"
    assert "DETECTIVE_TEST_KEY" in result.open_questions[0]


@pytest.mark.asyncio
async def test_local_hypothesis_ids_do_not_collide_across_investigations(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)

    answer = submission(hypothesis(), "HYPOTHESIS")
    answer.trigger = "H1"
    answer.contributions[0].factor = "H1"
    scripted(monkeypatch, [("submit", answer.model_dump(mode="json"))])
    detective = Detective(world, registry, make_graph(registry, "s3"))
    first = await detective.investigate(
        anomaly("s3"), AsyncMock(), investigation_id="a"
    )
    second = await detective.investigate(
        anomaly("s3"), AsyncMock(), investigation_id="b"
    )
    for result in (first, second):
        assert result.root_cause is not None
        assert result.trigger == result.root_cause.id == result.hypotheses[0].id
        assert result.contributions[0].factor == result.trigger
    assert first.trigger != second.trigger


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [None, "search_relevance", "ranker release"])
async def test_loop_fills_missing_fields_from_measured_change_points(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str | None,
) -> None:
    seed(world, "s3")
    world.execute(
        "UPDATE search_hourly SET relevance = 0.4375 WHERE ts = ? AND country = 'DE'",
        [START - HOUR],
    )
    registry = load_registry(world)
    graph = make_graph(registry, "s3")
    graph.add_edge(
        Edge(
            id="feedback",
            src="search_conversion",
            dst="search_relevance",
            type="causal_hypothesis",
            status="hypothesis",
            confidence=0.5,
            evidence={},
            created_by="seed",
        )
    )
    answer = submission(hypothesis(), "HYPOTHESIS")
    answer.trigger = trigger
    scripted(monkeypatch, [("submit", answer.model_dump(mode="json"))])
    result = await Detective(world, registry, graph).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert result.loop is not None and set(result.loop) == {
        "search_conversion",
        "search_relevance",
    }
    assert result.root_cause is not None
    assert result.trigger == result.root_cause.id


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["DATA_ISSUE", "INNOCENT", "MIXED"])
async def test_other_unsubstantiated_verdicts_are_downgraded(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    verdict: Verdict,
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    scripted(
        monkeypatch,
        [("submit", submission(hypothesis(), verdict).model_dump(mode="json"))],
    )
    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert result.verdict == "HYPOTHESIS"
    assert result.open_questions


@pytest.mark.asyncio
async def test_data_issue_requires_stale_entry_window(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(world, "s3")
    world.execute(
        "INSERT INTO partition_status VALUES ('search_hourly', 'DE', ?, 100, 40, ?)",
        [START, START + HOUR],
    )
    registry = load_registry(world)
    scripted(
        monkeypatch,
        [("submit", submission(hypothesis(), "DATA_ISSUE").model_dump(mode="json"))],
    )
    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert result.verdict == "DATA_ISSUE"
    assert result.stop_reason == "data_issue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause_mode", ["root", "measured", "trigger", "none", "pending", "scoped"]
)
async def test_downstream_symptoms(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    cause_mode: str,
) -> None:
    seed(world, "s3")
    world.execute(
        "UPDATE orders_hourly SET units = 128, gmv = 2560 WHERE ts = ? AND country = 'DE'",
        [START],
    )
    registry = load_registry(world)
    graph = make_graph(registry, "s3")
    for src, dst in [
        ("search_conversion", "orders"),
        ("orders", "gmv"),
        ("search_relevance", "search_ctr"),
        ("search_relevance", "searches"),
    ]:
        graph.add_edge(
            Edge(
                id=f"{src}:{dst}",
                src=src,
                dst=dst,
                type="causal_hypothesis",
                status="hypothesis",
                confidence=0.5,
                evidence={},
                created_by="seed",
            )
        )
    if cause_mode == "pending":
        registry.get("search_ctr").status = "pending"
    answer = submission(hypothesis(), "HYPOTHESIS")
    if cause_mode == "scoped":
        world.execute(
            "UPDATE orders_hourly SET units = 128, gmv = 2560 WHERE country = 'DE' AND platform = 'web'"
        )
        answer.root_cause.segment = {"country": "DE", "platform": "web"}
    if cause_mode == "measured":
        answer.root_cause.first_change_ts = None
    if cause_mode in {"trigger", "none"}:
        answer.root_cause = None
        answer.trigger = "H1" if cause_mode == "trigger" else None
    scripted(monkeypatch, [("submit", answer.model_dump(mode="json"))])
    steps: list[TrailStep] = []

    async def trail(step: TrailStep) -> None:
        steps.append(step)

    entry = anomaly("s3").model_copy(update={"ts": END})
    result = await Detective(world, registry, graph).investigate(entry, trail)
    symptoms = [item.model_dump(mode="json") for item in result.symptoms]
    print("symptoms=" + json.dumps(symptoms, sort_keys=True))
    expected = (
        []
        if cause_mode in {"none", "scoped"}
        else ["gmv", "orders", "search_conversion"]
    )
    assert [item.node for item in result.symptoms] == expected
    assert all(item.first_change_ts == START + HOUR for item in result.symptoms)
    assert bool(expected) == any(
        step.action == "note" and step.tool == "first_change" for step in steps
    )


@pytest.mark.asyncio
async def test_two_failed_calls_to_one_tool_do_not_stop_investigation(
    world: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    bad = {"metric": "not_a_metric", "dim": "country"}
    answer = submission(hypothesis(), "HYPOTHESIS")
    scripted(
        monkeypatch,
        [
            ("mix_rate_split", bad),
            ("mix_rate_split", bad),
            ("submit", answer.model_dump(mode="json")),
        ],
    )
    result = await Detective(world, registry, make_graph(registry, "s3")).investigate(
        anomaly("s3"), AsyncMock()
    )
    assert not any("max retries" in q for q in result.open_questions)
    assert result.root_cause is not None


@pytest.mark.asyncio
async def test_logs_tool_failures_and_outcome(
    world: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed(world, "s3")
    registry = load_registry(world)
    scripted(
        monkeypatch,
        [
            ("mix_rate_split", {"metric": "not_a_metric", "dim": "country"}),
            ("submit", submission(hypothesis(), "HYPOTHESIS").model_dump(mode="json")),
        ],
    )
    with caplog.at_level("INFO", logger="eye.agents.detective"):
        await Detective(world, registry, make_graph(registry, "s3")).investigate(
            anomaly("s3"), AsyncMock(), investigation_id="inv-log"
        )
    failed = [r.getMessage() for r in caplog.records if "tool_failed" in r.getMessage()]
    assert failed and "tool=mix_rate_split" in failed[0]
    assert "not_a_metric" in failed[0]
    finished = [r for r in caplog.records if "detective finished" in r.getMessage()]
    assert "investigation=inv-log" in finished[0].getMessage()
    assert "verdict=HYPOTHESIS" in finished[0].getMessage()
