"""Real OAuth issuer shared by offline application tests."""

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
import pytest
import yaml
from a2a.types import AgentCard
from a2a.utils.signing import create_agent_card_signer
from werkzeug.serving import make_server

from eye_auth.server import create_app
from eye_auth.trust import jwks


@pytest.fixture(scope="session")
def auth_issuer(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    directory = tmp_path_factory.mktemp("oauth")
    config = yaml.safe_load((Path(__file__).parent / "config/auth.yaml").read_text())
    scopes = {
        scope for client in config["clients"].values() for scope in client["scopes"]
    }
    scopes.update(f"{name}:echo" for name in config["card_clients"])
    scopes.update(f"{name}:unrelated" for name in config["card_clients"])
    scopes.add("eye-core:unrelated")
    config["clients"]["test-client"] = {
        "secret_env": "TEST_OAUTH_SECRET",
        "scopes": sorted(scopes),
    }
    path = directory / "clients.yaml"
    path.write_text(json.dumps(config))
    server = make_server("127.0.0.1", 0, lambda environ, start: [], threaded=True)
    url = f"http://127.0.0.1:{server.server_port}"
    with pytest.MonkeyPatch.context() as env:
        env.setenv("EYE_AUTH_URL", url)
        for client in config["clients"].values():
            env.setenv(client["secret_env"], "test-only-oauth-secret")
        key = directory / "key.pem"
        app = create_app(config_path=path, key_path=key)
    server.app = app
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    kid = httpx.get(url + "/.well-known/jwks.json").json()["keys"][0]["kid"]
    yield {"url": url, "key": key, "kid": kid, "config": config}
    server.shutdown()
    thread.join()
    server.server_close()
    jwks.cache_clear()


@pytest.fixture
def runtime_auth(
    auth_issuer: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    monkeypatch.setenv("EYE_AUTH_URL", auth_issuer["url"])
    monkeypatch.setenv("EYE_AUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("EYE_AUTH_CLIENT_SECRET", "test-only-oauth-secret")
    for client in auth_issuer["config"]["clients"].values():
        monkeypatch.setenv(client["secret_env"], "test-only-oauth-secret")
    return auth_issuer


@pytest.fixture(autouse=True)
def enrolled_test_cards(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if "tests/auth" in str(request.path):
        return
    authority = request.getfixturevalue("runtime_auth")
    # ASGITransport intentionally does not run lifespan. These isolated tests
    # enroll their cards with the test issuer's actual key; startup enrollment
    # itself is covered separately using application lifespans.
    signer = create_agent_card_signer(
        authority["key"].read_bytes(),
        {
            "kid": authority["kid"],
            "alg": "RS256",
            "jku": authority["url"] + "/.well-known/jwks.json",
            "typ": "JOSE",
        },
    )
    for name in (
        "eye.agents.service",
        "eye.specialists.app",
        "selleros.server",
        "scripted_agents",
        "stub_server",
    ):
        module = sys.modules.get(name)
        if module is None:
            continue
        original = module.create_agent_card_routes

        def routes(
            card: AgentCard, *args: Any, _original: Callable = original, **kwargs: Any
        ) -> list:
            del card.signatures[:]
            signer(card)
            return _original(card, *args, **kwargs)

        monkeypatch.setattr(module, "create_agent_card_routes", routes)


@pytest.fixture
def oauth_headers(runtime_auth: dict[str, Any]) -> Callable[..., dict[str, str]]:
    def headers(scope: str, client_id: str = "test-client") -> dict[str, str]:
        response = httpx.post(
            runtime_auth["url"] + "/oauth/token",
            auth=(client_id, "test-only-oauth-secret"),
            data={"grant_type": "client_credentials", "scope": scope},
        )
        response.raise_for_status()
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    return headers
