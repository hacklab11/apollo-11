"""Container smoke assertions; run inside auth so its private key never leaves it."""

import asyncio
import json
import os
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
import jwt
from a2a.types import AgentCard
from google.protobuf.json_format import ParseDict

from eye.config import A2aSettings
from eye.directory import AgentDirectory
from eye_auth.client import sync_client
from eye_auth.trust import decode_token, issuer, verify_card


def probe_tokens() -> tuple[str, str]:
    with sync_client({"sentinel:detect"}, client_id="eye-core", timeout=10) as client:
        valid = client.token["access_token"]
    claims = decode_token(valid, "sentinel")
    # A single-skill audience has no other configured skill. Only this test,
    # inside the issuer container, signs a same-audience negative-scope probe.
    claims["scope"] = "sentinel:unauthorized"
    private = Path(os.environ["EYE_AUTH_KEY_PATH"]).read_bytes()
    wrong = jwt.encode(
        claims, private, algorithm="RS256", headers=jwt.get_unverified_header(valid)
    )
    return valid, wrong


async def reject_card(card: dict[str, Any], damage: str) -> None:
    body = deepcopy(card)
    if damage == "unsigned":
        body.pop("signatures", None)
    else:
        body["description"] = "Tampered smoke card"
    content = json.dumps(body).encode()

    class CardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), CardHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            directory = AgentDirectory(
                A2aSettings(sentinel_url=f"http://127.0.0.1:{server.server_port}/")
            )
            try:
                await directory.resolve()
            except ValueError as exc:
                assert str(exc) == "Untrusted agent card", str(exc)
            else:
                raise AssertionError(f"Directory accepted a {damage} card")
            assert not directory.cards
        finally:
            await asyncio.to_thread(server.shutdown)
            thread.join()
    print(f"OK A2A caller rejects {damage} card before routing", flush=True)


async def main() -> None:
    valid, wrong = await asyncio.to_thread(probe_tokens)
    url = "http://sentinel:8210/"
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        assert (await client.get(issuer() + "/healthz")).status_code == 200
        assert (await client.get(url + "healthz")).status_code == 200
        response = await client.get(url + ".well-known/agent-card.json")
        response.raise_for_status()
        card = response.json()
        await verify_card(ParseDict(card, AgentCard()), "sentinel")
        print("OK auth health and public signed agent card", flush=True)
        for token in (None, "invalid"):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = await client.post(url, json={}, headers=headers)
            assert response.status_code == 401, response.text
        print("OK agent JSON-RPC missing/invalid token -> 401", flush=True)
        response = await client.post(
            url, json={}, headers={"Authorization": f"Bearer {wrong}"}
        )
        assert response.status_code == 403, response.text
        print("OK agent JSON-RPC valid JWT with wrong scope -> 403", flush=True)
        response = await client.post(
            url,
            json={},
            headers={"Authorization": f"Bearer {valid}", "A2A-Version": "1.0"},
        )
        assert response.status_code not in {401, 403}, response.text
        print("OK correctly scoped JWT passes the agent auth boundary", flush=True)
    for damage in ("unsigned", "tampered"):
        await reject_card(card, damage)


if __name__ == "__main__":
    asyncio.run(main())
