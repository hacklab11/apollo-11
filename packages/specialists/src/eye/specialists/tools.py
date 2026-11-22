"""Specialist domain tools: read-only HTTP calls to the core data API."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import ModelRetry, RunContext, Tool

from eye.contracts_v2 import Category, Channel, Country, Platform, SellerTier

# The data API returns up to 5 000 rows; the model only needs enough to reason.
MAX_ROWS = 200


Psp = Literal["PSP-1", "PSP-2"]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime = Field(description="Inclusive window start, ISO 8601 UTC")
    end: AwareDatetime = Field(
        description="Exclusive window end, at most 14 days later"
    )


class AdsPacingInput(Window):
    advertiser_id: str | None = Field(None, description="For example ADV-3")


class CategoryInput(Window):
    category: Category | None = None


class CountryInput(Window):
    country: Country | None = None


class OffersInput(Window):
    sku: str | None = None
    category: Category | None = None


class CapacityInput(Window):
    tier: SellerTier | None = None


class NewSellersInput(Window):
    since: AwareDatetime | None = Field(
        None, description="Joined at or after; defaults to start"
    )


class FeedExposureInput(Window):
    top_n: int = Field(20, description=f"Number of SKUs, 1 to {MAX_ROWS}")


class CheckoutFunnelInput(Window):
    country: Country | None = None
    platform: Platform | None = None


class PaymentsInput(Window):
    psp: Psp | None = None


class ErrorsInput(Window):
    platform: Platform | None = None
    page: str | None = Field(None, description="For example checkout")


class ChangesInput(Window):
    system: str | None = Field(None, description="For example pacing-svc")
    kind: str | None = Field(None, description="For example release")


class MetricInput(Window):
    name: str = Field(description="Registry metric name, for example orders")
    country: Country | None = None
    platform: Platform | None = None
    category: Category | None = None
    seller_tier: SellerTier | None = None
    channel: Channel | None = None
    psp: Psp | None = None
    advertiser_id: str | None = None


class DataResult(BaseModel):
    endpoint: str
    rows: list[dict[str, Any]]
    total_rows: int
    summary: str = Field(max_length=300)


class DataApiUnavailable(Exception):
    """The core data API cannot be reached."""


class DataClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def get(self, endpoint: str, params: dict[str, Any]) -> DataResult:
        response = await self.http.get(f"{self.base_url}/{endpoint}", params=params)
        if response.status_code == 422:
            raise ModelRetry(f"/data/{endpoint} returned 422: {response.text}")
        if response.status_code >= 400:
            raise ValueError(
                f"/data/{endpoint} returned {response.status_code}: "
                f"{response.text[:200]}"
            )
        rows = response.json()["rows"]
        shown = rows[:MAX_ROWS]
        summary = f"/data/{endpoint}: {len(rows)} rows"
        if len(rows) > MAX_ROWS:
            summary += f", only the first {MAX_ROWS} shown"
            if "ts" in shown[-1]:
                summary += f" (up to ts {shown[-1]['ts']})"
            summary += "; narrow the window or add a filter to see the rest"
        return DataResult(
            endpoint=endpoint,
            rows=shown,
            total_rows=len(rows),
            summary=summary + ".",
        )

    async def check(self, start: AwareDatetime, end: AwareDatetime) -> None:
        """Fail fast when core is down instead of letting the model retry."""
        try:
            response = await self.http.get(
                f"{self.base_url}/app_components",
                params={"start": start.isoformat(), "end": end.isoformat()},
            )
        except httpx.HTTPError as exc:
            raise DataApiUnavailable(
                f"core data API unavailable at {self.base_url}: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 500:
            raise DataApiUnavailable(
                f"core data API unavailable at {self.base_url}: "
                f"HTTP {response.status_code}"
            )


def _params(args: BaseModel) -> dict[str, Any]:
    return args.model_dump(mode="json", exclude_none=True)


@dataclass
class Evidence:
    steps: list[dict[str, str]] = field(default_factory=list)
    used: set[str] = field(default_factory=set)


def _tool(
    name: str,
    description: str,
    input_model: type[Window],
    handler: Callable[[BaseModel], Awaitable[DataResult]],
) -> Tool[Evidence]:
    async def call(ctx: RunContext[Evidence], **kwargs: Any) -> DataResult:
        try:
            args = input_model.model_validate(kwargs)
        except ValidationError as exc:
            raise ModelRetry(str(exc)) from exc
        result = await handler(args)
        ctx.deps.steps.append({"tool": name, "summary": result.summary})
        ctx.deps.used.add(result.endpoint)
        return result

    return Tool.from_schema(
        call,
        name=name,
        description=description,
        json_schema=input_model.model_json_schema(),
        takes_ctx=True,
    )


def data_tool(
    client: DataClient,
    name: str,
    endpoint: str,
    input_model: type[Window],
    description: str,
) -> Tool[Evidence]:
    async def call(args: BaseModel) -> DataResult:
        return await client.get(endpoint, _params(args))

    return _tool(name, description, input_model, call)


def metric_tool(client: DataClient) -> Tool[Evidence]:
    async def call(args: BaseModel) -> DataResult:
        params = _params(args)
        segment = {
            key: params.pop(key)
            for key in (
                "country",
                "platform",
                "category",
                "seller_tier",
                "channel",
                "psp",
                "advertiser_id",
            )
            if key in params
        }
        params["segment"] = json.dumps(segment, sort_keys=True)
        return await client.get("metric", params)

    return _tool(
        "metric",
        "Hourly actual vs expected values and z-scores for one registry metric in "
        "one segment. Use it to quantify the metric the question is about and a "
        "control segment.",
        MetricInput,
        call,
    )


def changes_tool(client: DataClient) -> Tool[Evidence]:
    return data_tool(
        client,
        "changes",
        "changes",
        ChangesInput,
        "Change log entries (releases, configs, campaigns) and external provider "
        "status in the window. Use it to line up timing with a change.",
    )
