"""Reusable manual agent loop with validated tools and observable steps."""

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from anthropic import AnthropicError
from pydantic import BaseModel, ValidationError

from eye.contracts import Actor, StepAction, TrailStep
from eye.llm.client import BudgetExceeded, LLMUnavailable, get_client


class AgentRefused(Exception):
    """The model refused even with server-side fallback enabled."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[BaseModel], BaseModel | Awaitable[BaseModel]]
    strict: bool = True


@dataclass
class AgentSpec:
    name: Actor
    system_prompt_file: str
    tools: list[ToolSpec]
    submit_model: type[BaseModel]
    effort: str
    model: str
    max_steps: int
    max_tokens: int = 16000


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()

    def close(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "additionalProperties" not in node:
                node["additionalProperties"] = False
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for value in node:
                close(value)

    close(schema)
    return schema


async def run_agent(
    spec: AgentSpec,
    user_content: str,
    investigation_id: str,
    on_step: Callable[[TrailStep], Awaitable[None]],
) -> BaseModel:
    tools = {tool.name: tool for tool in spec.tools}
    if len(tools) != len(spec.tools) or "submit" in tools:
        raise ValueError("tool names must be unique and may not be submit")
    definitions = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": _schema(t.input_model),
            "strict": t.strict,
        }
        for t in spec.tools
    ]
    definitions.append(
        {
            "name": "submit",
            "description": "Submit the final answer.",
            "input_schema": _schema(spec.submit_model),
            "strict": True,
        }
    )
    definitions.sort(key=lambda t: t["name"])
    system = [
        {
            "type": "text",
            "text": await asyncio.to_thread(
                Path(spec.system_prompt_file).read_text, encoding="utf-8"
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    n = 0

    async def emit(action: StepAction, summary: str, **kwargs: Any) -> None:
        nonlocal n
        n += 1
        await on_step(
            TrailStep(
                id=str(uuid4()),
                investigation_id=investigation_id,
                n=n,
                real_ts=datetime.now(UTC),
                actor=spec.name,
                action=action,
                output_summary=summary[:500] or "Completed",
                **kwargs,
            )
        )

    async def execute(
        block: Any, reason: str
    ) -> tuple[dict[str, Any], BaseModel | None]:
        started = time.monotonic()
        output = None
        error = False
        try:
            if block.name == "submit":
                output = spec.submit_model.model_validate(block.input, extra="forbid")
            else:
                tool = tools[block.name]
                args = tool.input_model.model_validate(block.input, extra="forbid")
                output = tool.fn(args)
                if inspect.isawaitable(output):
                    output = await output
                if not isinstance(output, BaseModel):
                    raise TypeError("tool output must be a BaseModel")
            content = output.model_dump_json()
            summary = str(getattr(output, "summary", "Submitted final answer"))
        except Exception as exc:  # noqa: BLE001 -- tool failures are returned to the model
            error = True
            output = None
            summary = f"{type(exc).__name__}: tool {block.name} failed"
            if isinstance(exc, ValidationError):
                detail = "; ".join(
                    f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                    for item in exc.errors(include_input=False, include_url=False)
                )
            else:
                detail = str(exc)
            content = f"{summary}: {detail[:300]}"
        await emit(
            "tool_call",
            summary,
            tool=block.name,
            input=block.input,
            output=output.model_dump(mode="json") if output else None,
            reason=reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return (
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": error,
            },
            output if block.name == "submit" else None,
        )

    for _ in range(spec.max_steps):
        started = time.monotonic()
        try:
            response = await get_client().create(
                {
                    "model": spec.model,
                    "max_tokens": spec.max_tokens,
                    "system": system,
                    "tools": definitions,
                    "messages": messages,
                    "output_config": {"effort": spec.effort},
                    "tool_choice": {"type": "auto"},
                    "betas": ["server-side-fallback-2026-07-01"],
                    "fallbacks": "default",
                }
            )
        except AnthropicError as exc:
            await emit("note", "LLM unavailable after SDK retries")
            raise LLMUnavailable("LLM unavailable after SDK retries") from exc
        stop = response.stop_reason
        text = " ".join(b.text for b in response.content if b.type == "text")
        await emit(
            "llm_call",
            text or f"LLM stop: {stop}",
            tokens_in=response.usage.input_tokens
            + (response.usage.cache_creation_input_tokens or 0)
            + (response.usage.cache_read_input_tokens or 0),
            tokens_out=response.usage.output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if stop == "refusal":
            await emit("note", "Agent refused")
            raise AgentRefused(spec.name)
        messages.append(
            {
                "role": "assistant",
                "content": [
                    b.model_dump(mode="json", exclude_none=True)
                    for b in response.content
                ],
            }
        )
        if stop == "max_tokens":
            await emit("note", "Maximum output tokens reached")
            messages.append({"role": "user", "content": "Continue. Be shorter."})
            continue
        pending = []
        reason = ""
        for block in response.content:
            if block.type == "text":
                reason = block.text
            elif block.type == "tool_use":
                pending.append(execute(block, reason))
        if not pending:
            messages.append(
                {"role": "user", "content": "Call submit with your final answer."}
            )
            continue
        results = await asyncio.gather(*pending)
        for _, final in results:
            if final is not None:
                return final
        messages.append({"role": "user", "content": [r for r, _ in results]})
    raise BudgetExceeded("agent turn budget exhausted")
