"""Typed, skill-routed A2A calls across the process boundary."""

from uuid import uuid4

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.errors import AgentCardResolutionError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types.a2a_pb2 import AgentCard, Message, Part, Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct, Value
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel

from eye.config import A2aSettings
from eye.contracts_v2 import AgentResult, TaskMetadata


class AgentDirectory:
    def __init__(
        self,
        settings: A2aSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.cards: dict[str, AgentCard] = {}
        self.urls: dict[str, str] = {}
        self.provider = TracerProvider()
        self.tracer = self.provider.get_tracer(__name__)

    async def resolve(self) -> None:
        cards: dict[str, AgentCard] = {}
        urls: dict[str, str] = {}
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.settings.timeout_s
        ) as client:
            for name in ("sentinel", "detective", "reporter", "cartographer"):
                url = getattr(self.settings, f"{name}_url")
                try:
                    card = await A2ACardResolver(client, url).get_agent_card()
                except (httpx.HTTPError, AgentCardResolutionError):
                    continue
                for skill in card.skills:
                    if skill.id in cards:
                        raise ValueError(f"duplicate A2A skill: {skill.id}")
                    cards[skill.id], urls[skill.id] = card, url
        self.cards, self.urls = cards, urls

    async def call[T: BaseModel](
        self,
        skill: str,
        payload: BaseModel,
        metadata: TaskMetadata,
        result_type: type[T],
    ) -> AgentResult[T]:
        if skill not in self.cards:
            await self.resolve()
        if skill not in self.cards:
            raise ValueError(f"unknown A2A skill: {skill}")
        parent = propagate.extract({"traceparent": metadata.traceparent})
        with self.tracer.start_as_current_span(
            f"agent.{skill}", context=parent, kind=trace.SpanKind.CLIENT
        ):
            headers: dict[str, str] = {"A2A-Version": "1.0"}
            propagate.inject(headers)
            metadata = metadata.model_copy(
                update={"traceparent": headers["traceparent"]}
            )
            request = SendMessageRequest(
                message=Message(
                    message_id=uuid4().hex,
                    context_id=metadata.context_id,
                    role=Role.ROLE_USER,
                    parts=[
                        Part(data=ParseDict(payload.model_dump(mode="json"), Value()))
                    ],
                ),
                metadata=ParseDict(metadata.model_dump(mode="json"), Struct()),
            )
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.settings.timeout_s,
                headers=headers,
            ) as client:
                response = await JsonRpcTransport(
                    client, self.cards[skill], self.urls[skill], is_shared_httpx=True
                ).send_message(request)
            if not response.HasField("task"):
                raise ValueError("expected blocking A2A task")
            task = response.task
            if (
                len(task.artifacts) != 1
                or len(task.artifacts[0].parts) != 1
                or not task.artifacts[0].parts[0].HasField("data")
            ):
                raise ValueError("expected one artifact containing one DataPart")
            return AgentResult[result_type].model_validate(
                MessageToDict(task.artifacts[0].parts[0].data)
            )
