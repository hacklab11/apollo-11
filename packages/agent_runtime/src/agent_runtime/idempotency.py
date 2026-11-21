"""Idempotency keys so a retried or resumed side-effecting tool call runs once."""

import hashlib
import json
from typing import Any, Protocol

IDEMPOTENT_METADATA_KEY = "idempotent"


def idempotency_key(run_id: str, tool_name: str, args: dict[str, Any]) -> str:
    """Same run, tool and arguments give the same key, even when a resumed model re-issues the call."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{tool_name}\0{canonical}".encode()).hexdigest()
    return f"{run_id}:{digest}"


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> tuple[bool, Any]:
        """(found, stored result)."""
        ...

    async def put(self, key: str, result: Any) -> None: ...


class InMemoryIdempotencyStore:
    """Process-local store; services back the same protocol with a unique index in Postgres."""

    def __init__(self) -> None:
        self._results: dict[str, Any] = {}

    async def get(self, key: str) -> tuple[bool, Any]:
        if key in self._results:
            return True, self._results[key]
        return False, None

    async def put(self, key: str, result: Any) -> None:
        self._results.setdefault(key, result)
