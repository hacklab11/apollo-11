"""SellerOS assistant with a request-local privacy boundary."""

import asyncio
import re
from pathlib import Path

from selleros.client import get_client
from selleros.data import ScopedData, Selection, SellerStore, ToolOutput
from selleros.models import (
    SpecialistRequest,
    SpecialistResponse,
    TrailStep,
)
from selleros.runtime import AgentSpec, ToolSpec, run_agent


def scope_for(request: SpecialistRequest) -> Selection:
    text = (
        request.question
        + " "
        + " ".join(
            value
            for key, value in request.segment.items()
            if key in {"seller_id", "seller_ids", "sku", "skus"}
        )
    )
    return Selection(
        seller_ids=sorted(set(re.findall(r"\bS-\d+\b", text))),
        skus=sorted(set(re.findall(r"\b(?:E|H|F|T)\d{2}\b", text))),
    )


async def answer(store: SellerStore, request: SpecialistRequest) -> SpecialistResponse:
    snapshot = await asyncio.to_thread(store.read)
    scope = scope_for(request)
    if not scope.seller_ids and not scope.skus:
        return SpecialistResponse(
            agent="selleros",
            answered=False,
            findings=[],
            suggested_next=["Name seller IDs or SKUs to authorize per-seller facts."],
        )
    data = ScopedData(store, snapshot, scope, request.window_end_ts)
    tools = []
    for name in (
        "get_repricer",
        "get_costs",
        "get_price_history",
        "get_supplier_eta",
        "price_intent_check",
    ):

        def query(selection: Selection, name: str = name) -> ToolOutput:
            return data.query(name, selection)

        tools.append(
            ToolSpec(
                name,
                f"Read scoped seller data: {name}. Costs include deterministic margin per unit. Price intent includes the last change, unit cost and computed verdict.",
                Selection,
                query,
            )
        )

    async def on_step(step: TrailStep) -> None:
        return None

    result = await run_agent(
        AgentSpec(
            name="selleros",
            system_prompt_file=str(Path(__file__).with_name("prompt.md")),
            tools=tools,
            submit_model=SpecialistResponse,
            effort="medium",
            model=get_client().settings.model,
            max_steps=6,
        ),
        request.model_dump_json(),
        request.investigation_id,
        on_step,
    )
    response = SpecialistResponse.model_validate(result.model_dump())
    allowed = {seller.seller_id for seller in data.sellers}
    named = set(re.findall(r"\bS-\d+\b", response.model_dump_json()))
    if not named <= allowed:
        return SpecialistResponse(agent="selleros", answered=False, findings=[])
    response.agent = "selleros"
    return response
