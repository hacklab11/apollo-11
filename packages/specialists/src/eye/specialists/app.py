"""One ASGI app serving the Growth, Marketplace, and Discovery A2A specialists."""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from types import ModuleType
from typing import Any

import httpx
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
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from google.protobuf.json_format import MessageToDict
from pydantic_ai import Agent, RunContext, Tool, ToolOutput
from pydantic_ai.models import Model
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from agent_runtime import RunLimits, RunScope, end_state_outputs, run_agent
from agent_runtime.configured_models import build_model, price_table
from agent_runtime.tracing import instrument_app, setup_telemetry
from eye.config import Settings, load_settings
from eye.contracts import SpecialistRequest, SpecialistResponse
from eye.specialists import discovery, growth, marketplace
from eye.specialists.tools import DataApiUnavailable, DataClient, DataResult, Evidence

from eye_auth.client import sign_card
from eye_auth.trust import A2AAuthMiddleware, SkillGuard, granted_scopes

SPECIALISTS: tuple[ModuleType, ...] = (growth, marketplace, discovery)
TOOL_SKILLS = {
    "growth": {
        "ads_pacing": "ads_pacing",
        "ads_auction": "auction_health",
        "marketing": "traffic_sources",
        "discounts": "discounts",
    },
    "marketplace": {
        "offers": "pricing",
        "buybox": "buybox",
        "stock": "availability",
        "purchase_orders": "purchasing",
        "capacity": "availability",
        "new_sellers": "new_sellers",
    },
    "discovery": {
        "search_quality": "search_quality",
        "feed_exposure": "feed_exposure",
        "checkout_funnel": "checkout_funnel",
        "payments": "checkout_funnel",
        "errors": "client_errors",
        "app_components": "components",
    },
}


def authorized_tools(
    specialist: ModuleType, client: DataClient, guard: SkillGuard | None
) -> list[Tool[Evidence]]:
    tools = specialist.tools(client)
    if guard is None:
        return tools

    def authorize(tool: Tool[Evidence]) -> Tool[Evidence]:
        async def invoke(ctx: RunContext[Evidence], **kwargs: Any) -> DataResult:
            skill = TOOL_SKILLS[specialist.NAME].get(tool.name)
            if skill is not None:
                guard.check(skill)
            elif tool.name not in {"metric", "changes"}:
                raise PermissionError("Unmapped specialist tool")
            return await tool.function(ctx, **kwargs)

        return Tool.from_schema(
            invoke,
            name=tool.name,
            description=tool.description,
            json_schema=tool.tool_def.parameters_json_schema,
            takes_ctx=True,
        )

    return [authorize(tool) for tool in tools]

async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


PROMPTS = files("eye.llm").joinpath("prompts")
MAX_STEPS = 8


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


async def answer(
    specialist: ModuleType,
    client: DataClient,
    request: SpecialistRequest,
    settings: Settings,
    scopes: frozenset[str] | None = None,
    *,
    model: Model | None = None,
) -> tuple[SpecialistResponse, list[dict[str, str]]]:
    """Run one specialist turn; returns the response and its `{tool, summary}` steps."""
    await client.check(request.window_start_ts, request.window_end_ts)
    evidence = Evidence()
    guard = SkillGuard(specialist.NAME, scopes) if scopes is not None else None
    agent = Agent(
        model or build_model(settings.llm, specialist.NAME, effort_key="specialists"),
        name=specialist.NAME,
        instructions=(PROMPTS / f"{specialist.NAME}.md").read_text(),
        deps_type=Evidence,
        tools=authorized_tools(specialist, client, guard),
        output_type=end_state_outputs(ToolOutput(SpecialistResponse, name="submit")),
        model_settings={"max_tokens": settings.llm.max_tokens},
    )
    content = json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True)
    outcome = await run_agent(
        agent,
        content,
        scope=RunScope(run_id=request.investigation_id, tenant_id="default"),
        limits=RunLimits(max_steps=MAX_STEPS),
        prices=price_table(settings.llm),
        deps=evidence,
    )
    if guard is not None:
        guard.finish()
    if outcome.result.status != "done":
        raise RuntimeError(f"{outcome.result.status}: {outcome.result.reason}")
    response = SpecialistResponse.model_validate(outcome.result.result).model_copy(
        update={"agent": specialist.NAME, "data_used": sorted(evidence.used)}
    )
    return response, evidence.steps


