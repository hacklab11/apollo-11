"""One ASGI app serving the Growth, Marketplace, and Discovery A2A specialists."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
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
from starlette.applications import Starlette
from starlette.routing import BaseRoute

from eye.config import Settings, load_settings
from eye.contracts import SpecialistRequest, SpecialistResponse, TrailStep
from eye.llm.runtime import AgentSpec, run_agent
from eye.specialists import discovery, growth, marketplace
from eye.specialists.tools import DataApiUnavailable, DataClient

SPECIALISTS: tuple[ModuleType, ...] = (growth, marketplace, discovery)
PROMPTS = Path(__file__).resolve().parents[1] / "llm" / "prompts"
MAX_STEPS = 8


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


async def answer(
    specialist: ModuleType,
    client: DataClient,
    request: SpecialistRequest,
    settings: Settings,
) -> tuple[SpecialistResponse, list[dict[str, str]]]:
    """Run one specialist turn; returns the response and its `{tool, summary}` steps."""
    await client.check(request.window_start_ts, request.window_end_ts)
    steps: list[dict[str, str]] = []
    used: set[str] = set()

    async def on_step(step: TrailStep) -> None:
        if step.action == "tool_call" and step.tool != "submit":
            steps.append({"tool": step.tool or "", "summary": step.output_summary})
            if step.output is not None:
                used.add(step.output["endpoint"])

    spec = AgentSpec(
        name=specialist.NAME,
        system_prompt_file=str(PROMPTS / f"{specialist.NAME}.md"),
        tools=specialist.tools(client),
        submit_model=SpecialistResponse,
        effort=settings.llm.effort["specialists"],
        model=settings.llm.model_for(specialist.NAME),
        max_steps=MAX_STEPS,
        max_tokens=settings.llm.max_tokens,
    )
    # Protobuf Struct does not preserve key order; sorting keeps replay keys stable.
    content = json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True)
    result = await run_agent(spec, content, request.investigation_id, on_step)
    response = SpecialistResponse.model_validate(result.model_dump()).model_copy(
        update={"agent": specialist.NAME, "data_used": sorted(used)}
    )
    return response, steps


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
            parts = [part for part in context.message.parts if part.HasField("data")]
            if len(parts) != 1:
                raise ValueError("expected exactly one JSON data part")
            request = SpecialistRequest.model_validate(
                MessageToDict(parts[0].data), extra="forbid"
            )
            response, steps = await answer(
                self.specialist, self.client, request, self.settings
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
        name=specialist.TITLE,
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
    specialist: ModuleType, client: DataClient, settings: Settings
) -> list[BaseRoute]:
    # Prefixed routes, not Mount: clients POST to /growth with no trailing slash.
    prefix = f"/{specialist.NAME}"
    card = agent_card(specialist, getattr(settings.a2a, f"{specialist.NAME}_url"))
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
            f"http://127.0.0.1:{settings.ports.core}/data",
        )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        if owned:
            await client.http.aclose()

    return Starlette(
        routes=[
            route
            for specialist in SPECIALISTS
            for route in specialist_routes(specialist, client, settings)
        ],
        lifespan=lifespan,
    )


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=settings.ports.specialists,
        access_log=False,
        log_config=None,
    )
