import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from factories import make_anomaly
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from agent_runtime import InMemoryKillSwitch, ModelPrice, PriceTable, model_chain
from eye.agents.sentinel import Sentinel, _Candidate
from eye.contracts import TrailStep

TICK = datetime(2026, 11, 27, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


def candidate(ts: datetime = TICK, completeness: float = 1) -> _Candidate:
    anomaly = make_anomaly().model_copy(update={"ts": ts})
    return _Candidate(anomaly, completeness, "ok")


def sentinel(monkeypatch: pytest.MonkeyPatch, function, **kwargs) -> Sentinel:
    model = FunctionModel(function, model_name="scripted")
    monkeypatch.setattr("eye.agents.sentinel.configured_model", lambda *_: model)
    return Sentinel(
        data=Mock(),
        prices=PriceTable(
            prices={"scripted": ModelPrice(input_per_mtok=0, output_per_mtok=0)}
        ),
        **kwargs,
    )


def submit(action: str):
    def respond(messages, info):
        prompt = next(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        rows = json.loads(prompt)["candidates"]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit",
                    {
                        "decisions": [
                            {
                                "anomaly": row["anomaly"],
                                "action": action,
                                "reason": "Measured drop",
                            }
                            for row in rows
                        ]
                    },
                )
            ],
            model_name="scripted",
        )

    return respond


@pytest.mark.asyncio
async def test_done_and_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = sentinel(monkeypatch, submit("investigate"))
    monkeypatch.setattr(agent, "_candidates", lambda ts: [candidate(ts)])
    agent._data.read.return_value.events = []
    steps: list[TrailStep] = []

    async def record(step: TrailStep) -> None:
        steps.append(step)

    decisions = await agent.run(TICK, on_step=record)
    assert decisions[0].action == "investigate"
    assert decisions[0].anomaly == candidate().anomaly
    assert any(step.tool == "submit" for step in steps)
    assert any(step.action == "llm_call" for step in steps)


@pytest.mark.asyncio
async def test_incomplete_forces_defer(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = sentinel(monkeypatch, submit("investigate"))
    monkeypatch.setattr(agent, "_candidates", lambda ts: [candidate(ts, 0.4)])
    agent._data.read.return_value.events = []
    decisions = await agent.run(TICK)
    assert decisions[0].action == "defer"
    assert "40.00%" in decisions[0].reason


@pytest.mark.asyncio
@pytest.mark.parametrize("gap,expected", [(1, "investigate"), (2, "watch")])
async def test_watch_memory(
    monkeypatch: pytest.MonkeyPatch, gap: int, expected: str
) -> None:
    agent = sentinel(monkeypatch, submit("watch"))
    monkeypatch.setattr(agent, "_candidates", lambda ts: [candidate(ts)])
    agent._data.read.return_value.events = []
    first = await agent.run(TICK)
    assert first[0].action == "watch"
    assert (await agent.run(TICK + gap * HOUR))[0].action == expected
    replica = sentinel(monkeypatch, submit("watch"))
    monkeypatch.setattr(replica, "_candidates", lambda ts: [candidate(ts)])
    replica._data.read.return_value.events = []
    assert (await replica.run(TICK + HOUR, previous_decisions=first))[
        0
    ].action == "investigate"


@pytest.mark.asyncio
async def test_quiet_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = sentinel(monkeypatch, lambda *_: pytest.fail("Model called"))
    monkeypatch.setattr(agent, "_candidates", lambda ts: [])
    assert await agent.run(TICK) == []


@pytest.mark.asyncio
async def test_loop_surfaces_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def repeat(*_):
        return ModelResponse(
            parts=[ToolCallPart("get_graph", {})], model_name="scripted"
        )

    agent = sentinel(monkeypatch, repeat)
    monkeypatch.setattr(agent, "_candidates", lambda ts: [candidate(ts)])
    agent._data.read.return_value.events = []
    with pytest.raises(RuntimeError, match="gave_up: loop detected"):
        await agent.run(TICK)


@pytest.mark.asyncio
async def test_kill_surfaces_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    kill = InMemoryKillSwitch()
    kill.kill_system("maintenance")
    agent = sentinel(
        monkeypatch, lambda *_: pytest.fail("Model called"), kill_switch=kill
    )
    monkeypatch.setattr(agent, "_candidates", lambda ts: [candidate(ts)])
    agent._data.read.return_value.events = []
    with pytest.raises(RuntimeError, match="failed: system killed"):
        await agent.run(TICK)


@pytest.mark.asyncio
async def test_provider_error_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def broken(*_):
        calls.append("primary")
        raise ModelAPIError("primary", "offline")

    agent = sentinel(monkeypatch, submit("investigate"))
    fallback = FunctionModel(submit("investigate"), model_name="scripted")
    chain = model_chain(FunctionModel(broken, model_name="primary"), fallback)
    monkeypatch.setattr("eye.agents.sentinel.configured_model", lambda *_: chain)
    monkeypatch.setattr(agent, "_candidates", lambda ts: [candidate(ts)])
    agent._data.read.return_value.events = []
    assert (await agent.run(TICK))[0].action == "investigate"
    assert calls == ["primary"]
