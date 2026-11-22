"""Authlib client-credentials sessions and startup card enrollment."""

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from a2a.types import AgentCard
from authlib.integrations.httpx_client import AsyncOAuth2Client, OAuth2Client
from google.protobuf.json_format import MessageToDict, ParseDict

from eye_auth.trust import issuer, verify_card


def credentials(client_id: str | None = None) -> tuple[str, str]:
    client_id = client_id or os.environ["EYE_AUTH_CLIENT_ID"]
    secret = os.environ.get(f"EYE_AUTH_{client_id.upper().replace('-', '_')}_SECRET")
    return client_id, secret or os.environ["EYE_AUTH_CLIENT_SECRET"]


@asynccontextmanager
async def oauth_client(
    scopes: set[str], *, client_id: str | None = None, **kwargs: Any
) -> AsyncIterator[AsyncOAuth2Client]:
    client_id, secret = credentials(client_id)
    async with AsyncOAuth2Client(
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
def sync_client(scopes: set[str], **kwargs: Any) -> Iterator[OAuth2Client]:
    client_id, secret = credentials()
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
