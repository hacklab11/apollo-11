"""Typed, skill-routed A2A calls across the process boundary."""

import logging
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.errors import AgentCardResolutionError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types.a2a_pb2 import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.errors import A2AError
from authlib.integrations.base_client.errors import OAuthError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct, Value
from opentelemetry import propagate, trace
from pydantic import BaseModel

from eye.config import A2aSettings
from eye.contracts_v2 import (
    SELLER_REVERT_APPROVAL_METADATA_KEY,
    AgentResult,
    SellerAlert,
    SellerAlertAck,
    TaskMetadata,
)
from eye_auth.client import oauth_client
from eye_auth.trust import verify_card

logger = logging.getLogger(__name__)


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
        self.tracer = trace.get_tracer(__name__)

    async def resolve(self) -> None:
        cards: dict[str, AgentCard] = {}
        urls: dict[str, str] = {}
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.settings.timeout_s
        ) as client:
            for name in (
                "sentinel",
                "detective",
                "skeptic",
                "reporter",
                "cartographer",
            ):
                url = getattr(self.settings, f"{name}_url")
                try:
                    card = await A2ACardResolver(client, url).get_agent_card()
                    await verify_card(card, name)
                except (httpx.HTTPError, AgentCardResolutionError):
                    continue
                except ValueError:
                    logger.warning("Skipping untrusted A2A card for %s", name)
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
            ) as raw:
                card, url = self.cards[skill], self.urls[skill]
                name = card.name
                try:
                    await verify_card(card, name)
                except ValueError:
                    card = await A2ACardResolver(raw, url).get_agent_card()
                    await verify_card(card, name)
                    if skill not in {item.id for item in card.skills}:
                        raise ValueError(f"unknown A2A skill: {skill}")
                    self.cards[skill] = card
                async with oauth_client(
                    {f"{name}:{skill}"}, client_id="eye-core", http=raw
                ) as client:
                    response = await JsonRpcTransport(
                        client, card, url, is_shared_httpx=True
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

    async def notify_seller_os(
        self, alert: SellerAlert, *, approval_id: str | None = None
    ) -> SellerAlertAck:
        try:
            with self.tracer.start_as_current_span("agent.selleros"):
                headers = {"A2A-Version": "1.0"}
                propagate.inject(headers)
                async with (
                    AsyncExitStack() as stack,
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=self.settings.timeout_s,
                        headers=headers,
                    ) as client,
                ):
                    url = self.settings.selleros_url
                    card = await A2ACardResolver(client, url).get_agent_card()
                    await verify_card(card, "selleros")
                    authenticated = await stack.enter_async_context(
                        oauth_client(
                            {"selleros:receive_alert"},
                            client_id="eye-core",
                            http=client,
                        )
                    )
                    response = await JsonRpcTransport(
                        authenticated, card, url, is_shared_httpx=True
                    ).send_message(
                        SendMessageRequest(
                            message=Message(
                                message_id=uuid4().hex,
                                role=Role.ROLE_USER,
                                parts=[
                                    Part(
                                        data=ParseDict(
                                            alert.model_dump(mode="json"), Value()
                                        )
                                    ),
                                    Part(text=alert.summary),
                                ],
                            ),
                            metadata=(
                                ParseDict(
                                    {SELLER_REVERT_APPROVAL_METADATA_KEY: approval_id},
                                    Struct(),
                                )
                                if approval_id is not None
                                else None
                            ),
                        )
                    )
                task = response.task
                if (
                    not response.HasField("task")
                    or task.status.state != TaskState.TASK_STATE_COMPLETED
                    or len(task.artifacts) != 1
                    or len(task.artifacts[0].parts) != 1
                    or not task.artifacts[0].parts[0].HasField("data")
                ):
                    raise ValueError(
                        "expected a completed SellerOS task with one DataPart"
                    )
                return SellerAlertAck.model_validate(
                    MessageToDict(task.artifacts[0].parts[0].data)
                )
        except (A2AError, httpx.HTTPError, ValueError, KeyError, OAuthError):
            return SellerAlertAck(received=False, actions_taken=[])
