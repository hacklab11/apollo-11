import hashlib
import json
import os
from dataclasses import replace

import httpx
import pytest
from anthropic import AnthropicError
from pydantic import BaseModel, ValidationError

from eye.config import LlmSettings
from eye.contracts import SeriesPoint
from eye.llm.client import (
    LLMClient,
    RunCallLimitExceeded,
    _chat_request,
    _chat_response,
    configure_client,
)
from eye.llm.runtime import AgentRefused, LLMUnavailable, ToolSpec, run_agent

from .test_runtime import Input, discard, spec


def completion(calls=None, *, finish="tool_calls", text=None):
    return {
        "id": "chat-test",
        "model": "test-model",
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": text, "tool_calls": calls or []},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def call(name, arguments, id="t1"):
    return {
        "id": id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def final():
    return completion([call("submit", '{"value":9,"summary":"Total 9"}')])


def test_named_tool_choice_is_preserved():
    request = {
        "model": "test",
        "max_tokens": 10,
        "system": [{"text": "Submit"}],
        "messages": [{"role": "user", "content": "Now"}],
        "tools": [],
        "tool_choice": {"type": "tool", "name": "submit"},
    }
    assert _chat_request(request)["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit"},
    }


@pytest.mark.asyncio
async def test_final_submit_choice_reaches_chat_completions(transport):
    _, requests = transport(
        [completion(text="Still exploring", finish="stop"), final()]
    )
    agent = replace(spec(), max_steps=2, force_submit_on_final_turn=True)
    result = await run_agent(agent, "Calculate", "inv", discard)
    assert result.value == 9
    assert requests[0]["tool_choice"] == "auto"
    assert requests[-1]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit"},
    }


@pytest.mark.asyncio
async def test_compact_seed_context_and_series_on_wire_only(transport):
    lags = [{"lag": i, "corr": i / 100, "n": 336 - i} for i in range(97)]
    numbers = {
        "best_lag": 96,
        "corr": 0.96,
        "n": 240,
        "evidence": {"lags": lags, "excluded_hours": 0},
    }
    context = json.dumps({"seeds": [{"edge": {"lag_hours": 0}, "numbers": numbers}]})
    points = [
        SeriesPoint(ts=f"2026-11-{day:02}T00:00:00Z", value=day) for day in range(1, 29)
    ]

    class Samples(BaseModel):
        summary: str = "28 points"
        points: list[SeriesPoint]

    _, requests = transport(
        [completion([call("query_metric", '{"value":0}')]), final()]
    )
    trail = []

    async def collect(step):
        trail.append(step)

    agent = replace(
        spec(),
        tools=[
            ToolSpec("query_metric", "Query", Input, lambda _: Samples(points=points))
        ],
    )
    await run_agent(agent, context, "inv", collect)
    sent, notice = requests[0]["messages"][1]["content"].split("\n\n", 1)
    compact = json.loads(sent)["seeds"][0]["numbers"]
    assert compact["best_lag"] == numbers["best_lag"]
    assert compact["evidence"] == {
        "excluded_hours": 0,
        "tested_lags": 97,
        "strongest_at_proposed_lag_plus_minus_6h": lags[6],
    }
    assert notice.startswith("Runtime budget:")
    result = json.loads(requests[1]["messages"][-2]["content"])
    restored = [
        dict(zip(result["point_fields"], row, strict=True)) for row in result["points"]
    ]
    saved = next(step.output for step in trail if step.tool == "query_metric")
    assert restored == saved["points"]
    assert len(requests[1]["messages"][-2]["content"]) < len(
        Samples(points=points).model_dump_json()
    )


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-placeholder")

    def make(script, **settings):
        requests = []

        async def handle(request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer offline-placeholder"
            requests.append(json.loads(request.content))
            item = script.pop(0)
            if isinstance(item, httpx.Response):
                return item
            if isinstance(item, Exception):
                raise item
            if isinstance(item, int):
                return httpx.Response(item)
            return httpx.Response(200, json=item)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        client = LLMClient(
            LlmSettings(provider="openai_compatible", **settings), http_client=http
        )
        monkeypatch.setattr("eye.llm.client._client", client)
        return client, requests

    return make


@pytest.mark.asyncio
async def test_round_trip(transport):
    client, requests = transport(
        [
            completion(
                [call("increment", '{"value":2}'), call("double", '{"value":3}', "t2")]
            ),
            final(),
        ]
    )
    result = await run_agent(spec(), "Calculate", "inv", discard)
    assert result.value == 9
    first, second = requests
    assert set(first) == {"model", "max_tokens", "messages", "tools", "tool_choice"}
    assert first["tool_choice"] == "auto"
    assert first["messages"][0]["role"] == "system"
    assert first["max_tokens"] == spec().max_tokens
    assert [t["function"]["name"] for t in first["tools"]] == [
        "double",
        "increment",
        "submit",
    ]
    assert all(
        set(t["function"]) == {"name", "description", "parameters"}
        for t in first["tools"]
    )
    assistant = second["messages"][-4]
    assert assistant["role"] == "assistant"
    assert len(assistant["tool_calls"]) == 2
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
        "value": 2
    }
    results = second["messages"][-3:-1]
    assert [r["role"] for r in results] == ["tool", "tool"]
    assert [r["tool_call_id"] for r in results] == ["t1", "t2"]
    assert [json.loads(r["content"])["value"] for r in results] == [3, 6]
    assert "4 LLM turn(s) remaining" in second["messages"][-1]["content"]
    assert client.status()["tokens_in"] == 20
    assert client.status()["tokens_out"] == 10


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("tool_calls", "tool_use"),
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("content_filter", "refusal"),
    ],
)
def test_finish_reason(finish, expected):
    result = _chat_response(completion(finish=finish, text="hello"), "fallback")
    assert result.stop_reason == expected
    assert result.content[0].text == "hello"


