"""Kill switches for one run, one tenant, or the whole system."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class RunScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    tenant_id: str


class KillSwitch(Protocol):
    async def reason(self, scope: RunScope) -> str | None:
        """Why this run must stop, or None if it may continue."""
        ...


class InMemoryKillSwitch:
    """Process-local kill switch; the orchestrator backs the same protocol with Postgres."""

    def __init__(self) -> None:
        self._system: str | None = None
        self._tenants: dict[str, str] = {}
        self._runs: dict[str, str] = {}

    def kill_system(self, reason: str) -> None:
        self._system = reason

    def kill_tenant(self, tenant_id: str, reason: str) -> None:
        self._tenants[tenant_id] = reason

    def kill_run(self, run_id: str, reason: str) -> None:
        self._runs[run_id] = reason

    def clear(self) -> None:
        self._system = None
        self._tenants.clear()
        self._runs.clear()

    async def reason(self, scope: RunScope) -> str | None:
        if self._system is not None:
            return f"system killed: {self._system}"
        if scope.tenant_id in self._tenants:
            return f"tenant {scope.tenant_id} killed: {self._tenants[scope.tenant_id]}"
        if scope.run_id in self._runs:
            return f"run {scope.run_id} killed: {self._runs[scope.run_id]}"
        return None
