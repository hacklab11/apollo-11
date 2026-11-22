"""SellerOS assistant with a request-local privacy boundary."""

import asyncio
import re
from pathlib import Path
from uuid import uuid4

from pydantic_ai import Agent, RunContext, Tool, ToolOutput
from pydantic_ai.framing import register_tag, wrap
from pydantic_ai.models import Model

from agent_runtime import PriceTable, RunLimits, RunScope, end_state_outputs, run_agent
from selleros.client import PRICES, LlmSettings, build_model
from selleros.data import ScopedData, Selection, SellerStore
from selleros.data import ToolOutput as ReadResult
from selleros.models import SpecialistRequest, SpecialistResponse

LIMITS = RunLimits(max_steps=6, max_tool_calls=20, max_tokens=16_000)
PROMPT = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")
REQUEST_TAG = register_tag("selleros_request")
READ_TOOLS = {
    "get_repricer": "Read scoped repricer mode, floor and status.",
    "get_costs": "Read scoped costs and deterministic margins per unit.",
    "get_price_history": "Read scoped price history for the last 24 hours.",
    "get_supplier_eta": "Read scoped expected seller restock dates.",
    "price_intent_check": "Read the scoped last price change, unit cost and computed verdict.",
}


def read_tool(name: str, description: str) -> Tool[ScopedData]:
    def query(ctx: RunContext[ScopedData], selection: Selection) -> ReadResult:
        return ctx.deps.query(name, selection)

    return Tool(query, name=name, description=description)


def scope_for(request: SpecialistRequest) -> Selection:
    text = " ".join(
        [
            request.question,
            *(
                value
                for key, value in request.segment.items()
                if key in {"seller_id", "seller_ids", "sku", "skus"}
            ),
        ]
    )
    return Selection(
        seller_ids=sorted(set(re.findall(r"\bS-\d+\b", text))),
        skus=sorted(set(re.findall(r"\b(?:E|H|F|T)\d{2}\b", text))),
    )


async def answer(
    store: SellerStore,
    request: SpecialistRequest,
    *,
    model: Model | None = None,
    prices: PriceTable = PRICES,
) -> SpecialistResponse:
    scope = scope_for(request)
    if not scope.seller_ids and not scope.skus:
        return SpecialistResponse(
            agent="selleros",
            answered=False,
            findings=[],
            suggested_next=["Name seller IDs or SKUs to authorize per-seller facts."],
        )
    snapshot = await asyncio.to_thread(store.read)
    data = ScopedData(store, snapshot, scope, request.window_end_ts)
    agent = Agent(
        model or build_model(LlmSettings.from_env()),
        name="selleros",
        instructions=PROMPT,
        deps_type=ScopedData,
        tools=[
            read_tool(name, description) for name, description in READ_TOOLS.items()
        ],
        output_type=end_state_outputs(
            ToolOutput(SpecialistResponse, name="submit", strict=False)
        ),
        model_settings={"max_tokens": LIMITS.max_tokens},
    )
    outcome = await run_agent(
        agent,
        wrap(REQUEST_TAG, request.model_dump_json()),
        scope=RunScope(run_id=uuid4().hex, tenant_id="default"),
        limits=LIMITS,
        prices=prices,
        deps=data,
    )
    result = outcome.result
    if result.status == "needs_input":
        return SpecialistResponse(agent="selleros", answered=False, findings=[])
    if result.status != "done" or not isinstance(result.result, SpecialistResponse):
        raise RuntimeError("SellerOS could not complete the answer")
    response = result.result
    allowed = {seller.seller_id for seller in data.sellers}
    named = set(re.findall(r"\bS-\d+\b", response.model_dump_json()))
    if not named <= allowed:
        return SpecialistResponse(agent="selleros", answered=False, findings=[])
    response.agent = "selleros"
    return response
