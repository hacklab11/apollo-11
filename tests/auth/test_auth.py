import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from time import time

import httpx
import jwt
import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.signing import create_agent_card_signer
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Request
from google.protobuf.json_format import MessageToDict
from werkzeug.serving import make_server

from eye_auth.client import oauth_client, sign_card, sync_client
from eye_auth.server import create_app, load_key
from eye_auth.trust import (
    A2AAuthMiddleware,
    decode_token,
    jwks,
    require_scopes,
    verify_card,
)


def agent_card(name: str) -> AgentCard:
    return AgentCard(
        name=name,
        description="Test agent",
        version="1.0",
        capabilities=AgentCapabilities(),
        supported_interfaces=[
            AgentInterface(
                url="http://127.0.0.1:1",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="detect", name="Detect", description="Detect", tags=["detect"]
            )
        ],
    )


@pytest.fixture
def authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Path]]:
    config = tmp_path / "auth.yaml"
    config.write_text(
        json.dumps(
            {
                "card_clients": ["sentinel", "detective"],
                "clients": {
                    "sentinel": {
                        "secret_env": "TEST_SENTINEL_SECRET",
                        "scopes": ["auth:card", "sentinel:detect", "sentinel:other"],
                    },
                    "detective": {
                        "secret_env": "TEST_DETECTIVE_SECRET",
                        "scopes": ["auth:card"],
                    },
                    "unconfigured": {
                        "secret_env": "TEST_MISSING_SECRET",
                        "scopes": ["auth:card"],
                    },
                },
            }
        )
    )
    monkeypatch.setenv("TEST_SENTINEL_SECRET", "test-only-sentinel")
    monkeypatch.setenv("TEST_DETECTIVE_SECRET", "test-only-detective")
    monkeypatch.setenv("EYE_AUTH_CLIENT_ID", "sentinel")
    monkeypatch.setenv("EYE_AUTH_CLIENT_SECRET", "test-only-sentinel")
    # Bind first so tokens carry the actual ephemeral issuer URL.
    server = make_server("127.0.0.1", 0, lambda environ, start: [])
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("EYE_AUTH_URL", url)
    key = tmp_path / "key.pem"
    app = create_app(config_path=config, key_path=key)
    server.app = app
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield url, key
    server.shutdown()
    thread.join()
    server.server_close()
    jwks.cache_clear()


def issue(
    url: str,
    scope: str = "sentinel:detect",
    *,
    client: str = "sentinel",
    secret: str = "test-only-sentinel",
) -> httpx.Response:
    return httpx.post(
        url + "/oauth/token",
        auth=(client, secret),
        data={"grant_type": "client_credentials", "scope": scope},
    )


def test_issuance_and_jwks(authority: tuple[str, Path]) -> None:
    url, key = authority
    response = issue(url)
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == 300
    assert "refresh_token" not in body
    claims = decode_token(body["access_token"], "sentinel")
    assert claims["iss"] == url
    assert claims["sub"] == claims["client_id"] == "sentinel"
    assert claims["aud"] == "sentinel"
    assert claims["scope"] == "sentinel:detect"
    assert claims["exp"] - claims["iat"] == 300
    assert claims["jti"]
    assert 298 <= claims["exp"] - time() <= 300
    assert issue(url).json()["access_token"] != body["access_token"]
    public = httpx.get(url + "/.well-known/jwks.json").json()["keys"][0]
    assert public["kid"] == jwt.get_unverified_header(body["access_token"])["kid"]
    assert not {"d", "p", "q", "dp", "dq", "qi"}.intersection(public)
    assert load_key(key) == key.read_bytes()
    assert key.stat().st_mode & 0o777 == 0o600
    assert httpx.get(url + "/healthz").status_code == 200


@pytest.mark.parametrize(
    "scope", ["", "sentinel:forbidden", "auth:card sentinel:detect"]
)
def test_scope_restrictions(authority: tuple[str, Path], scope: str) -> None:
    response = issue(authority[0], scope)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


@pytest.mark.parametrize(
    "client,secret", [("sentinel", "bad"), ("unknown", "bad"), ("unconfigured", "")]
)
def test_invalid_client(authority: tuple[str, Path], client: str, secret: str) -> None:
    assert issue(authority[0], client=client, secret=secret).status_code == 401


