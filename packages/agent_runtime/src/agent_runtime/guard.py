"""The capability that enforces kill switches, cost caps, loop detection and idempotency inside a run."""

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ToolCallPart
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
        self.cost_usd += self.prices.cost(response.model_name or "", response.usage)
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
        self._track_repeats(call)
        if not (tool_def.metadata or {}).get(IDEMPOTENT_METADATA_KEY):
            return await handler(args)
        if self.idempotency is None:
            raise RuntimeError(
                f"tool {call.tool_name!r} is idempotent but the run has no idempotency store"
            )
        key = idempotency_key(self.scope.run_id, call.tool_name, call.args_as_dict())
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
