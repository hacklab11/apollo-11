import ast
import importlib.util
import json
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from selleros.data import ScopedData, Selection

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_evals", ROOT / "scripts/agent_evals.py"
)
evals = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evals
SPEC.loader.exec_module(evals)


def test_every_production_pydantic_agent_is_covered() -> None:
    found = set()
    for path in (ROOT / "packages").rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
            for node in ast.walk(tree)
        ):
            found.add("selleros" if "selleros" in path.parts else path.stem)
    assert found == evals.AGENTS


def test_regressions_auto_discovered(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text((evals.CASES / "s8.yaml").read_text())
    regression = tmp_path / "regressions"
    regression.mkdir()
    original = evals.load_cases().cases[0]
    (regression / "captured.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "captured-failure",
                        "inputs": original.inputs.model_dump(mode="json"),
                        "expected_output": {"decisions": []},
                    }
                ]
            }
        )
    )
    assert "captured-failure" in {
        case.name for case in evals.load_cases(tmp_path).cases
    }


def test_empty_cases_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No eval cases"):
        evals.load_cases(tmp_path)


def test_score_normal_empty_and_wrong_decisions() -> None:
    evaluator = evals.DecisionScore()
    assert (
        evaluator.evaluate(
            SimpleNamespace(output={"decisions": []}, expected_output={"decisions": []})
        )
        == 1
    )
    assert (
        evaluator.evaluate(
            SimpleNamespace(
                output={"verdict": "CONFIRMED", "summary": "ok"},
                expected_output={"verdict": "CONFIRMED"},
            )
        )
        == 1
    )
    assert (
        evaluator.evaluate(
            SimpleNamespace(
                output={"verdict": "HYPOTHESIS"},
                expected_output={"verdict": "CONFIRMED"},
            )
        )
        == 0
    )
    with pytest.raises(ValueError, match="expected decision fields"):
        evaluator.evaluate(SimpleNamespace(output={}, expected_output={}))


def test_configured_fallbacks_are_in_matrix(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        "llm:\n  models: {default: primary}\n  fallback_models: {sentinel: [backup, primary]}\n"
    )
    assert evals.model_matrix(path)["sentinel"] == ["primary", "backup"]


@pytest.mark.asyncio
async def test_offline_suite_never_opens_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args, **kwargs):
        raise AssertionError("Offline eval attempted network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    assert await evals.evaluate()


@pytest.mark.asyncio
async def test_live_global_request_cap_prevents_provider_call() -> None:
    calls = 0

    def response(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[ToolCallPart("submit", {})])

    model = evals.EvalModel(FunctionModel(response), RunUsage(requests=30), True, [])
    with pytest.raises(UsageLimitExceeded):
        await model.request(
            [],
            None,
            ModelRequestParameters(output_tools=[ToolDefinition(name="submit")]),
        )
    assert calls == 0


def test_recording_exhaustion_fails_without_provider() -> None:
    inputs = evals.Inputs(agent="reporter", payload={}, responses=[{}])
    model = evals.replay(inputs, "recorded")
    info = type("Info", (), {"output_tools": [ToolDefinition(name="submit")]})()
    model.function([], info)
    with pytest.raises(ValueError, match="Recording exhausted"):
        model.function([], info)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", sorted(evals.AGENTS))
async def test_namespaced_serving_route_is_priced(agent: str) -> None:
    case = next(case for case in evals.load_cases().cases if case.inputs.agent == agent)
    route = "anthropic/claude-opus-5-5"
    output = await evals.run_case(
        case.inputs, evals.replay(case.inputs, route), evals.CLAUDE
    )
    assert (
        evals.DecisionScore().evaluate(
            SimpleNamespace(output=output, expected_output=case.expected_output)
        )
        == 1
    )
    if agent == "skeptic":
        assert output["reasons"] == case.inputs.responses[-1]["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_hypothesis", [True, False])
async def test_skeptic_hypothesis_reaches_model_request(has_hypothesis: bool) -> None:
    case = next(
        case for case in evals.load_cases().cases if case.inputs.agent == "skeptic"
    )
    inputs = case.inputs.model_copy(deep=True)
    if not has_hypothesis:
        inputs.payload.pop("hypothesis")
    expected = (
        evals.Hypothesis.model_validate(inputs.payload["hypothesis"]).model_dump(
            mode="json"
        )
        if has_hypothesis
        else None
    )
    recorded = evals.replay(inputs, evals.CLAUDE)
    requests = []

    def respond(messages, info):
        payload = next(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        requests.append(json.loads(payload)["hypothesis"])
        return recorded.function(messages, info)

    await evals.run_case(
        inputs, FunctionModel(respond, model_name=evals.CLAUDE), evals.CLAUDE
    )
    assert requests
    assert all(hypothesis == expected for hypothesis in requests)


def test_recorded_submissions_match_s8_finite_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "s8_eval_provider", ROOT / "tests/e2e/provider.py"
    )
    provider = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = provider
    spec.loader.exec_module(provider)
    monkeypatch.setattr(provider, "affected_sellers", lambda: ["S-1"])
    monkeypatch.setattr(
        provider, "investigate_after", datetime.fromisoformat("2026-11-27T12:00:00Z")
    )
    baseline = evals.Dataset[evals.Inputs, dict, dict].from_file(
        evals.CASES / "s8.yaml"
    )
    for case in baseline.cases:
        inputs = case.inputs
        outputs = []
        if inputs.agent in {"detective", "skeptic"}:
            outputs = [inputs.reads["baseline"], inputs.reads["freshness"]]
            if inputs.agent == "detective":
                outputs.extend([{}, {}, {}])
        elif inputs.agent == "selleros":
            scoped = ScopedData(
                evals.RecordedSellerStore(inputs.snapshot),
                inputs.snapshot,
                Selection(seller_ids=["S-1"]),
                datetime.fromisoformat(inputs.payload["window_end_ts"]),
            )
            outputs = [
                scoped.query("price_intent_check", Selection()).model_dump(mode="json")
            ]
        messages = [{"role": "user", "content": json.dumps(inputs.payload)}]
        messages.extend(
            {"role": "tool", "content": json.dumps(output)} for output in outputs
        )
        request = provider.CompletionRequest(
            model={
                "selleros": "openai/gpt-oss-20b",
                "cartographer": "cartographer:free",
            }.get(inputs.agent, inputs.agent),
            messages=messages,
            tools=[
                {"function": {"name": name}}
                for name in ["final_result_Narrative", "final_result_Submission"]
            ],
        )
        captured = provider.scripted_response(request)
        assert (
            json.loads(captured["tool_calls"][0]["function"]["arguments"])
            == inputs.responses[-1]
        )
