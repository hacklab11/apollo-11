import json
import os
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from anthropic import AsyncAnthropic
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from eye.config import LlmSettings
from eye.llm.client import (
    LLMClient,
    RunCallLimitExceeded,
    budget_status,
    configure_client,
)
from eye.llm.runtime import (
    AgentRefused,
    AgentSpec,
    BudgetExceeded,
    LLMUnavailable,
    ToolSpec,
    run_agent,
)


class Input(BaseModel):
    value: int


class Output(BaseModel):
    value: int
    summary: str


class MappingOutput(BaseModel):
    numbers: dict[str, float]
    summary: str


def increment(args: Input) -> Output:
    return Output(value=args.value + 1, summary=f"Incremented {args.value} by 1")


async def double(args: Input) -> Output:
    return Output(value=args.value * 2, summary=f"Doubled {args.value} by 2")


def spec() -> AgentSpec:
    return AgentSpec(
        name="chat",
        system_prompt_file=str(Path(__file__).with_name("agent.md")),
        tools=[
            ToolSpec("increment", "Add one.", Input, increment),
            ToolSpec("double", "Multiply by two.", Input, double),
        ],
        submit_model=Output,
        effort="low",
        model="claude-opus-5-5",
        max_steps=5,
    )


def tool(name: str, value: object, id: str = "t1") -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": value}


def response(content: list, stop: str = "tool_use") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def final() -> dict:
    return response([tool("submit", {"value": 9, "summary": "Total 9"})])


@pytest.fixture
def harness(monkeypatch):
    clients = []

    def make(script: list, **settings):
        requests = []

        async def handle(request):
            requests.append(json.loads(request.content))
            assert (
                request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
            )
            item = script.pop(0)
            if isinstance(item, int):
                return httpx2.Response(
                    item,
                    json={
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Unavailable"},
                    },
                )
            return httpx2.Response(200, json=item)

        sdk = AsyncAnthropic(
            api_key="offline-placeholder",
            max_retries=2,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )
        clients.append(sdk)
        client = LLMClient(LlmSettings(**settings), sdk)
        monkeypatch.setattr("eye.llm.client._client", client)
        return client, requests

    return make


@pytest.mark.asyncio
async def test_anthropic_final_turn_keeps_auto_tool_choice(harness):
    _, requests = harness([response([tool("increment", {"value": 2})]), final()])
    agent = replace(spec(), max_steps=2, force_submit_on_final_turn=True)
    result = await run_agent(agent, "Calculate", "inv", discard)
    assert result.value == 9
    assert len(requests) == 2
    assert all(request["tool_choice"] == {"type": "auto"} for request in requests)
    assert "final allowed turn" in requests[-1]["messages"][-1]["content"][-1]["text"]


@pytest.mark.asyncio
async def test_per_run_cap_includes_anthropic_sdk_retries(harness):
    client, requests = harness([500, final()], max_calls_per_run=1)
    with pytest.raises(RunCallLimitExceeded, match="1 provider requests"):
        await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 1
    assert client.status()["budget_paused"]


@pytest.mark.asyncio
async def test_parallel_tools_trail_and_request(harness):
    client, requests = harness(
        [
            response(
                [
                    {"type": "text", "text": "Compute both"},
                    tool("increment", {"value": 2}),
                    tool("double", {"value": 3}, "t2"),
                ]
            ),
            final(),
        ]
    )
    steps = []

    async def collect(step):
        steps.append(step)

    result = await run_agent(spec(), "Calculate", "inv", collect)
    assert result.value == 9
    assert len(requests) == 2
    first = requests[0]
    assert first["tool_choice"] == {"type": "auto"}
    assert first["output_config"] == {"effort": "low"}
    assert first["fallbacks"] == "default"
    assert "thinking" not in first
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == ["double", "increment", "submit"]
    assert all(
        t["strict"] and not t["input_schema"]["additionalProperties"]
        for t in first["tools"]
    )
    assert first["tools"] == requests[1]["tools"]
    assert first["system"] == requests[1]["system"]
    assert len(requests[1]["messages"][-1]["content"]) == 3
    calls = [s for s in steps if s.action == "tool_call"]
    assert [s.tool for s in calls] == ["increment", "double", "submit"]
    assert all(s.output_summary for s in calls)
    assert calls[0].reason == "Compute both"
    assert [s.n for s in steps] == list(range(1, len(steps) + 1))
    assert client.status() == {
        "calls_per_hour": 2,
        "tokens_in": 20,
        "tokens_out": 10,
        "budget_paused": False,
    }
    assert budget_status() == client.status()


async def discard(step):
    pass


@pytest.mark.asyncio
async def test_strict_tool_schema_keeps_numeric_bounds_in_local_validation(harness):
    class BoundedInput(Input):
        value: int | None = Field(default=None, ge=0)

    agent = spec()
    agent.tools[0].input_model = BoundedInput
    _, requests = harness([response([tool("increment", {"value": -1})]), final()])

    result = await run_agent(agent, "go", "inv", discard)

    assert result.value == 9
    definition = next(t for t in requests[0]["tools"] if t["name"] == "increment")
    assert definition["strict"] is True
    value = definition["input_schema"]["properties"]["value"]
    integer = next(option for option in value["anyOf"] if option["type"] == "integer")
    assert "minimum" not in integer
    error = requests[1]["messages"][-1]["content"][0]
    assert error["is_error"]
    assert "Input should be greater than or equal to 0" in error["content"]


