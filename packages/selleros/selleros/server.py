"""Standalone A2A JSON-RPC service."""

import asyncio
import os

import uvicorn
from a2a.helpers.proto_helpers import (
    new_data_part,
    new_task_from_user_message,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from selleros.agent import answer
from selleros.data import FileSellerStore, SellerStore, receive_alert
from selleros.models import SellerAlert, SpecialistRequest

SKILLS = {
    "repricer_status": "Repricer mode, floor and last 24 hour price path",
    "below_cost_check": "Current prices below unit cost and margin per unit",
    "supplier_eta": "Expected seller restock dates",
    "price_intent_check": "Last price change, source, unit cost and deterministic price intent verdict",
    "receive_alert": "Pause no-floor repricers or revert likely erroneous manual prices in response to alerts",
}


class Executor(AgentExecutor):
    def __init__(self, store: SellerStore) -> None:
        self.store = store

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        try:
            parts = [part for part in context.message.parts if part.HasField("data")]
            if len(parts) != 1:
                raise ValueError("expected exactly one JSON data part")
            payload = MessageToDict(parts[0].data)
            if "incident_id" in payload:
                alert = SellerAlert.model_validate(payload, extra="forbid")
                output = await asyncio.to_thread(receive_alert, self.store, alert)
            else:
                request = SpecialistRequest.model_validate(payload, extra="forbid")
                output = await answer(self.store, request)
            await updater.add_artifact([new_data_part(output.model_dump(mode="json"))])
            await updater.complete()
        except Exception as exc:  # noqa: BLE001 -- failures must terminate the A2A task
            await updater.failed(
                new_text_message(f"SellerOS request failed: {type(exc).__name__}")
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(store: SellerStore | None = None) -> Starlette:
    card = AgentCard(
        name="SellerOS Partner Agent",
        description="Third-party repricing and inventory assistant for small marketplace sellers",
        version="0.1.0",
        capabilities=AgentCapabilities(),
        supported_interfaces=[
            AgentInterface(
                url=os.environ.get("SELLEROS_URL", "http://127.0.0.1:8300/"),
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(id=name, name=name, description=description, tags=[name])
            for name, description in SKILLS.items()
        ],
    )
    handler = DefaultRequestHandler(
        Executor(store if store is not None else FileSellerStore()),
        InMemoryTaskStore(),
        card,
    )
    return Starlette(
        routes=[
            Route("/healthz", healthz),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=True),
        ]
    )


def main() -> None:
    uvicorn.run(
        create_app(),
        host=os.environ.get("EYE_BIND_HOST", "127.0.0.1"),
        port=8300,
        access_log=False,
        log_config=None,
    )
