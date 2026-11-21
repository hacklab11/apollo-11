"""Messages transport, replay storage, and process-local usage counters."""

import asyncio
import hashlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from anthropic import AnthropicError, AsyncAnthropic
from anthropic.types.beta import BetaMessage

from eye.config import LlmSettings, load_settings


class BudgetExceeded(Exception):
    """The turn or rolling hourly call budget is exhausted."""


class LLMUnavailable(AnthropicError):
    """The provider could not complete the request."""


def _chat_request(request: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n".join(b["text"] for b in request["system"])}
    ]
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            messages.append({"role": message["role"], "content": content})
            continue
        text = "\n".join(b["text"] for b in content if b["type"] == "text")
        if message["role"] == "assistant":
            assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b["input"], sort_keys=True),
                    },
                }
                for b in content
                if b["type"] == "tool_use"
            ]
            if calls:
                assistant["tool_calls"] = calls
            messages.append(assistant)
        else:
            for block in content:
                if block["type"] == "tool_result":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        }
                    )
            if text:
                messages.append({"role": "user", "content": text})
    return {
        "model": request["model"],
        "max_tokens": request["max_tokens"],
        "messages": messages,
        "tool_choice": (
            {"type": "function", "function": {"name": request["tool_choice"]["name"]}}
            if request.get("tool_choice", {}).get("type") == "tool"
            else "auto"
        ),
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in request["tools"]
        ],
    }


def _chat_response(raw: dict[str, Any], model: str) -> BetaMessage:
    choice = raw["choices"][0]
    message = choice["message"]
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        try:
            args = json.loads(call["function"]["arguments"])
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
        except (ValueError, TypeError):
            args = {
                "__invalid_json_arguments__": "Arguments must be a valid JSON object"
            }
        content.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["function"]["name"],
                "input": args,
            }
        )
    finish = {
        "tool_calls": "tool_use",
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "refusal",
    }[choice["finish_reason"]]
    if message.get("refusal"):
        finish = "refusal"
    usage = raw.get("usage") or {}
    return BetaMessage.model_validate(
        {
            "id": raw["id"],
            "type": "message",
            "role": "assistant",
            "model": raw.get("model", model),
            "content": content,
            "stop_reason": finish,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }
    )


def _append_record(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as cache:
        cache.write(json.dumps(entry, sort_keys=True) + "\n")


def _read_records(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


class LLMClient:
    def __init__(
        self,
        settings: LlmSettings,
        sdk: AsyncAnthropic | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._sdk = sdk
        self._http_client = http_client
        self._calls: deque[float] = deque()
        self.tokens_in = 0
        self.tokens_out = 0

    async def _openai_create(self, request: dict[str, Any]) -> BetaMessage:
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise LLMUnavailable(
                f"Missing API key environment variable: {self.settings.api_key_env}"
            )
        payload = _chat_request(request)
        if self._http_client is None:
            async with httpx.AsyncClient(timeout=120.0) as client:
                return await self._chat_post(client, key, payload)
        return await self._chat_post(self._http_client, key, payload)

    async def _chat_post(
        self, client: httpx.AsyncClient, key: str, payload: dict[str, Any]
    ) -> BetaMessage:
        for attempt in range(3):
            try:
                response = await client.post(
                    self.settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise LLMUnavailable(
                        "Chat Completions connection failed after retries"
                    ) from None
            else:
                if response.status_code < 400:
                    try:
                        return _chat_response(response.json(), payload["model"])
                    except (
                        ValueError,
                        KeyError,
                        TypeError,
                        IndexError,
                        AttributeError,
                    ):
                        raise LLMUnavailable(
                            "Invalid Chat Completions response"
                        ) from None
                if response.status_code != 429 and response.status_code < 500:
                    raise LLMUnavailable(
                        f"Chat Completions HTTP {response.status_code}"
                    )
                if attempt == 2:
                    raise LLMUnavailable(
                        f"Chat Completions HTTP {response.status_code} after retries"
                    )
            await asyncio.sleep(0.1 * (2**attempt))
        raise AssertionError("retry loop exhausted")

    def status(self) -> dict[str, int | bool]:
        now = time.monotonic()
        while self._calls and self._calls[0] <= now - 3600:
            self._calls.popleft()
        return {
            "calls_per_hour": len(self._calls),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "budget_paused": len(self._calls) >= self.settings.max_calls_per_hour,
        }

    async def create(self, request: dict[str, Any]) -> BetaMessage:
        if self.settings.provider == "anthropic" and "tool_choice" in request:
            request = {**request, "tool_choice": {"type": "auto"}}
        identity = {
            "model": request["model"],
            "system": "".join(b["text"] for b in request["system"]),
            "tools": [t["name"] for t in request["tools"]],
            "messages": request["messages"],
        }
        if self.settings.provider != "anthropic":
            identity["provider"] = self.settings.provider
            identity["base_url"] = self.settings.base_url
        digest = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(digest.encode()).hexdigest()
        path = Path(self.settings.cache_path)
        response = None
        if self.settings.mode == "replay":
            records = await asyncio.to_thread(_read_records, path)
            if records:
                for line in records.splitlines():
                    entry = json.loads(line)
                    if entry["key"] == key:
                        response = BetaMessage.model_validate(entry["response_json"])
                        break
            if response is None:
                print(json.dumps({"event": "replay_miss", "key": key}))
        if response is None:
            if self.status()["budget_paused"]:
                raise BudgetExceeded("hourly call budget exhausted")
            self._calls.append(time.monotonic())
            if self.settings.provider == "openai_compatible":
                response = await self._openai_create(request)
            else:
                if self._sdk is None:
                    self._sdk = AsyncAnthropic(max_retries=2)
                response = await self._sdk.beta.messages.create(**request)
            if self.settings.mode == "record":
                await asyncio.to_thread(
                    _append_record,
                    path,
                    {
                        "key": key,
                        "request_digest": key,
                        "response_json": response.model_dump(mode="json"),
                    },
                )
        usage = response.usage
        self.tokens_in += (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        self.tokens_out += usage.output_tokens
        return response


_client: LLMClient | None = None


def configure_client(client: LLMClient) -> None:
    """Install the shared transport (also supports offline SDK transports)."""
    global _client
    _client = client


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient(load_settings().llm)
    return _client


def budget_status() -> dict[str, int | bool]:
    """Snapshot for /api/status; replay tokens count, replay hits use no calls."""
    return get_client().status()
