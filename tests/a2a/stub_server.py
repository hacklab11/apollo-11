"""An SDK JSON-RPC server used by the core client tests."""

from typing import Any
from uuid import uuid4

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
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.applications import Starlette

from eye.contracts_v2 import (
    SellerAlert,
    SellerAlertAck,
    SpecialistRequest,
    SpecialistResponse,
)


class EchoExecutor(AgentExecutor):
    def __init__(
        self, response: dict[str, Any] | None = None, *, failed: bool = False
    ) -> None:
        self.response = response
        self.failed = failed
        self.requests: list[SpecialistRequest | SellerAlert] = []
        self.texts: list[str] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.message is not None
        assert len(context.message.parts) == 2
        payload = MessageToDict(context.message.parts[0].data)
        self.texts.append(context.message.parts[1].text)
        if "question" in payload:
            req = SpecialistRequest.model_validate(payload)
            self.requests.append(req)
            result = SpecialistResponse(agent="growth", answered=True, findings=[])
        else:
            self.requests.append(SellerAlert.model_validate(payload))
            result = SellerAlertAck(received=True, actions_taken=["review scheduled"])
        data = (
            result.model_dump(mode="json") if self.response is None else self.response
        )
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_FAILED
                    if self.failed
                    else TaskState.TASK_STATE_COMPLETED
                ),
                artifacts=[
                    Artifact(
                        artifact_id=uuid4().hex,
                        parts=[Part(data=ParseDict(data, Value()))],
                    )
                ],
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def stub_server(
    executor: EchoExecutor | None = None, *, name: str = "growth"
) -> tuple[Starlette, EchoExecutor]:
    executor = executor or EchoExecutor()
    card = AgentCard(
        name=name,
        description="Fixed specialist response",
        version="0.1.0",
        capabilities=AgentCapabilities(),
        supported_interfaces=[
            AgentInterface(
                url=f"http://{name}/",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(
                id="echo", name="Echo", description="Fixed response", tags=["test"]
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
    )
    app = Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/"),
        ]
    )
    return app, executor
