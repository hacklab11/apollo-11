"""The single entry point agents use to run a model: every cap, end state and guard applied."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from agent_runtime.budget import Budget
from agent_runtime.errors import LoopDetected, RunKilled, UnpricedModel
from agent_runtime.guard import RunGuard
from agent_runtime.idempotency import IdempotencyStore
from agent_runtime.kill_switch import KillSwitch, RunScope
from agent_runtime.limits import PriceTable, RunLimits
from agent_runtime.results import (
    AgentResult,
    GaveUp,
    NeedsInput,
    PendingApproval,
    RunUsageSummary,
)
from agent_runtime.tracing import (
    cost_histogram,
    end_state_counter,
    loop_counter,
    tracer,
)


def end_state_outputs[T](result_type: type[T]) -> OutputSpec[Any]:
    """Output types an agent must offer the model so it can end in any explicit state."""
    return [result_type, NeedsInput, GaveUp, DeferredToolRequests]


@dataclass
class RunOutcome[T]:
    result: AgentResult[T]
    messages: list[ModelMessage] = field(default_factory=list)
    """Private history to checkpoint; pass back as `message_history` to resume."""


async def run_agent[T](
    agent: Agent[Any, Any],
    prompt: str | None,
    *,
    scope: RunScope,
    limits: RunLimits,
    prices: PriceTable,
    budget: Budget | None = None,
    deps: Any = None,
    kill_switch: KillSwitch | None = None,
    idempotency: IdempotencyStore | None = None,
    message_history: Sequence[ModelMessage] | None = None,
    deferred_tool_results: DeferredToolResults | None = None,
) -> RunOutcome[T]:
    limits = limits.within(budget)
    guard = RunGuard(
        scope=scope,
        prices=prices,
        max_cost_usd=limits.max_cost_usd,
        loop_threshold=limits.loop_threshold,
        kill_switch=kill_switch,
        idempotency=idempotency,
    )
    with tracer.start_as_current_span(
        "agent_runtime.run",
        attributes={"agent.run_id": scope.run_id, "tenant.id": scope.tenant_id},
    ) as span:
        outcome = await _run(
            agent,
            prompt,
            guard=guard,
            limits=limits,
            budget=budget,
            deps=deps,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
        )
        status = outcome.result.status
        span.set_attribute("agent.end_state", status)
        span.set_attribute("agent.cost_usd", outcome.result.usage.cost_usd)
        if status in ("failed", "gave_up"):
            span.set_status(Status(StatusCode.ERROR, outcome.result.reason))
        end_state_counter.add(1, {"status": status})
        cost_histogram.record(outcome.result.usage.cost_usd)
        return outcome


async def _run[T](
    agent: Agent[Any, Any],
    prompt: str | None,
    *,
    guard: RunGuard,
    limits: RunLimits,
    budget: Budget | None,
    deps: Any,
    message_history: Sequence[ModelMessage] | None,
    deferred_tool_results: DeferredToolResults | None,
) -> RunOutcome[T]:
    if budget is not None and budget.exhausted():
        return _stopped(guard, "gave_up", "budget exhausted before the run started")
    if guard.kill_switch is not None:
        kill_reason = await guard.kill_switch.reason(guard.scope)
        if kill_reason is not None:
            return _stopped(guard, "failed", kill_reason)
    try:
        async with asyncio.timeout(limits.max_wall_clock_s):
            run = await agent.run(
                prompt,
                deps=deps,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
                usage_limits=limits.usage_limits(),
                capabilities=[guard],
            )
    except TimeoutError:
        return _stopped(
            guard, "gave_up", f"wall-clock limit {limits.max_wall_clock_s}s reached"
        )
    except UsageLimitExceeded as exc:
        return _stopped(guard, "gave_up", f"limit reached: {exc}")
    except LoopDetected as exc:
        loop_counter.add(1)
        return _stopped(guard, "gave_up", f"loop detected: {exc}")
    except RunKilled as exc:
        return _stopped(guard, "failed", str(exc))
    except (
        ModelAPIError,
        FallbackExceptionGroup,
        UnexpectedModelBehavior,
        UnpricedModel,
    ) as exc:
        return _stopped(guard, "failed", f"{type(exc).__name__}: {exc}")

    usage = _summary(guard, run.usage)
    messages = run.all_messages()
    output = run.output
    if isinstance(output, NeedsInput):
        result = AgentResult[T](
            status="needs_input", question=output.question, usage=usage
        )
    elif isinstance(output, GaveUp):
        result = AgentResult[T](status="gave_up", reason=output.reason, usage=usage)
    elif isinstance(output, DeferredToolRequests):
        result = AgentResult[T](
            status="needs_input",
            question="approval required for: "
            + ", ".join(call.tool_name for call in output.approvals),
            pending_approvals=[
                PendingApproval(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=call.args_as_dict(),
                )
                for call in output.approvals
            ],
            usage=usage,
        )
    else:
        result = AgentResult[T](status="done", result=output, usage=usage)
    return RunOutcome(result=result, messages=messages)


def _summary(guard: RunGuard, usage: Any = None) -> RunUsageSummary:
    if usage is None:
        return RunUsageSummary(cost_usd=guard.cost_usd)
    return RunUsageSummary(
        requests=usage.requests,
        tool_calls=usage.tool_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=guard.cost_usd,
    )


def _stopped[T](guard: RunGuard, status: str, reason: str) -> RunOutcome[T]:
    return RunOutcome(
        result=AgentResult[T].model_validate(
            {"status": status, "reason": reason, "usage": _summary(guard)}
        )
    )
