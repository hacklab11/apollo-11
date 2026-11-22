"""Authlib client-credentials sessions and startup card enrollment."""

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx
from a2a.types import AgentCard
from authlib.integrations.httpx_client import AsyncOAuth2Client, OAuth2Client
from google.protobuf.json_format import MessageToDict, ParseDict

from eye_auth.trust import issuer, verify_card


class _OAuth2Client(AsyncOAuth2Client):
    async def send(
        self,
        request: httpx.Request,
        *,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        **kwargs: Any,
    ) -> httpx.Response:
        # A2A sends prebuilt requests, bypassing Authlib's request() override.
        if auth is httpx.USE_CLIENT_DEFAULT:
            await self.ensure_active_token(self.token)
            auth = self.token_auth
        return await super().send(request, auth=auth, **kwargs)


class _CallerTransport(httpx.AsyncBaseTransport):
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.auth = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = httpx.URL(issuer())
        if (request.url.scheme, request.url.host, request.url.port) == (
            target.scheme,
            target.host,
            target.port,
        ):
            return await self.auth.handle_async_request(request)
        return await self.http.send(request)

    async def aclose(self) -> None:
        await self.auth.aclose()


def credentials(client_id: str | None = None) -> tuple[str, str]:
    client_id = client_id or os.environ["EYE_AUTH_CLIENT_ID"]
    secret = os.environ.get(f"EYE_AUTH_{client_id.upper().replace('-', '_')}_SECRET")
    return client_id, secret or os.environ["EYE_AUTH_CLIENT_SECRET"]


@asynccontextmanager
async def oauth_client(
    scopes: set[str],
    *,
    client_id: str | None = None,
    http: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> AsyncIterator[AsyncOAuth2Client]:
    client_id, secret = credentials(client_id)
    if http is not None:
        kwargs.setdefault("timeout", http.timeout)
        kwargs["transport"] = _CallerTransport(http)
        kwargs["headers"] = {**http.headers, **kwargs.get("headers", {})}
    async with _OAuth2Client(
        client_id,
        secret,
        scope=" ".join(sorted(scopes)),
        token_endpoint=issuer() + "/oauth/token",
        grant_type="client_credentials",
        trust_env=False,
        **kwargs,
    ) as client:
        await client.fetch_token(issuer() + "/oauth/token")
        yield client


@contextmanager
def sync_client(
    scopes: set[str], *, client_id: str | None = None, **kwargs: Any
) -> Iterator[OAuth2Client]:
    client_id, secret = credentials(client_id)
    with OAuth2Client(
        client_id,
        secret,
        scope=" ".join(sorted(scopes)),
        token_endpoint=issuer() + "/oauth/token",
        grant_type="client_credentials",
        trust_env=False,
        **kwargs,
    ) as client:
        client.fetch_token(issuer() + "/oauth/token")
        yield client


async def sign_card(card: AgentCard, client_id: str) -> None:
    async with oauth_client({"auth:card"}, client_id=client_id) as client:
        response = await client.post(issuer() + "/cards/sign", json=MessageToDict(card))
        response.raise_for_status()
        signed = ParseDict(response.json(), AgentCard())
    await verify_card(signed, client_id)
    card.CopyFrom(signed)
