"""Offline SDK A2A agents with typed results and captured wire requests."""

import json
from typing import Any
from uuid import uuid4

import httpx
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.applications import Starlette

from eye.config import A2aSettings
from eye.contracts_v2 import AgentResult, DetectResult, ReportResult
from eye.directory import AgentDirectory


class ScriptedExecutor(AgentExecutor):
    def __init__(self, result: AgentResult) -> None:
        self.result = result

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                artifacts=[
                    Artifact(
                        artifact_id=uuid4().hex,
                        parts=[
                            Part(
                                data=ParseDict(
                                    self.result.model_dump(mode="json"), Value()
                                )
                            )
                        ],
                    )
                ],
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


class ScriptedAgents:
    def __init__(self, results: dict[str, AgentResult] | None = None) -> None:
        defaults = {
            "detect": AgentResult(status="done", result=DetectResult(decisions=[])),
            "report": AgentResult(
                status="done", result=ReportResult(title="Report", summary="Summary")
            ),
        }
        defaults.update(results or {})
        self.requests: list[tuple[str, dict[str, Any], str]] = []
        self.executors: dict[str, ScriptedExecutor] = {}
        self.apps = {}
        for name, skill in (
            ("sentinel", "detect"),
            ("detective", "investigate"),
            ("reporter", "report"),
            ("cartographer", "map"),
        ):
            executor = ScriptedExecutor(
                defaults.get(skill, AgentResult(status="failed", reason="not scripted"))
            )
            self.executors[skill] = executor
            card = AgentCard(
                name=name,
                description="Scripted offline agent",
                version="1.0",
                capabilities=AgentCapabilities(),
                supported_interfaces=[
                    AgentInterface(
                        url=f"http://{name}/",
                        protocol_binding="JSONRPC",
                        protocol_version="1.0",
                    )
                ],
                default_input_modes=["application/json"],
                default_output_modes=["application/json"],
                skills=[
                    AgentSkill(
                        id=skill, name=skill, description="Scripted", tags=["test"]
                    )
                ],
            )
            handler = DefaultRequestHandler(
                agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
            )
            self.apps[name] = Starlette(
                routes=[
                    *create_agent_card_routes(card),
                    *create_jsonrpc_routes(handler, rpc_url="/"),
                ]
            )
        self.transport = httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:

        if request.method == "POST":
            self.requests.append(
                (
                    request.url.host,
                    json.loads(request.content),
                    request.headers["traceparent"],
                )
            )
        app = self.apps[request.url.host]
        return await httpx.ASGITransport(app=app).handle_async_request(request)

    def directory(self, settings: A2aSettings) -> AgentDirectory:
        return AgentDirectory(settings, transport=self.transport)
