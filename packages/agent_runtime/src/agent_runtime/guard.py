"""The capability that enforces kill switches, cost caps, loop detection and idempotency inside a run."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from agent_runtime.errors import CostLimitExceeded, LoopDetected, RunKilled
from agent_runtime.idempotency import (
    IDEMPOTENT_METADATA_KEY,
    IdempotencyStore,
    idempotency_key,
)
from agent_runtime.kill_switch import KillSwitch, RunScope
from agent_runtime.limits import PriceTable
from agent_runtime.models import serving_model
from agent_runtime.tracing import redact_text

_LOG = logging.getLogger("agent_runtime.trace")


def _short(value: Any, limit: int = 400) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return redact_text(" ".join(text.split()))[:limit]


def _parts(response: ModelResponse) -> list[str]:
    """One short line per response part: reasoning, text or tool call."""
    parts = []
    for part in response.parts:
        if isinstance(part, ThinkingPart):
            parts.append(
                f"reasoning[{len(part.content)} chars]: {_short(part.content)}"
            )
        elif isinstance(part, TextPart):
            parts.append(f"text: {_short(part.content)}")
        elif isinstance(part, ToolCallPart):
            parts.append(f"call {part.tool_name}({_short(part.args_as_dict())})")
    return parts


@dataclass
class RunGuard(AbstractCapability[Any]):
    """Per-run state; build a fresh one for every run."""

    scope: RunScope
    prices: PriceTable
    max_cost_usd: float
    loop_threshold: int
    kill_switch: KillSwitch | None = None
    idempotency: IdempotencyStore | None = None
    cost_usd: float = 0.0
    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    _last_call: tuple[str, str] | None = field(default=None, repr=False)
    _repeats: int = field(default=0, repr=False)

    async def _check_kill(self) -> None:
        if self.kill_switch is not None:
            reason = await self.kill_switch.reason(self.scope)
            if reason is not None:
                raise RunKilled(reason)

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        await self._check_kill()
        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        self.requests += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.cost_usd += self.prices.cost(
            response.model_name or "",
            response.usage,
            requested=serving_model(request_context.model, response),
        )
        if _LOG.isEnabledFor(logging.INFO):
            _LOG.info(
                "agent model_response run=%s request=%d model=%s tokens_in=%d "
                "tokens_out=%d parts=%s",
                self.scope.run_id,
                self.requests,
                response.model_name,
                response.usage.input_tokens,
                response.usage.output_tokens,
                json.dumps(_parts(response)),
            )
        if self.cost_usd > self.max_cost_usd:
            raise CostLimitExceeded(
                f"cost ${self.cost_usd:.6f} exceeds cap ${self.max_cost_usd:.6f}"
            )
        return response

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        handler: Any,
    ) -> Any:
        await self._check_kill()
        self.tool_calls += 1
        self._track_repeats(call)
        try:
            result = await self._execute(call, tool_def, args, handler)
        except Exception as exc:
            _LOG.warning(
                "agent tool_error run=%s tool=%s args=%s error=%s: %s",
                self.scope.run_id,
                call.tool_name,
                _short(call.args_as_dict()),
                type(exc).__name__,
                _short(str(exc)),
            )
            raise
        _LOG.info(
            "agent tool_result run=%s tool=%s args=%s result=%s",
            self.scope.run_id,
            call.tool_name,
            _short(call.args_as_dict()),
            _short(result),
        )
        return result

    async def _execute(
        self, call: ToolCallPart, tool_def: ToolDefinition, args: Any, handler: Any
    ) -> Any:
        if not (tool_def.metadata or {}).get(IDEMPOTENT_METADATA_KEY):
            return await handler(args)
        if self.idempotency is None:
            raise RuntimeError(
                f"tool {call.tool_name!r} is idempotent but the run has no idempotency store"
            )
        key = idempotency_key(self.scope.run_id, call.tool_name, call.args_as_dict())
        # PydanticAI runs tool calls in parallel; serialize identical ones so only one executes.
        async with self._locks.setdefault(key, asyncio.Lock()):
            found, stored = await self.idempotency.get(key)
            if found:
                return stored
            result = await handler(args)
            await self.idempotency.put(key, result)
            return result

    def _track_repeats(self, call: ToolCallPart) -> None:
        signature = (
            call.tool_name,
            json.dumps(call.args_as_dict(), sort_keys=True, default=str),
        )
        self._repeats = self._repeats + 1 if signature == self._last_call else 1
        self._last_call = signature
        if self._repeats >= self.loop_threshold:
            raise LoopDetected(
                f"{call.tool_name} called {self._repeats} times in a row with the same arguments"
            )
