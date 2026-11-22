"""Check whether a measured anomaly is explained by a plan or normal seasonality."""

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from uuid import uuid4

import duckdb
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext, Tool, ToolOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.output import OutputContext
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition

from agent_runtime import (
    InMemoryKillSwitch,
    KillSwitch,
    ModelPrice,
    PriceTable,
    RunLimits,
    RunScope,
    end_state_outputs,
    model_chain,
    run_agent,
)
from eye.agents.investigation_tools import (
    _ControlInput,
    _EventsInput,
    _MetricInput,
    _Window,
    _WindowTools,
)
from eye.config import LlmSettings, SentinelSettings
from eye.contracts import Anomaly, Hypothesis, TrailStep
from eye.contracts_v2 import SkepticResult
from eye.registry.loader import Registry
from eye.tools.models import (
    BaselineCompareOutput,
    CheckFreshnessOutput,
    CompareSegmentsOutput,
    ListEventsOutput,
    PlannedEffectCheckInput,
    PlannedEffectCheckOutput,
)
from eye.warehouse import operations as ops
from eye.warehouse.queries import DataQueries, LocalQueries

_PROMPT = files("eye.llm").joinpath("prompts/skeptic.md")
_MAX_STEPS = 6


def _prices(llm: LlmSettings) -> PriceTable:
    return PriceTable(
        prices={
            name: ModelPrice(**price.model_dump()) for name, price in llm.prices.items()
        }
    )


@dataclass
class _Deps:
    data: DataQueries
    tools: _WindowTools
    entry: _MetricInput
    freshness: SentinelSettings
    investigation_id: str
    on_step: Callable[[TrailStep], Awaitable[None]]
    checks: dict[str, PlannedEffectCheckOutput] = field(default_factory=dict)
    seasonal: bool = False
    n: int = 0