@pytest.mark.asyncio
async def test_mapping_schema_and_submit(harness):
    payload = {"numbers": {"sales": 12.5}, "summary": "Sales 12.5"}
    _, requests = harness([response([tool("submit", payload)])])
    result = await run_agent(
        replace(spec(), submit_model=MappingOutput), "go", "inv", discard
    )
    assert result.numbers == {"sales": 12.5}
    definition = next(t for t in requests[0]["tools"] if t["name"] == "submit")
    assert definition["strict"] is False
    assert all(
        t["strict"] is True for t in requests[0]["tools"] if t["name"] != "submit"
    )
    schema = definition["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["numbers"]["additionalProperties"] == {"type": "number"}
    Draft202012Validator(schema).validate(payload)
    with pytest.raises(Exception, match="not of type 'number'"):
        Draft202012Validator(schema).validate(
            {"numbers": {"sales": "invalid"}, "summary": "Invalid"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exception", "invalid", "unknown", "submit"])
async def test_failed_tools_get_second_chance(harness, kind):
    name, args = {
        "exception": ("increment", {"value": 1}),
        "invalid": ("double", {"value": "bad"}),
        "unknown": ("missing", {}),
        "submit": ("submit", {}),
    }[kind]
    _, requests = harness([response([tool(name, args)]), final()])
    agent = spec()
    if kind == "exception":

        def broken(args):
            raise RuntimeError("upstream calculation unavailable")

        agent.tools[0].fn = broken
    assert (await run_agent(agent, "go", "inv", discard)).value == 9
    block = requests[1]["messages"][-1]["content"][0]
    assert block["is_error"]
    if kind == "submit":
        assert "value: Field required" in block["content"]
        assert "summary: Field required" in block["content"]
    elif kind == "exception":
        assert "upstream calculation unavailable" in block["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["end_turn", "max_tokens"])
async def test_empty_turns_and_budget(harness, stop):
    _, requests = harness([response([], stop), response([], stop)])
    with pytest.raises(BudgetExceeded):
        await run_agent(replace(spec(), max_steps=2), "go", "inv", discard)
    assert len(requests) == 2
    assert requests[1]["messages"][-1]["role"] == "user"
    assert "final allowed turn" in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_steps", [1, 2])
async def test_final_turn_notice_with_tool_results(harness, max_steps):
    script = [final()]
    if max_steps == 2:
        script.insert(0, response([tool("double", {"value": 3})]))
    _, requests = harness(script)
    result = await run_agent(replace(spec(), max_steps=max_steps), "go", "inv", discard)
    assert result.value == 9
    content = requests[-1]["messages"][-1]["content"]
    if max_steps == 2:
        assert content[0]["type"] == "tool_result"
        assert content[-1]["type"] == "text"
        content = content[-1]["text"]
        assert "2 LLM turn(s) remaining" in requests[0]["messages"][0]["content"]
    assert "1 LLM turn(s) remaining" in content
    assert "call submit now" in content
    assert "Request independent tool calls in one turn" in content


@pytest.mark.asyncio
async def test_refusal(harness):
    harness([response([], "refusal")])
    steps = []

    async def collect(step):
        steps.append(step)

    with pytest.raises(AgentRefused):
        await run_agent(spec(), "go", "inv", collect)
    assert steps[-1].action == "note"


@pytest.mark.asyncio
async def test_retries_and_unavailability(harness):
    _, requests = harness([529, 529, 529])
    steps = []

    async def collect(step):
        steps.append(step)

    with pytest.raises(LLMUnavailable):
        await run_agent(spec(), "go", "inv", collect)
    assert len(requests) == 3
    assert steps[-1].action == "note"


@pytest.mark.asyncio
async def test_record_replay_and_miss(harness, tmp_path, capsys):
    path = tmp_path / "cache.jsonl"
    _, recorded = harness(
        [response([tool("double", {"value": 3})]), final()],
        mode="record",
        cache_path=path,
    )
    expected = await run_agent(spec(), "go", "inv", discard)
    assert len(recorded) == 2
    assert all(
        set(json.loads(line)) == {"key", "request_digest", "response_json"}
        for line in path.read_text().splitlines()
    )
    _, replayed = harness([final()], mode="replay", cache_path=path)
    assert await run_agent(spec(), "go", "another-id", discard) == expected
    assert replayed == []
    await run_agent(spec(), "different", "inv", discard)
    assert len(replayed) == 1
    assert json.loads(capsys.readouterr().out)["event"] == "replay_miss"


@pytest.mark.asyncio
async def test_hourly_budget(harness, monkeypatch):
    client, requests = harness([final(), final()])
    client.settings.max_calls_per_hour = 1
    await run_agent(spec(), "go", "inv", discard)
    with pytest.raises(BudgetExceeded):
        await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 1
    assert client.status()["budget_paused"]
    client._calls[0] -= 3601
    assert not client.status()["budget_paused"]
    await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 2


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY unset"
)
async def test_live_two_tool_round_trip():
    async with AsyncAnthropic(max_retries=2) as sdk:
        configure_client(LLMClient(LlmSettings(), sdk))
        steps = []

        async def collect(step):
            steps.append(step)

        answer = await run_agent(
            spec(),
            "Run both arithmetic tools, then submit their sum.",
            "live-test",
            collect,
        )
        assert answer.value == 9
        assert {s.tool for s in steps if s.action == "tool_call"} >= {
            "increment",
            "double",
            "submit",
        }


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY unset"
)
async def test_live_mapping_submit():
    async with AsyncAnthropic(max_retries=2) as sdk:
        configure_client(LLMClient(LlmSettings(), sdk))
        answer = await run_agent(
            replace(spec(), submit_model=MappingOutput),
            "Run both arithmetic tools. Call submit with numbers mapping increment to 3 "
            "and double to 6, plus a short summary. Do not use a value field.",
            "live-mapping-test",
            discard,
        )
        assert answer.numbers == {"increment": 3.0, "double": 6.0}