@pytest.mark.asyncio
async def test_explicit_refusal_with_stop(transport):
    refused = completion(finish="stop")
    refused["choices"][0]["message"]["refusal"] = "Cannot comply"
    transport([refused])
    with pytest.raises(AgentRefused):
        await run_agent(spec(), "go", "inv", discard)


def test_thinking_dropped():
    result = _chat_request(
        {
            "model": "m",
            "max_tokens": 10,
            "system": [{"text": "system"}],
            "tools": [],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": "visible"},
                    ],
                }
            ],
        }
    )
    assert result["messages"][-1] == {"role": "assistant", "content": "visible"}
    assert "private" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["{broken", "[]", "null"])
async def test_invalid_json_returns_tool_error(transport, arguments):
    _, requests = transport([completion([call("submit", arguments)]), final()])
    assert (await run_agent(spec(), "go", "inv", discard)).value == 9
    error = requests[1]["messages"][-2]
    assert error["role"] == "tool"
    assert "ValidationError" in error["content"]
    assert "Field required" in error["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 503, httpx.ConnectError("offline")])
async def test_retries_and_unavailable(transport, failure):
    _, requests = transport([failure] * 3)
    steps = []

    async def collect(step):
        steps.append(step)

    with pytest.raises(LLMUnavailable):
        await run_agent(spec(), "go", "inv", collect)
    assert len(requests) == 3
    assert steps[-1].action == "note"


@pytest.mark.asyncio
async def test_retry_success(transport):
    _, requests = transport([429, 500, final()])
    assert (await run_agent(spec(), "go", "inv", discard)).value == 9
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,delay",
    [
        ({"Retry-After": "7"}, 7),
        ({"Retry-After": "86400"}, 60),
        ({"X-RateLimit-Reset": "1700000009"}, 9),
        ({"X-RateLimit-Reset": "1700000009000"}, 9),
        ({"Retry-After": "Tue, 14 Nov 2023 22:13:29 GMT"}, 9),
        ({"Retry-After": "bad"}, 0.1),
    ],
)
async def test_retry_honors_bounded_provider_delay(
    transport, monkeypatch, headers, delay
):
    monkeypatch.setattr("eye.llm.client.time.time", lambda: 1700000000)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("eye.llm.client.asyncio.sleep", sleep)
    transport(
        [
            httpx.Response(
                429,
                headers=headers,
                json={"error": {"message": "Free model quota exceeded"}},
            ),
            final(),
        ]
    )
    assert (await run_agent(spec(), "go", "inv", discard)).value == 9
    assert sleeps == [delay]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (429, {"error": {"message": "Free model quota exceeded"}}),
        (200, {"error": {"code": 429, "message": "Free model quota exceeded"}}),
    ],
)
async def test_rate_limit_diagnostic_and_cooldown(transport, monkeypatch, status, body):
    clock = [100.0]
    monkeypatch.setattr("eye.llm.client.time.monotonic", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr("eye.llm.client.asyncio.sleep", sleep)
    _, requests = transport(
        [httpx.Response(status, json=body) for _ in range(3)] + [final()]
    )
    steps = []

    async def collect(step):
        steps.append(step)

    with pytest.raises(LLMUnavailable, match="Free model quota exceeded"):
        await run_agent(spec(), "go", "inv", collect)
    assert len(requests) == 3
    assert "429" in steps[-1].output_summary
    with pytest.raises(LLMUnavailable, match="cooldown"):
        await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 3
    clock[0] += 61
    assert (await run_agent(spec(), "go", "inv", discard)).value == 9


@pytest.mark.asyncio
async def test_cooldown_rejections_do_not_consume_hourly_budget(transport, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("eye.llm.client.time.monotonic", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr("eye.llm.client.asyncio.sleep", sleep)
    client, requests = transport([429, 429, 429, final()], max_calls_per_hour=50)
    client._calls.extend([clock[0]] * 48)
    with pytest.raises(LLMUnavailable):
        await run_agent(spec(), "go", "inv", discard)
    for _ in range(4):
        with pytest.raises(LLMUnavailable, match="cooldown"):
            await run_agent(spec(), "go", "inv", discard)
        assert client.status()["calls_per_hour"] == 49
        assert not client.status()["budget_paused"]
        assert len(requests) == 3
    clock[0] += 61
    assert (await run_agent(spec(), "go", "inv", discard)).value == 9
    assert len(requests) == 4
    assert client.status()["calls_per_hour"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_daily_rate_limit_stops_the_process(transport, monkeypatch, status):
    clock = [100.0]
    monkeypatch.setattr("eye.llm.client.time.monotonic", lambda: clock[0])
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("eye.llm.client.asyncio.sleep", sleep)
    response = httpx.Response(
        status,
        headers={"Retry-After": "86400"},
        json={
            "error": {
                "code": 429,
                "message": "Provider returned an error",
                "metadata": {"raw": "Rate limit exceeded: free-models-per-day."},
            }
        },
    )
    _, requests = transport([response] * 4)
    for _ in range(2):
        with pytest.raises(LLMUnavailable, match="Daily model request quota"):
            await run_agent(spec(), "go", "inv", discard)
        clock[0] += 3601
    assert len(requests) == 1
    assert not sleeps


@pytest.mark.asyncio
async def test_per_run_cap_includes_failed_requests_and_retries(transport, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("eye.llm.client.time.monotonic", lambda: clock[0])

    async def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr("eye.llm.client.asyncio.sleep", sleep)
    client, requests = transport([500, 503, final()], max_calls_per_run=2)
    for _ in range(2):
        with pytest.raises(RunCallLimitExceeded, match="2 provider requests"):
            await run_agent(spec(), "go", "inv", discard)
        clock[0] += 3601
    assert len(requests) == 2
    assert client.status()["budget_paused"]
    assert client.status()["calls_per_hour"] == 0


@pytest.mark.asyncio
async def test_provider_error_envelope_retries_and_redacts(transport, monkeypatch):
    async def sleep(seconds):
        pass

    monkeypatch.setattr("eye.llm.client.asyncio.sleep", sleep)
    error = {
        "error": {
            "code": 502,
            "message": "Provider returned error offline-placeholder",
            "metadata": {"raw": '{"error":{"message":"Upstream overloaded"}}'},
        }
    }
    _, requests = transport([error] * 3)
    with pytest.raises(LLMUnavailable) as caught:
        await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 3
    assert "HTTP 200" in str(caught.value)
    assert "502" in str(caught.value)
    assert "Upstream overloaded" in str(caught.value)
    assert "offline-placeholder" not in str(caught.value)


@pytest.mark.asyncio
async def test_provider_failure_matches_narrative_error_handler(transport):
    transport([401])
    steps = []

    async def collect(step):
        steps.append(step)

    with pytest.raises(AnthropicError) as error:
        await run_agent(spec(), "go", "inv", collect)
    assert isinstance(error.value, LLMUnavailable)
    assert str(error.value) == "Chat Completions HTTP 401"
    assert steps[-1].action == "note"
    assert (
        steps[-1].output_summary
        == "LLM unavailable after provider retries: Chat Completions HTTP 401"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [401, {"choices": []}])
async def test_nonretryable_errors(transport, bad):
    _, requests = transport([bad])
    with pytest.raises(LLMUnavailable):
        await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("message", None),
        ("message", []),
        ("message", "bad"),
        ("usage", "bad"),
        ("tool_calls", [None]),
        ("function", None),
    ],
)
async def test_malformed_success_response(transport, field, value):
    raw = final()
    message = raw["choices"][0]["message"]
    if field == "message":
        raw["choices"][0][field] = value
    elif field == "usage":
        raw[field] = value
    elif field == "function":
        message["tool_calls"][0][field] = value
    else:
        message[field] = value
    _, requests = transport([raw])
    with pytest.raises(LLMUnavailable, match="^Invalid Chat Completions response$"):
        await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_missing_key(transport, monkeypatch):
    _, requests = transport([])
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(
        LLMUnavailable, match="Missing API key environment variable: OPENROUTER_API_KEY"
    ):
        await run_agent(spec(), "go", "inv", discard)
    assert not requests


@pytest.mark.asyncio
async def test_key_read_at_call_time(transport, monkeypatch):
    _, requests = transport([final()])
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-placeholder")
    await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_forced_submit_does_not_replay_auto_response(transport, tmp_path):
    path = tmp_path / "cache.jsonl"
    request = {
        "model": "test-model",
        "max_tokens": 10,
        "system": [{"type": "text", "text": "Submit when done"}],
        "messages": [{"role": "user", "content": "Investigate"}],
        "tools": [
            {
                "name": "submit",
                "description": "Final answer",
                "input_schema": spec().submit_model.model_json_schema(),
            }
        ],
        "tool_choice": {"type": "auto"},
    }
    client, _ = transport(
        [completion(text="Still exploring", finish="stop")],
        mode="record",
        cache_path=path,
    )
    exploratory = await client.create(request)
    legacy_identity = {
        "model": request["model"],
        "system": "Submit when done",
        "messages": request["messages"],
        "tools": ["submit"],
        "provider": client.settings.provider,
        "base_url": client.settings.base_url,
    }
    legacy_digest = json.dumps(legacy_identity, sort_keys=True, separators=(",", ":"))
    assert (
        json.loads(path.read_text())["key"]
        == hashlib.sha256(legacy_digest.encode()).hexdigest()
    )

    client, requests = transport([final()], mode="replay", cache_path=path)
    assert await client.create(request) == exploratory
    assert not requests
    forced = await client.create(
        {**request, "tool_choice": {"type": "tool", "name": "submit"}}
    )
    assert [block.type for block in forced.content] == ["tool_use"]
    assert len(requests) == 1
    assert requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit"},
    }


@pytest.mark.asyncio
async def test_record_replay(transport, tmp_path, monkeypatch):
    path = tmp_path / "cache.jsonl"
    _, requests = transport(
        [completion([call("double", '{"value":3}')]), final()],
        mode="record",
        cache_path=path,
    )
    expected = await run_agent(spec(), "go", "inv", discard)
    assert len(requests) == 2
    assert "offline-placeholder" not in path.read_text()
    client, replayed = transport([], mode="replay", cache_path=path)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert await run_agent(spec(), "go", "other-inv", discard) == expected
    assert not replayed
    assert client.status()["calls_per_hour"] == 0


@pytest.mark.parametrize(
    "values",
    [
        {"provider": "unknown"},
        {"base_url": "not-url"},
        {"base_url": "ftp://example.com"},
        {"api_key_env": ""},
        {"api_key_env": "HAS SPACE"},
        {"api_key_env": 1},
    ],
)
def test_provider_settings_invalid(values):
    with pytest.raises(ValidationError):
        LlmSettings(**values)


def test_provider_settings_defaults():
    settings = LlmSettings()
    assert settings.provider == "anthropic"
    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.api_key_env == "OPENROUTER_API_KEY"


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"), reason="OPENROUTER_API_KEY unset"
)
async def test_live_openai_round_trip():
    model = os.environ.get(
        "EYE_LIVE_OPENAI_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"
    )
    async with httpx.AsyncClient(timeout=120.0) as http:
        configure_client(
            LLMClient(LlmSettings(provider="openai_compatible"), http_client=http)
        )
        answer = await run_agent(
            replace(spec(), model=model),
            "Run both arithmetic tools and submit their sum.",
            "live-openai",
            discard,
        )
    assert answer.value == 9
