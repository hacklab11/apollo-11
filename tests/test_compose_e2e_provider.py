import json
from collections.abc import Iterator

import pytest
from e2e import provider
from fastapi.testclient import TestClient
from httpx import Response
from openai.types.chat import ChatCompletion


@pytest.fixture
def client() -> Iterator[TestClient]:
    provider.calls.clear()
    provider.errors.clear()
    provider.investigate_after = None
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


def post(client: TestClient, body: dict) -> Response:
    return client.post(
        "/v1/chat/completions",
        json=body,
        headers={"Authorization": "Bearer offline-test-only"},
    )


def test_empty_candidates_submit_no_decisions(client: TestClient) -> None:
    response = post(client, completion("sentinel", {"candidates": []}))
    assert response.status_code == 200
    parsed = ChatCompletion.model_validate(response.json())
    assert parsed.object == "chat.completion"
    assert parsed.usage is not None
    assert parsed.usage.total_tokens == 0
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
    ("ts", "completeness", "expected"),
    [
        ("2026-11-27T09:00:00Z", 1, "ignore"),
        ("2026-11-27T10:00:00Z", 1, "investigate"),
        ("2026-11-27T10:00:00Z", 0.5, "defer"),
    ],
)
def test_sentinel_follows_injected_schedule(
    client: TestClient, ts: str, completeness: float, expected: str
) -> None:
    payload = {
        "ts": ts,
        "min_completeness": 0.9,
        "open_incidents": [],
        "candidates": [
            {
                "completeness": completeness,
                "anomaly": {
                    "metric": "aov",
                    "segment": {"category": "electronics"},
                    "delta": -1,
                },
            }
        ],
    }
    body = completion("sentinel", payload)
    response = post(client, body)
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    action = json.loads(call["function"]["arguments"])["decisions"][0]["action"]
    assert action == ("defer" if completeness < 0.9 else "ignore")
    assert (
        client.post(
            "/scenario", json={"investigate_after": "2026-11-27T10:00:00Z"}
        ).status_code
        == 200
    )
    response = post(client, body)
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert (
        json.loads(call["function"]["arguments"])["decisions"][0]["action"] == expected
    )


def test_rejects_invalid_scenario_schedule(client: TestClient) -> None:
    assert (
        client.post("/scenario", json={"investigate_after": "not-a-date"}).status_code
        == 422
    )
    assert provider.investigate_after is None


@pytest.mark.parametrize("include_target", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_sentinel_ignores_unrelated_margin_signals(
    client: TestClient, include_target: bool, reverse: bool
) -> None:
    client.post(
        "/scenario", json={"investigate_after": "2026-11-27T10:00:00Z"}
    ).raise_for_status()
    anomalies = [
        {
            "metric": "contribution_margin",
            "segment": {"country": "DE"},
            "delta": -15968.9,
        },
        {
            "metric": "first_party_margin",
            "segment": {"category": "fashion"},
            "delta": -100,
        },
        {
            "metric": "aov",
            "segment": {"category": "electronics", "country": "DE"},
            "delta": -50,
        },
    ]
    target = {"metric": "aov", "segment": {"category": "electronics"}, "delta": -10}
    if include_target:
        anomalies.append(target)
    if reverse:
        anomalies.reverse()
    response = post(
        client,
        completion(
            "sentinel",
            {
                "ts": "2026-11-27T10:00:00Z",
                "min_completeness": 0.9,
                "open_incidents": [],
                "candidates": [{"anomaly": a, "completeness": 1} for a in anomalies],
            },
        ),
    )
    assert response.status_code == 200
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    decisions = json.loads(call["function"]["arguments"])["decisions"]
    assert [d["anomaly"] for d in decisions if d["action"] == "investigate"] == (
        [target] if include_target else []
    )
    assert all(d["action"] == "ignore" for d in decisions if d["anomaly"] != target)


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