class SpecialistExecutor(AgentExecutor):
    def __init__(
        self, specialist: ModuleType, client: DataClient, settings: Settings
    ) -> None:
        self.specialist = specialist
        self.client = client
        self.settings = settings

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        name = self.specialist.NAME
        try:
            scopes = granted_scopes()
            if not any(f"{name}:{skill}" in scopes for skill in self.specialist.SKILLS):
                raise PermissionError("Insufficient specialist scope")
            parts = [part for part in context.message.parts if part.HasField("data")]
            if len(parts) != 1:
                raise ValueError("expected exactly one JSON data part")
            request = SpecialistRequest.model_validate(
                MessageToDict(parts[0].data), extra="forbid"
            )
            response, steps = await answer(
                self.specialist, self.client, request, self.settings, scopes
            )
        except DataApiUnavailable as exc:
            await self._fail(updater, f"{self.specialist.TITLE} failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 -- failures must terminate the A2A task
            await self._fail(
                updater, f"{self.specialist.TITLE} failed: {type(exc).__name__}"
            )
            return
        await updater.add_artifact(
            [new_data_part(response.model_dump(mode="json"))],
            name=f"{name}_response",
            metadata={"steps": steps},
        )
        await updater.complete()
        _log("specialist_completed", agent=name, findings=len(response.findings))

    async def _fail(self, updater: TaskUpdater, message: str) -> None:
        await updater.failed(new_text_message(message))
        _log("specialist_failed", agent=self.specialist.NAME, error=message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def agent_card(specialist: ModuleType, url: str) -> AgentCard:
    return AgentCard(
        name=specialist.NAME,
        description=specialist.DESCRIPTION,
        version="0.1.0",
        capabilities=AgentCapabilities(),
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(id=skill, name=skill, description=description, tags=[skill])
            for skill, description in specialist.SKILLS.items()
        ],
    )


def specialist_routes(
    specialist: ModuleType,
    client: DataClient,
    settings: Settings,
    cards: list[AgentCard],
) -> list[BaseRoute]:
    # Prefixed routes, not Mount: clients POST to /growth with no trailing slash.
    prefix = f"/{specialist.NAME}"
    card = agent_card(specialist, getattr(settings.a2a, f"{specialist.NAME}_url"))
    cards.append(card)
    handler = DefaultRequestHandler(
        SpecialistExecutor(specialist, client, settings), InMemoryTaskStore(), card
    )
    return [
        *create_agent_card_routes(card, card_url=prefix + AGENT_CARD_WELL_KNOWN_PATH),
        *create_jsonrpc_routes(handler, prefix),
    ]


def create_app(
    settings: Settings | None = None, client: DataClient | None = None
) -> Starlette:
    settings = settings or load_settings()
    owned = client is None
    if client is None:
        client = DataClient(
            httpx.AsyncClient(timeout=15.0),
            os.environ.get("EYE_CORE_URL", f"http://127.0.0.1:{settings.ports.core}")
            + "/data",
        )
    cards: list[AgentCard] = []

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        for card in cards:
            await sign_card(card, card.name)
        yield
        if owned:
            await client.http.aclose()

    return Starlette(
        routes=[Route("/healthz", healthz)]
        + [
            route
            for specialist in SPECIALISTS
            for route in specialist_routes(specialist, client, settings, cards)
        ],
        lifespan=lifespan,
        middleware=[
            Middleware(
                A2AAuthMiddleware,
                services={f"/{s.NAME}": (s.NAME, set()) for s in SPECIALISTS},
            )
        ],
    )


def main() -> None:
    setup_telemetry()
    settings = load_settings()
    uvicorn.run(
        instrument_app(create_app(settings)),
        host=os.environ.get("EYE_BIND_HOST", "127.0.0.1"),
        port=settings.ports.specialists,
        access_log=False,
        log_config=None,
    )