@pytest.mark.asyncio
async def test_token_validation_and_middleware(authority: tuple[str, Path]) -> None:
    url, key_path = authority
    app = FastAPI()
    app.add_middleware(
        A2AAuthMiddleware, services={"/rpc": ("sentinel", {"sentinel:detect"})}
    )

    @app.post("/rpc")
    async def rpc() -> dict:
        return {"ok": True}

    @app.get("/dependency")
    async def dependency(request: Request) -> dict:
        return await require_scopes(request, "sentinel", {"sentinel:detect"})

    good = issue(url).json()["access_token"]
    wrong_scope = issue(url, "sentinel:other").json()["access_token"]
    wrong_audience = issue(url, "auth:card").json()["access_token"]
    claims = decode_token(good, "sentinel")
    private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    header = jwt.get_unverified_header(good)
    expired = jwt.encode(
        claims | {"iat": int(time()) - 600, "exp": int(time()) - 300},
        private,
        algorithm="RS256",
        headers=header,
    )
    wrong_issuer = jwt.encode(
        claims | {"iss": "http://untrusted"}, private, algorithm="RS256", headers=header
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for token in (None, "invalid", expired, wrong_issuer, wrong_audience):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = await client.post("/rpc", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
        assert (
            await client.post(
                "/rpc", headers={"Authorization": f"Bearer {wrong_scope}"}
            )
        ).status_code == 403
        assert (
            await client.post("/rpc", headers={"Authorization": f"Bearer {good}"})
        ).status_code == 200
        assert (await client.get("/dependency")).status_code == 401
        assert (
            await client.get("/dependency", headers={"Authorization": f"Bearer {good}"})
        ).status_code == 200


@pytest.mark.asyncio
async def test_signed_cards_and_authlib_clients(authority: tuple[str, Path]) -> None:
    url, _ = authority
    card = agent_card("sentinel")
    with pytest.raises(ValueError, match="Untrusted"):
        await verify_card(card)
    await sign_card(card, "sentinel")
    await verify_card(card, "sentinel")
    with pytest.raises(ValueError, match="identity"):
        await verify_card(card, "detective")
    card.description = "tampered"
    with pytest.raises(ValueError, match="Untrusted"):
        await verify_card(card)
    async with oauth_client({"auth:card"}) as client:
        response = await client.post(
            url + "/cards/sign", json=MessageToDict(agent_card("detective"))
        )
        assert response.status_code == 403
        response = await client.post(url + "/cards/sign", json={"unknown": "field"})
        assert response.status_code == 400
        # Client credentials do not use refresh tokens: Authlib reissues on expiry.
        old = client.token["access_token"]
        client.token["expires_at"] = 1
        assert (await client.get(url + "/healthz")).status_code == 200
        assert client.token["access_token"] != old
    with sync_client({"sentinel:detect"}) as client:
        old = client.token["access_token"]
        client.token["expires_at"] = 1
        assert client.get(url + "/healthz").status_code == 200
        assert client.token["access_token"] != old
    assert (
        await asyncio.to_thread(httpx.post, url + "/cards/sign", json={})
    ).status_code == 401
    token = issue(url).json()["access_token"]
    assert (
        await asyncio.to_thread(
            httpx.post,
            url + "/cards/sign",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    ).status_code == 401
    token = issue(url, "auth:card").json()["access_token"]
    assert (
        await asyncio.to_thread(
            httpx.post,
            url + "/cards/sign",
            json=MessageToDict(agent_card("sentinel")),
            headers={"Authorization": f"Bearer {token}"},
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_key_rotation_and_pinned_jwks(
    authority: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url, _ = authority
    original = issue(url).json()["access_token"]
    decode_token(original, "sentinel")
    replacement = serialization.load_pem_private_key(
        load_key(tmp_path / "new.pem"), password=None
    )
    public_jwk = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(replacement.public_key())
    ) | {"kid": "rotated", "alg": "RS256"}
    # A real HTTP JWKS endpoint advertises the rotated key after the old key
    # has already been cached by PyJWKClient.
    endpoint = make_server(
        "127.0.0.1",
        0,
        lambda environ, start: (
            start("200 OK", [("Content-Type", "application/json")]),
            [json.dumps({"keys": [public_jwk]}).encode()],
        )[1],
    )
    thread = Thread(target=endpoint.serve_forever, daemon=True)
    thread.start()
    try:
        keys = jwks(url)
        keys.uri = f"http://127.0.0.1:{endpoint.server_port}/jwks"
        claims = jwt.decode(original, options={"verify_signature": False})
        token = jwt.encode(
            claims,
            replacement,
            algorithm="RS256",
            headers={"kid": "rotated", "typ": "at+jwt"},
        )
        assert decode_token(token, "sentinel")["sub"] == "sentinel"
        card = agent_card("sentinel")
        create_agent_card_signer(
            replacement,
            {
                "kid": "rotated",
                "alg": "RS256",
                "jku": "http://untrusted.invalid/jwks",
                "typ": "JOSE",
            },
        )(card)
        await verify_card(card, "sentinel")
    finally:
        endpoint.shutdown()
        thread.join()
        endpoint.server_close()
