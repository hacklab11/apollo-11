"""SellerOS assistant with a request-local privacy boundary."""

import asyncio
import re
from pathlib import Path

from selleros.data import ScopedData, Selection, Store, ToolOutput
from selleros.models import SellerAlert, SpecialistRequest, SpecialistResponse, TrailStep
from selleros.runtime import AgentSpec, ToolSpec, run_agent


def scope_for(request: SpecialistRequest) -> Selection:
    text = request.question + " " + " ".join(
        value for key, value in request.segment.items()
        if key in {"seller_id", "seller_ids", "sku", "skus"}
    )
    return Selection(
        seller_ids=sorted(set(re.findall(r"\bS-\d+\b", text))),
        skus=sorted(set(re.findall(r"\b(?:E|H|F|T)\d{2}\b", text))),
    )


async def answer(store: Store, request: SpecialistRequest) -> SpecialistResponse:
    snapshot = await asyncio.to_thread(store.read)
    scope = scope_for(request)
    if not scope.seller_ids and not scope.skus:
        return SpecialistResponse(agent="selleros", answered=False, findings=[],
            suggested_next=["Name seller IDs or SKUs to authorize per-seller facts."])
    data = ScopedData(store, snapshot, scope, request.window_end_ts)
    tools = []
    for name in ("get_repricer", "get_costs", "get_price_history", "get_supplier_eta"):
        def query(selection: Selection, name: str = name) -> ToolOutput:
            return data.query(name, selection)
        tools.append(ToolSpec(name, f"Read scoped seller data: {name}. Costs include deterministic margin per unit.", Selection, query))

    def pause(selection: Selection) -> ToolOutput:
        allowed = {seller.seller_id for seller in data.sellers}
        if not selection.seller_ids or not set(selection.seller_ids) <= allowed:
            raise ValueError("pause requires explicitly scoped seller IDs")
        ack = store.pause(snapshot, SellerAlert(
            incident_id=request.investigation_id,
            seller_ids=selection.seller_ids, skus=scope.skus,
            summary=request.question, recommended_action="pause",
        ))
        return ToolOutput(rows=[ack.model_dump()], summary=f"{len(ack.actions_taken)} repricers paused")

    tools.append(ToolSpec("pause_repricer", "Pause no-floor repricers only when explicitly requested.", Selection, pause))

    async def on_step(step: TrailStep) -> None:
        return None

    result = await run_agent(AgentSpec(
        name="selleros", system_prompt_file=str(Path(__file__).with_name("prompt.md")),
        tools=tools, submit_model=SpecialistResponse, effort="medium",
        model="claude-opus-5-5", max_steps=6,
    ), request.model_dump_json(), request.investigation_id, on_step)
    response = SpecialistResponse.model_validate(result.model_dump())
    allowed = {seller.seller_id for seller in data.sellers}
    named = set(re.findall(r"\bS-\d+\b", response.model_dump_json()))
    if not named <= allowed:
        return SpecialistResponse(agent="selleros", answered=False, findings=[])
    response.agent = "selleros"
    return response
