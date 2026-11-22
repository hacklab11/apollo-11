import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import check_allow_model_requests
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolResults

from agent_runtime import (
    IDEMPOTENT_METADATA_KEY,
    Budget,
    GaveUp,
    InMemoryIdempotencyStore,
    InMemoryKillSwitch,
    NeedsInput,
    PriceTable,
    RunLimits,
    RunScope,
    end_state_outputs,
    model_chain,
    run_agent,
)

from .conftest import Diagnosis, call, finish, scripted

LIMITS = RunLimits(
    max_steps=10, max_tokens=100_000, max_cost_usd=1.0, max_wall_clock_s=5
)


def agent_with(model: FunctionModel | object) -> Agent[None, object]:
    agent: Agent[None, object] = Agent(model, output_type=end_state_outputs(Diagnosis))

    @agent.tool_plain
    def lookup(metric: str) -> str:
        return f"{metric}: -12%"

    return agent


async def run(
    agent: Agent[None, object], scope: RunScope, prices: PriceTable, **kwargs: object
):
    return await run_agent(
        agent,
        "why did GMV drop?",
        scope=scope,
        limits=kwargs.pop("limits", LIMITS),
        prices=prices,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_done_with_validated_output_and_usage(
    scope: RunScope, prices: PriceTable
) -> None:
    agent = agent_with(
        scripted(
            call("lookup", metric="gmv"), finish(Diagnosis, cause="payment outage")
        )
    )
    outcome = await run(agent, scope, prices)
    assert outcome.result.status == "done"
    assert outcome.result.result == Diagnosis(cause="payment outage")
    assert outcome.result.usage.requests == 2
    assert outcome.result.usage.tool_calls == 1
    assert outcome.result.usage.cost_usd > 0
    assert outcome.messages


@pytest.mark.asyncio
async def test_needs_input_end_state(scope: RunScope, prices: PriceTable) -> None:
    agent = agent_with(scripted(finish(NeedsInput, question="Which region?")))
    outcome = await run(agent, scope, prices)
    assert (outcome.result.status, outcome.result.question) == (
        "needs_input",
        "Which region?",
    )
    assert outcome.result.a2a_state == "input-required"


@pytest.mark.asyncio
async def test_agent_can_give_up_explicitly(
    scope: RunScope, prices: PriceTable
) -> None:
    agent = agent_with(scripted(finish(GaveUp, reason="no data for that day")))
    outcome = await run(agent, scope, prices)
    assert (outcome.result.status, outcome.result.reason) == (
        "gave_up",
        "no data for that day",
    )


@pytest.mark.asyncio
async def test_step_cap_gives_up(scope: RunScope, prices: PriceTable) -> None:
    agent = agent_with(
        scripted(
            call("lookup", metric="a"),
            call("lookup", metric="b"),
            call("lookup", metric="c"),
            call("lookup", metric="d"),
        )
    )
    outcome = await run(
        agent, scope, prices, limits=LIMITS.model_copy(update={"max_steps": 3})
    )
    assert outcome.result.status == "gave_up"
    assert "request_limit" in outcome.result.reason
    assert outcome.result.usage.requests == 3
    assert outcome.result.usage.tool_calls == 3
    assert outcome.result.usage.total_tokens > 0


@pytest.mark.asyncio
async def test_token_cap_gives_up(scope: RunScope, prices: PriceTable) -> None:
    agent = agent_with(
        scripted(
            call("lookup", metric="a"),
            call("lookup", metric="b"),
            finish(Diagnosis, cause="x"),
        )
    )
    outcome = await run(
        agent, scope, prices, limits=LIMITS.model_copy(update={"max_tokens": 60})
    )
    assert outcome.result.status == "gave_up"
    assert "total_tokens_limit" in outcome.result.reason


@pytest.mark.asyncio
async def test_cost_cap_gives_up(scope: RunScope, prices: PriceTable) -> None:
    agent = agent_with(
        scripted(
            call("lookup", metric="a"),
            call("lookup", metric="b"),
            finish(Diagnosis, cause="x"),
        )
    )
    outcome = await run(
        agent, scope, prices, limits=LIMITS.model_copy(update={"max_cost_usd": 0.0003})
    )
    assert outcome.result.status == "gave_up"
    assert "cost" in outcome.result.reason
    assert outcome.result.usage.cost_usd > 0.0003


@pytest.mark.asyncio
async def test_wall_clock_cap_gives_up(scope: RunScope, prices: PriceTable) -> None:
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(1)
        raise AssertionError("should have timed out")

    agent = agent_with(FunctionModel(slow, model_name="scripted"))
    outcome = await run(
        agent,
        scope,
        prices,
        limits=LIMITS.model_copy(update={"max_wall_clock_s": 0.05}),
    )
    assert outcome.result.status == "gave_up"
    assert "wall-clock" in outcome.result.reason


@pytest.mark.asyncio
async def test_loop_detection_gives_up_on_third_identical_call(
    scope: RunScope, prices: PriceTable
) -> None:
    agent = agent_with(scripted(call("lookup", metric="gmv")))
    outcome = await run(agent, scope, prices)
    assert outcome.result.status == "gave_up"
    assert "loop detected: lookup called 3 times" in outcome.result.reason


@pytest.mark.asyncio
async def test_varied_calls_are_not_a_loop(scope: RunScope, prices: PriceTable) -> None:
    agent = agent_with(
        scripted(
            call("lookup", metric="gmv"),
            call("lookup", metric="gmv"),
            call("lookup", metric="orders"),
            call("lookup", metric="gmv"),
            finish(Diagnosis, cause="x"),
        )
    )
    assert (await run(agent, scope, prices)).result.status == "done"


@pytest.mark.parametrize("kill", ["system", "tenant", "run"])
@pytest.mark.asyncio
async def test_kill_switch_before_start(
    kill: str, scope: RunScope, prices: PriceTable
) -> None:
    switch = InMemoryKillSwitch()
    {
        "system": lambda: switch.kill_system("halt"),
        "tenant": lambda: switch.kill_tenant("tenant-a", "halt"),
        "run": lambda: switch.kill_run("run-1", "halt"),
    }[kill]()
    agent = agent_with(scripted(finish(Diagnosis, cause="x")))
    outcome = await run(agent, scope, prices, kill_switch=switch)
    assert outcome.result.status == "failed"
    assert outcome.result.reason.startswith(f"{kill} ")
    assert outcome.result.usage.requests == 0


@pytest.mark.asyncio
async def test_kill_switch_mid_run_stops_before_next_tool(
    scope: RunScope, prices: PriceTable
) -> None:
    switch = InMemoryKillSwitch()
    executed: list[str] = []
    agent: Agent[None, object] = Agent(
        scripted(call("first"), call("second"), finish(Diagnosis, cause="x")),
        output_type=end_state_outputs(Diagnosis),
    )

    @agent.tool_plain
    def first() -> str:
        executed.append("first")
        switch.kill_tenant("tenant-a", "operator")
        return "ok"

    @agent.tool_plain
    def second() -> str:
        executed.append("second")
        return "ok"

    outcome = await run(agent, scope, prices, kill_switch=switch)
    assert outcome.result.status == "failed"
    assert executed == ["first"]


@pytest.mark.asyncio
async def test_idempotent_tool_runs_once_across_retried_run(
    scope: RunScope, prices: PriceTable
) -> None:
    store = InMemoryIdempotencyStore()
    sent: list[str] = []

    def build() -> Agent[None, object]:
        agent: Agent[None, object] = Agent(
            scripted(call("notify", text="incident"), finish(Diagnosis, cause="x")),
            output_type=end_state_outputs(Diagnosis),
        )

        @agent.tool_plain(metadata={IDEMPOTENT_METADATA_KEY: True})
        def notify(text: str) -> str:
            sent.append(text)
            return f"sent #{len(sent)}"

        return agent

    first = await run(build(), scope, prices, idempotency=store)
    retried = await run(build(), scope, prices, idempotency=store)
    other_run = await run(
        build(), scope.model_copy(update={"run_id": "run-2"}), prices, idempotency=store
    )
    assert {first.result.status, retried.result.status, other_run.result.status} == {
        "done"
    }
    assert sent == ["incident", "incident"]


@pytest.mark.asyncio
async def test_idempotent_tool_without_store_fails(
    scope: RunScope, prices: PriceTable
) -> None:
    agent: Agent[None, object] = Agent(
        scripted(call("notify"), finish(Diagnosis, cause="x")),
        output_type=end_state_outputs(Diagnosis),
    )

    @agent.tool_plain(metadata={IDEMPOTENT_METADATA_KEY: True})
    def notify() -> str:
        return "sent"

    with pytest.raises(RuntimeError, match="no idempotency store"):
        await run(agent, scope, prices)


@pytest.mark.asyncio
async def test_approval_required_tool_pauses_then_resumes(
    scope: RunScope, prices: PriceTable
) -> None:
    closed: list[str] = []
    agent: Agent[None, object] = Agent(
        scripted(
            call("close_incident", incident_id="INC-1"),
            finish(Diagnosis, cause="closed"),
        ),
        output_type=end_state_outputs(Diagnosis),
    )

    @agent.tool_plain(requires_approval=True)
    def close_incident(incident_id: str) -> str:
        closed.append(incident_id)
        return "closed"

    paused = await run(agent, scope, prices)
    assert paused.result.status == "needs_input"
    assert [p.tool_name for p in paused.result.pending_approvals] == ["close_incident"]
    assert paused.result.pending_approvals[0].args == {"incident_id": "INC-1"}
    assert closed == []

    approval = DeferredToolResults(
        approvals={paused.result.pending_approvals[0].tool_call_id: True}
    )
    resumed = await run_agent(
        agent,
        None,
        scope=scope,
        limits=LIMITS,
        prices=prices,
        message_history=paused.messages,
        deferred_tool_results=approval,
    )
    assert resumed.result.status == "done"
    assert closed == ["INC-1"]


@pytest.mark.asyncio
async def test_fallback_model_takes_over_on_provider_error(
    scope: RunScope, prices: PriceTable
) -> None:
    def down(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelAPIError("primary", "503 overloaded")

    agent = agent_with(
        model_chain(
            FunctionModel(down, model_name="primary"),
            scripted(finish(Diagnosis, cause="via backup")),
        )
    )
    outcome = await run(agent, scope, prices)
    assert outcome.result.status == "done"
    assert outcome.result.result == Diagnosis(cause="via backup")


@pytest.mark.asyncio
async def test_all_models_down_fails(scope: RunScope, prices: PriceTable) -> None:
    def down(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelAPIError("m", "503")

    agent = agent_with(
        model_chain(
            FunctionModel(down, model_name="a"), FunctionModel(down, model_name="b")
        )
    )
    outcome = await run(agent, scope, prices)
    assert outcome.result.status == "failed"


@pytest.mark.asyncio
async def test_unpriced_model_fails_instead_of_running_uncapped(
    scope: RunScope, prices: PriceTable
) -> None:
    agent = agent_with(scripted(finish(Diagnosis, cause="x"), model_name="mystery"))
    outcome = await run(agent, scope, prices)
    assert outcome.result.status == "failed"
    assert "no pinned price for model 'mystery'" in outcome.result.reason


@pytest.mark.asyncio
async def test_exhausted_budget_gives_up_without_model_call(
    scope: RunScope, prices: PriceTable
) -> None:
    spent = Budget(
        tokens=0, cost_usd=1.0, deadline=datetime.now(UTC) + timedelta(minutes=1)
    )
    agent = agent_with(scripted(finish(Diagnosis, cause="x")))
    outcome = await run(agent, scope, prices, budget=spent)
    assert outcome.result.status == "gave_up"
    assert outcome.result.usage.requests == 0


@pytest.mark.asyncio
async def test_budget_tightens_run_limits(scope: RunScope, prices: PriceTable) -> None:
    small = Budget(
        tokens=60, cost_usd=1.0, deadline=datetime.now(UTC) + timedelta(minutes=1)
    )
    agent = agent_with(
        scripted(
            call("lookup", metric="a"),
            call("lookup", metric="b"),
            finish(Diagnosis, cause="x"),
        )
    )
    outcome = await run(agent, scope, prices, budget=small)
    assert outcome.result.status == "gave_up"
    assert "total_tokens_limit" in outcome.result.reason


def test_real_model_requests_are_blocked_in_tests() -> None:
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
        check_allow_model_requests()


@pytest.mark.asyncio
async def test_parallel_identical_idempotent_calls_execute_once(
    scope: RunScope, prices: PriceTable
) -> None:
    sent: list[str] = []
    turns = iter(range(2))

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if next(turns) == 0:
            parts = [ToolCallPart("notify", {"text": "incident"}) for _ in range(2)]
        else:
            name = next(t.name for t in info.output_tools if "Diagnosis" in t.name)
            parts = [ToolCallPart(name, {"cause": "x"})]
        return ModelResponse(parts=parts, model_name="scripted")

    agent: Agent[None, object] = Agent(
        FunctionModel(respond, model_name="scripted"),
        output_type=end_state_outputs(Diagnosis),
    )

    @agent.tool_plain(metadata={IDEMPOTENT_METADATA_KEY: True})
    async def notify(text: str) -> str:
        await asyncio.sleep(0.01)
        sent.append(text)
        return "sent"

    outcome = await run(agent, scope, prices, idempotency=InMemoryIdempotencyStore())
    assert outcome.result.status == "done"
    assert sent == ["incident"]
