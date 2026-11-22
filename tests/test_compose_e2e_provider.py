import json
from collections.abc import Iterator

import pytest
from e2e import provider
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    provider.calls.clear()
    provider.errors.clear()
    with TestClient(provider.app) as client:
        yield client


def completion(model: str, payload: dict, outputs: list[dict] | None = None) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": json.dumps(payload)},
            *[
                {"role": "tool", "content": json.dumps(output)}
                for output in (outputs or [])
            ],
        ],
        "tools": [{"type": "function", "function": {"name": "submit"}}],
    }


def post(client: TestClient, body: dict):
    return client.post(
        "/v1/chat/completions",
        json=body,
        headers={"Authorization": "Bearer offline-test-only"},
    )


def test_empty_candidates_submit_no_decisions(client: TestClient) -> None:
    response = post(client, completion("sentinel", {"candidates": []}))
    assert response.status_code == 200
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "submit"
    assert json.loads(call["function"]["arguments"]) == {"decisions": []}
    assert client.get("/stats").json() == {"calls": {"sentinel": 1}, "errors": []}


def test_reporter_returns_narrative_without_tools(client: TestClient) -> None:
    body = completion("reporter", {})
    del body["tools"]
    response = post(client, body)
    assert response.status_code == 200
    message = response.json()["choices"][0]["message"]
    assert (
        json.loads(message["content"])["title"]
        == "Electronics price-change investigation"
    )
    assert "tool_calls" not in message


@pytest.mark.parametrize(
    "body",
    [
        completion("unknown-model", {}),
        completion("cartographer", {"mode": "unexpected"}),
        completion("sentinel", {}),
        completion("detective", {"entry": {}}, [{"error": "tool failed"}]),
        {**completion("reporter", {}), "messages": []},
    ],
)
def test_unexpected_request_fails_closed(client: TestClient, body: dict) -> None:
    assert post(client, body).status_code == 422
    stats = client.get("/stats").json()
    assert stats["calls"] == {} and stats["errors"]


def test_rejects_real_or_missing_credentials(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=completion("reporter", {}))
    assert response.status_code == 401
    assert client.get("/stats").json()["calls"] == {}