class _Trail(AbstractCapability[_Deps]):
    async def after_model_request(
        self,
        ctx: RunContext[_Deps],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        ctx.deps.n += 1
        text = " ".join(
            part.content for part in response.parts if isinstance(part, TextPart)
        )
        await ctx.deps.on_step(
            TrailStep(
                id=str(uuid4()),
                investigation_id=ctx.deps.investigation_id,
                n=ctx.deps.n,
                real_ts=datetime.now(UTC),
                actor="skeptic",
                action="llm_call",
                output_summary=(text or f"LLM stop: {response.finish_reason}")[:500],
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
            )
        )
        return response

    async def wrap_output_validate(
        self,
        ctx: RunContext[_Deps],
        *,
        output_context: OutputContext,
        output: Any,
        handler: Any,
    ) -> Any:
        call = output_context.tool_call
        if call is None:
            return await handler(output)
        return await self.wrap_tool_execute(
            ctx,
            call=call,
            tool_def=output_context.tool_def,
            args=output,
            handler=handler,
        )

    async def wrap_tool_execute(
        self,
        ctx: RunContext[_Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        handler: Any,
    ) -> Any:
        started = time.monotonic()
        output = None
        summary = f"tool {call.tool_name} failed"
        try:
            output = await handler(args)
            summary = str(getattr(output, "summary", "Submitted final answer"))
            return output
        except Exception as exc:
            summary = f"{type(exc).__name__}: tool {call.tool_name} failed"
            raise
        finally:
            ctx.deps.n += 1
            await ctx.deps.on_step(
                TrailStep(
                    id=str(uuid4()),
                    investigation_id=ctx.deps.investigation_id,
                    n=ctx.deps.n,
                    real_ts=datetime.now(UTC),
                    actor="skeptic",
                    action="tool_call",
                    tool=call.tool_name,
                    input=call.args_as_dict(),
                    output=output.model_dump(mode="json")
                    if isinstance(output, BaseModel)
                    else None,
                    output_summary=summary[:500] or "Completed",
                    reason="",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )


def _model(llm: LlmSettings) -> Model:
    def build(name: str) -> Model:
        if llm.provider == "anthropic":
            return AnthropicModel(name)
        return OpenAIChatModel(
            name,
            provider=OpenAIProvider(
                base_url=llm.base_url,
                api_key=os.environ[llm.api_key_env],
            ),
        )

    return model_chain(
        build(llm.model_for("skeptic")),
        *(build(name) for name in llm.fallback_models.get("skeptic", [])),
    )


class _PlanInput(BaseModel):
    event_id: str


def list_events(ctx: RunContext[_Deps], args: _EventsInput) -> ListEventsOutput:
    try:
        return ctx.deps.tools.events(args, planned_only=True)
    except ValueError as exc:
        raise ModelRetry(str(exc)) from exc


def planned_effect_check(
    ctx: RunContext[_Deps], args: _PlanInput
) -> PlannedEffectCheckOutput:
    deps = ctx.deps
    try:
        events = list_events(
            ctx, _EventsInput(country=deps.entry.segment.get("country"))
        )
        if not any(
            event.id == args.event_id
            and all(
                deps.entry.segment.get(dim) in value
                if isinstance(value, list)
                else deps.entry.segment.get(dim) == value
                for dim, value in event.scope.items()
            )
            for event in events.events
        ):
            raise ValueError("Plan is outside the entry segment or evidence window")
        result = deps.data.read(
            ops.PLANNED,
            PlannedEffectCheckInput(
                event_id=args.event_id,
                **deps.entry.model_dump(),
                **deps.tools.window.bounds(),
            ),
        )
        deps.checks[args.event_id] = result
        return result
    except ValueError as exc:
        raise ModelRetry(str(exc)) from exc


def check_freshness(ctx: RunContext[_Deps], args: _MetricInput) -> CheckFreshnessOutput:
    try:
        return ctx.deps.tools.freshness(args)
    except ValueError as exc:
        raise ModelRetry(str(exc)) from exc


def baseline_compare(
    ctx: RunContext[_Deps], args: _MetricInput
) -> BaselineCompareOutput:
    try:
        result = ctx.deps.tools.baseline(args)
        if args == ctx.deps.entry:
            ctx.deps.seasonal = bool(result.points) and all(
                point.z is not None and abs(point.z) < ctx.deps.freshness.z_threshold
                for point in result.points
            )
        return result
    except ValueError as exc:
        raise ModelRetry(str(exc)) from exc


def compare_segments(
    ctx: RunContext[_Deps], args: _ControlInput
) -> CompareSegmentsOutput:
    try:
        return ctx.deps.tools.controls(args)
    except ValueError as exc:
        raise ModelRetry(str(exc)) from exc


class Skeptic:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection | None = None,
        registry: Registry | None = None,
        *,
        freshness: SentinelSettings | None = None,
        llm: LlmSettings | None = None,
        data: DataQueries | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._freshness = freshness or SentinelSettings()
        self._llm = llm or LlmSettings()
        if data is None:
            assert con is not None and registry is not None
            data = LocalQueries(con, registry, settings=self._freshness)
        self._data = data
        self.kill_switch = kill_switch or InMemoryKillSwitch()
        outputs = end_state_outputs(SkepticResult)
        outputs[0] = ToolOutput(SkepticResult, name="submit")
        self._agent = Agent(
            _model(self._llm),
            name="skeptic",
            deps_type=_Deps,
            instructions=_PROMPT.read_text(),
            output_type=outputs,
            model_settings={
                "max_tokens": self._llm.max_tokens,
                "anthropic_effort": "medium",
            },
            capabilities=[_Trail()],
            tools=[
                Tool(list_events, description="List planned events only."),
                Tool(
                    planned_effect_check,
                    description="Compare this plan to the entry metric and segment.",
                ),
                Tool(
                    check_freshness,
                    description="Check metric completeness.",
                ),
                Tool(
                    baseline_compare,
                    description="Compare to the seasonal baseline.",
                ),
                Tool(
                    compare_segments,
                    description="Compare affected and control segments.",
                ),
            ],
        )

    async def review(
        self,
        anomaly: Anomaly,
        investigation_id: str,
        on_step: Callable[[TrailStep], Awaitable[None]],
        *,
        hypothesis: Hypothesis | None = None,
    ) -> SkepticResult:
        window = _Window.from_anomaly(anomaly)
        tools = _WindowTools(self._data, window)
        entry = _MetricInput(metric=anomaly.metric, segment=anomaly.segment)
        deps = _Deps(
            self._data, tools, entry, self._freshness, investigation_id, on_step
        )
        checks = deps.checks
        payload = {
            "entry": anomaly.model_dump(mode="json"),
            "hypothesis": hypothesis.model_dump(mode="json") if hypothesis else None,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "evidence_start": window.lookback.isoformat(),
        }
        outcome = await run_agent(
            self._agent,
            json.dumps(payload, sort_keys=True),
            deps=deps,
            scope=RunScope(run_id=investigation_id, tenant_id="marketplace"),
            limits=RunLimits(
                max_steps=_MAX_STEPS,
                max_tokens=self._llm.max_tokens,
                max_tool_calls=30,
                max_cost_usd=3,
            ),
            prices=_prices(self._llm),
            kill_switch=self.kill_switch,
        )
        if outcome.result.status != "done":
            return SkepticResult(
                verdict="not_innocent",
                explained_share=0,
                reasons=[
                    outcome.result.reason
                    or outcome.result.question
                    or "Innocence was not established."
                ],
                evidence=[],
            )
        result = SkepticResult.model_validate(outcome.result.result)

        matches = [
            check.explained_share
            for check in checks.values()
            if check.verdict == "matches"
        ]
        if result.verdict in {"innocent", "data_issue"}:
            freshness = await asyncio.to_thread(tools.freshness, entry)
            if freshness.verdict != "ok":
                return result.model_copy(
                    update={
                        "verdict": "data_issue",
                        "explained_share": 0.0,
                        "reasons": [*result.reasons, freshness.summary],
                    }
                )
            if result.verdict == "data_issue":
                return result.model_copy(
                    update={
                        "verdict": "not_innocent",
                        "explained_share": 0.0,
                        "reasons": [
                            *result.reasons,
                            "Entry data is complete and on time.",
                        ],
                    }
                )
        if result.verdict == "innocent":
            if matches:
                return result.model_copy(update={"explained_share": max(matches)})
            if deps.seasonal:
                return result.model_copy(update={"explained_share": 1.0})
            return result.model_copy(
                update={
                    "verdict": "not_innocent",
                    "explained_share": 0.0,
                    "reasons": [
                        *result.reasons,
                        "No matching planned effect or normal seasonal baseline.",
                    ],
                }
            )
        if result.verdict == "partly_innocent":
            share = max(
                (
                    check.explained_share
                    for check in checks.values()
                    if check.verdict in {"matches", "exceeds"}
                ),
                default=0.0,
            )
            return result.model_copy(
                update={
                    "verdict": "partly_innocent" if share else "not_innocent",
                    "explained_share": share,
                }
            )
        return result.model_copy(update={"explained_share": 0.0})
