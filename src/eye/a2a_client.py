"""Core-side, non-streaming A2A communication with bounded failures."""

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.types.a2a_pb2 import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.errors import A2AError
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from google.protobuf.struct_pb2 import Value
from pydantic import BaseModel, Field

from eye.config import A2aSettings
from eye.contracts import (
    SellerAlert,
    SellerAlertAck,
    SpecialistRequest,
    SpecialistResponse,
)

_FAILURES = (
    A2AError,
    httpx.HTTPError,
    TimeoutError,
    ValueError,
    TypeError,
    KeyError,
    ParseError,
)


class AgentCardInfo(BaseModel):
    name: str
    url: str
    skills: list[str] = Field(default_factory=list)
    healthy: bool
    reason: str | None = None


class A2AClient:
    def __init__(
        self,
        settings: A2aSettings | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        on_unavailable: Callable[[str, str], None] | None = None,
    ) -> None:
        self.settings = settings or A2aSettings()
        self._http = http_client or httpx.AsyncClient(timeout=self.settings.timeout_s)
        self._owns_http = http_client is None
        self._factory = ClientFactory(
            ClientConfig(streaming=False, httpx_client=self._http)
        )
        self._urls = {
            name: getattr(self.settings, f"{name}_url")
            for name in ("growth", "marketplace", "discovery", "selleros")
        }
        self._cards: dict[str, AgentCard] = {}
        self._health: dict[str, AgentCardInfo] = {}
        self._checked: dict[str, float] = {}
        self._on_unavailable = on_unavailable

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _unavailable(self, agent: str, exc: Exception) -> str:
        # Exception text can contain remote payloads; only expose a safe reason.
        reason = f"agent unavailable: {type(exc).__name__}"
        if agent in self._urls:
            self._health[agent] = AgentCardInfo(
                name=agent, url=self._urls[agent], healthy=False, reason=reason
            )
        if self._on_unavailable:
            self._on_unavailable(agent, reason)
        return reason

    async def _card(self, agent: str, *, refresh: bool = False) -> AgentCard:
        if refresh or monotonic() - self._checked.get(agent, -float("inf")) >= 60:
            card = await A2ACardResolver(self._http, self._urls[agent]).get_agent_card()
            self._cards[agent] = card
            self._checked[agent] = monotonic()
            self._health[agent] = AgentCardInfo(
                name=card.name,
                url=self._urls[agent],
                skills=[skill.id for skill in card.skills],
                healthy=True,
            )
        return self._cards[agent]

    async def fetch_cards(self) -> dict[str, AgentCardInfo]:
        async def fetch(agent: str) -> None:
            try:
                async with asyncio.timeout(self.settings.timeout_s):
                    await self._card(agent, refresh=True)
            except _FAILURES as exc:
                self._cards.pop(agent, None)
                self._checked.pop(agent, None)
                self._unavailable(agent, exc)

        await asyncio.gather(*(fetch(agent) for agent in self._urls))
        return {name: info.model_copy(deep=True) for name, info in self._health.items()}

    async def _send(self, agent: str, payload: BaseModel, text: str) -> dict[str, Any]:
        async with asyncio.timeout(self.settings.timeout_s):
            card = await self._card(agent)
            data = ParseDict(payload.model_dump(mode="json"), Value())
            request = SendMessageRequest(
                message=Message(
                    message_id=uuid4().hex,
                    role=Role.ROLE_USER,
                    parts=[Part(data=data), Part(text=text)],
                )
            )
            async with self._factory.create(card) as client:
                async for event in client.send_message(request):
                    if not event.HasField("task"):
                        raise ValueError("expected a completed task")
                    task = event.task
                    if task.status.state != TaskState.TASK_STATE_COMPLETED:
                        raise ValueError("task did not complete")
                    if len(task.artifacts) != 1 or len(task.artifacts[0].parts) != 1:
                        raise ValueError("expected one artifact with one data part")
                    part = task.artifacts[0].parts[0]
                    if not part.HasField("data"):
                        raise ValueError("expected a data part")
                    result = MessageToDict(part.data)
                    if not isinstance(result, dict):
                        raise TypeError("expected an object")
                    return result
        raise ValueError("empty response")

    async def ask(self, agent: str, req: SpecialistRequest) -> SpecialistResponse:
        try:
            req = SpecialistRequest.model_validate(req)
            result = SpecialistResponse.model_validate(
                await self._send(agent, req, req.question)
            )
            if result.agent != agent:
                raise ValueError("response agent mismatch")
            self._health[agent].healthy = True
            self._health[agent].reason = None
            return result
        except _FAILURES as exc:
            self._unavailable(agent, exc)
            return SpecialistResponse(agent=agent, answered=False, findings=[])

    async def notify_seller_os(self, alert: SellerAlert) -> SellerAlertAck:
        try:
            alert = SellerAlert.model_validate(alert)
            result = SellerAlertAck.model_validate(
                await self._send("selleros", alert, alert.summary)
            )
            self._health["selleros"].healthy = True
            self._health["selleros"].reason = None
            return result
        except _FAILURES as exc:
            self._unavailable("selleros", exc)
            return SellerAlertAck(received=False, actions_taken=[])
