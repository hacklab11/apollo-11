"""Messages transport, replay storage, and process-local usage counters."""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from typing import Any

import httpx
from anthropic import (
    AnthropicError,
    APIRequest,
    AsyncAnthropic,
    AsyncAPIResponse,
    AsyncCallNext,
)
from anthropic.types.beta import BetaMessage

from eye.config import LlmSettings, load_settings
from eye.llm.chat_context import compact_chat_context


class BudgetExceeded(Exception):
    """The turn, hourly, or per-run call budget is exhausted."""


class RunCallLimitExceeded(BudgetExceeded):
    """The process has used its allotment of provider requests."""


class LLMUnavailable(AnthropicError):
    """The provider could not complete the request."""


class DailyQuotaExceeded(LLMUnavailable):
    """The provider's daily request quota is exhausted."""


class MissingProviderCredentials(LLMUnavailable):
    def __init__(self, env_var: str) -> None:
        self.env_var = env_var
        super().__init__(f"Missing API key environment variable: {env_var}")


_MAX_RETRY_DELAY = 60.0
_DAILY_QUOTA = re.compile(
    r"\b(?:free-models-per-day|daily (?:model )?(?:quota|rate limit))\b", re.IGNORECASE
)
_SDK_TYPE = AsyncAnthropic
_log = logging.getLogger(__name__)


def _retry_delay(response: httpx.Response, fallback: float) -> float:
    retry = response.headers.get("Retry-After")
    reset = response.headers.get("X-RateLimit-Reset")
    try:
        if retry is not None:
            try:
                delay = float(retry)
            except ValueError:
                delay = parsedate_to_datetime(retry).timestamp() - time.time()
        elif reset is not None:
            stamp = float(reset)
            delay = (stamp / 1000 if stamp > 1e12 else stamp) - time.time()
        else:
            return fallback
        if isfinite(delay):
            return min(_MAX_RETRY_DELAY, max(0.0, delay))
    except (ValueError, TypeError, OverflowError):
        pass
    return fallback


def _provider_detail(error: Any, key: str) -> str:
    parts = []
    if isinstance(error, dict):
        parts.append(str(error.get("message", "")))
        metadata = error.get("metadata")
        if isinstance(metadata, dict) and metadata.get("raw"):
            raw = metadata["raw"]
            try:
                raw = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                pass
            if isinstance(raw, dict):
                nested = raw.get("error", raw)
                raw = nested.get("message", "") if isinstance(nested, dict) else nested
            parts.append(str(raw))
    elif isinstance(error, str):
        parts.append(error)
    detail = " ".join(" ".join(parts).split()).replace(key, "[REDACTED]")
    detail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", detail)
    return detail


