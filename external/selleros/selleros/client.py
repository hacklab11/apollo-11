"""Messages transport, replay storage, and process-local usage counters."""

import asyncio
import hashlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaMessage

from typing import Literal

from pydantic import BaseModel


class LlmSettings(BaseModel):
    mode: Literal["live", "record", "replay"] = "live"
    cache_path: Path = Path(__file__).resolve().parents[1] / "data/llm_cache.jsonl"
    max_calls_per_hour: int = 600


class BudgetExceeded(Exception):
    """The turn or rolling hourly call budget is exhausted."""


class LLMUnavailable(Exception):
    """The SDK exhausted its retries."""


def _append_record(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as cache:
        cache.write(json.dumps(entry, sort_keys=True) + "\n")


def _read_records(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


class LLMClient:
    def __init__(
        self, settings: LlmSettings, sdk: AsyncAnthropic | None = None
    ) -> None:
        self.settings = settings
        self._sdk = sdk
        self._calls: deque[float] = deque()
        self.tokens_in = 0
        self.tokens_out = 0

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
        identity = {
            "model": request["model"],
            "system": "".join(b["text"] for b in request["system"]),
            "tools": [t["name"] for t in request["tools"]],
            "messages": request["messages"],
        }
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
        _client = LLMClient(LlmSettings())
    return _client


def budget_status() -> dict[str, int | bool]:
    """Snapshot for /api/status; replay tokens count, replay hits use no calls."""
    return get_client().status()
