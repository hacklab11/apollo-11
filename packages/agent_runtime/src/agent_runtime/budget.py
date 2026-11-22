"""End-to-end budget that flows down through delegated agents."""

from datetime import UTC, datetime
from typing import Any, Self

from agent_runtime.errors import DelegationRefused
from eye.contracts_v2 import BUDGET_METADATA_KEY
from eye.contracts_v2 import Budget as BudgetModel


class Budget(BudgetModel):
    """What is left for a run and everything it delegates to."""

    def seconds_left(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return max(0.0, (self.deadline - now).total_seconds())

    def exhausted(self, now: datetime | None = None) -> bool:
        return self.tokens == 0 or self.cost_usd == 0 or self.seconds_left(now) == 0

    def spend(self, *, tokens: int, cost_usd: float) -> Self:
        return self.model_copy(
            update={
                "tokens": max(0, self.tokens - tokens),
                "cost_usd": max(0.0, self.cost_usd - cost_usd),
            }
        )

    def delegate(self, fan_out: int = 1) -> Self:
        """Budget for each of `fan_out` parallel delegations; the remainder is split evenly."""
        if self.depth + 1 > self.max_depth:
            raise DelegationRefused(
                f"delegation depth {self.depth + 1} exceeds max {self.max_depth}"
            )
        if fan_out > self.max_fan_out:
            raise DelegationRefused(f"fan-out {fan_out} exceeds max {self.max_fan_out}")
        if fan_out < 1:
            raise ValueError("fan_out must be at least 1")
        return self.model_copy(
            update={
                "tokens": self.tokens // fan_out,
                "cost_usd": self.cost_usd / fan_out,
                "depth": self.depth + 1,
            }
        )

    def to_metadata(self) -> dict[str, Any]:
        return {BUDGET_METADATA_KEY: self.model_dump(mode="json")}

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> Self | None:
        if not metadata or BUDGET_METADATA_KEY not in metadata:
            return None
        return cls.model_validate(metadata[BUDGET_METADATA_KEY])