def _chat_request(request: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n".join(b["text"] for b in request["system"])}
    ]
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            messages.append(
                {
                    "role": message["role"],
                    "content": compact_chat_context(content)
                    if message["role"] == "user"
                    else content,
                }
            )
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
                            "content": compact_chat_context(block["content"]),
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


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, float):
        # BLAS backends can disagree in the last bits of computed tool results.
        rounded = round(value, 10)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {key: _fingerprint_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed, end = json.JSONDecoder().raw_decode(value.lstrip())
        except ValueError:
            return value
        suffix = value.lstrip()[end:]
        return (
            json.dumps(
                _fingerprint_value(parsed), sort_keys=True, separators=(",", ":")
            )
            + suffix
        )
    return value


def _request_fingerprint(identity: dict[str, Any]) -> str:
    # Tool results and user context contain JSON strings, not just nested objects.
    digest = json.dumps(
        _fingerprint_value(identity), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(digest.encode()).hexdigest()


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
        self._provider_requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._cooldown_until = 0.0
        self._cooldown_reason = ""
        self._daily_quota_reason = ""

    def terminal_error(self) -> DailyQuotaExceeded | RunCallLimitExceeded | None:
        if self._daily_quota_reason:
            return DailyQuotaExceeded(self._daily_quota_reason)
        limit = self.settings.max_calls_per_run
        if self._provider_requests >= limit:
            return RunCallLimitExceeded(
                f"Per-run model-call limit reached ({limit} provider requests); restart to reset it"
            )
        return None

    def _count_request(self) -> None:
        if error := self.terminal_error():
            raise error
        self._provider_requests += 1

    async def _count_sdk_attempt(
        self, request: APIRequest, call_next: AsyncCallNext
    ) -> AsyncAPIResponse[Any]:
        self._count_request()
        return await call_next(request)

    async def _openai_create(self, request: dict[str, Any]) -> BetaMessage:
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise MissingProviderCredentials(self.settings.api_key_env)
        payload = _chat_request(request)
        if self._http_client is None:
            async with httpx.AsyncClient(timeout=120.0) as client:
                return await self._chat_post(client, key, payload)
        return await self._chat_post(self._http_client, key, payload)

    async def _anthropic_create(self, request: dict[str, Any]) -> BetaMessage:
        try:
            if self._sdk is None:
                self._sdk = AsyncAnthropic(max_retries=2)
            sdk = self._sdk
            if isinstance(sdk, _SDK_TYPE):
                sdk = sdk.with_middleware(self._count_sdk_attempt)
            else:
                self._count_request()
            return await sdk.beta.messages.create(**request)
        except TypeError as exc:
            if "Could not resolve authentication method" not in str(exc):
                raise
            raise MissingProviderCredentials("ANTHROPIC_API_KEY") from exc

    async def _chat_post(
        self, client: httpx.AsyncClient, key: str, payload: dict[str, Any]
    ) -> BetaMessage:
        for attempt in range(3):
            delay = 0.1 * (2**attempt)
            self._count_request()
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
                try:
                    raw = response.json()
                except ValueError:
                    raw = None
                error = raw.get("error") if isinstance(raw, dict) else None
                if response.status_code < 400 and error is None:
                    try:
                        return _chat_response(raw, payload["model"])
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
                status = response.status_code
                message = f"Chat Completions HTTP {status}"
                if status < 400:
                    try:
                        status = (
                            int(error.get("code", 502))
                            if isinstance(error, dict)
                            else 502
                        )
                    except (ValueError, TypeError):
                        status = 502
                    message += f" (provider {status})"
                detail = _provider_detail(error if error is not None else raw, key)
                if status == 429 and _DAILY_QUOTA.search(detail):
                    if not self._daily_quota_reason:
                        self._daily_quota_reason = (
                            "Daily model request quota exhausted; provider calls stopped for "
                            "this process. Restart after the quota resets."
                        )
                        _log.warning(
                            json.dumps(
                                {
                                    "event": "llm_daily_quota_exhausted",
                                    "detail": self._daily_quota_reason,
                                }
                            )
                        )
                    raise DailyQuotaExceeded(self._daily_quota_reason)
                if detail:
                    message += ": " + detail[:300]
                if status != 429 and status < 500:
                    raise LLMUnavailable(message)
                delay = _retry_delay(response, delay)
                if attempt == 2:
                    if status == 429:
                        self._cooldown_until = time.monotonic() + _retry_delay(
                            response, _MAX_RETRY_DELAY
                        )
                        self._cooldown_reason = message
                    raise LLMUnavailable(message + " after retries")
            await asyncio.sleep(delay)
        raise AssertionError("retry loop exhausted")

    def status(self) -> dict[str, int | bool]:
        now = time.monotonic()
        while self._calls and self._calls[0] <= now - 3600:
            self._calls.popleft()
        return {
            "calls_per_hour": len(self._calls),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "budget_paused": (
                self.terminal_error() is not None
                or len(self._calls) >= self.settings.max_calls_per_hour
            ),
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
        tool_choice = request.get("tool_choice", {"type": "auto"})
        if tool_choice["type"] != "auto":
            identity["tool_choice"] = tool_choice
        digest = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        legacy_key = hashlib.sha256(digest.encode()).hexdigest()
        key = _request_fingerprint(identity)
        path = Path(self.settings.cache_path)
        response = None
        if self.settings.mode == "replay":
            records = await asyncio.to_thread(_read_records, path)
            if records:
                for line in records.splitlines():
                    entry = json.loads(line)
                    if entry["key"] in {key, legacy_key}:
                        response = BetaMessage.model_validate(entry["response_json"])
                        break
            if response is None:
                print(json.dumps({"event": "replay_miss", "key": key}))
        if response is None:
            if error := self.terminal_error():
                raise error
            if self.settings.provider == "openai_compatible":
                remaining = self._cooldown_until - time.monotonic()
                if remaining > 0:
                    raise LLMUnavailable(
                        f"Chat Completions rate-limit cooldown ({remaining:.0f}s remaining): "
                        + self._cooldown_reason
                    )
            if self.status()["budget_paused"]:
                raise BudgetExceeded("hourly call budget exhausted")
            self._calls.append(time.monotonic())
            if self.settings.provider == "openai_compatible":
                response = await self._openai_create(request)
            else:
                response = await self._anthropic_create(request)
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
