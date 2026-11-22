"""Per-run hard caps and the pinned price table used to cap cost."""

from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.models import Model
from pydantic_ai.usage import RequestUsage, UsageLimits

from agent_runtime.budget import Budget
from agent_runtime.errors import UnpricedModel


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=20, ge=1)
    max_tool_calls: int = Field(default=40, ge=0)
    max_tokens: int = Field(default=200_000, ge=1)
    max_cost_usd: float = Field(default=0.10, ge=0)
    max_wall_clock_s: float = Field(default=120.0, gt=0)
    loop_threshold: int = Field(default=3, ge=2)

    def within(self, budget: Budget | None) -> "RunLimits":
        """These limits tightened to what the caller's budget has left."""
        if budget is None:
            return self
        return self.model_copy(
            update={
                "max_tokens": max(1, min(self.max_tokens, budget.tokens)),
                "max_cost_usd": min(self.max_cost_usd, budget.cost_usd),
                "max_wall_clock_s": max(
                    0.001, min(self.max_wall_clock_s, budget.seconds_left())
                ),
            }
        )

    def usage_limits(self) -> UsageLimits:
        return UsageLimits(
            request_limit=self.max_steps,
            tool_calls_limit=self.max_tool_calls,
            total_tokens_limit=self.max_tokens,
        )


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class PriceTable(BaseModel):
    """Pinned USD per million tokens, keyed by reported or requested model name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prices: Mapping[str, ModelPrice]

    def cost(
        self,
        model_name: str,
        usage: RequestUsage,
        *,
        requested: Model | None = None,
    ) -> float:
        requested_price = self.prices.get(requested.model_name) if requested else None
        if requested is not None and _is_openrouter_free(requested):
            # The requested free route determines billing, not the base model it reports.
            price = requested_price
            if price is None:
                return 0.0
        else:
            price = self.prices.get(model_name) or requested_price
        if price is None:
            raise UnpricedModel(f"no pinned price for model {model_name!r}")
        return (
            usage.input_tokens * price.input_per_mtok
            + usage.output_tokens * price.output_per_mtok
        ) / 1_000_000


def _is_openrouter_free(model: Model) -> bool:
    if model.model_name != "openrouter/free" and not model.model_name.endswith(":free"):
        return False
    url = urlsplit(str(model.base_url or ""))
    return url.scheme == "https" and url.hostname == "openrouter.ai"
