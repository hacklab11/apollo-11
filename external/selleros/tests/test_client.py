import json
import shutil

import httpx
import pytest
from pydantic import ValidationError
from selleros.client import LLMClient, LlmSettings, LLMUnavailable, configure_client
from selleros.data import FileSellerStore
from test_selleros import FIXTURES, answer, install_mock, message, request


@pytest.fixture
def store(tmp_path):
    shutil.copyfile(FIXTURES / "s6.json", tmp_path / "sellers.json")
    return FileSellerStore(tmp_path)


def completion(name="submit", args=None):
    return {
        "id": "chat-test",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "tool-test",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    args
                                    or {
                                        "agent": "selleros",
                                        "answered": False,
                                        "findings": [],
                                    }
                                ),
                            },
                        }
                    ]
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def api_request():
    return {
        "model": "test:free",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "test"}],
        "messages": [{"role": "user", "content": "go"}],
        "tools": [],
    }


def test_environment_settings(monkeypatch):
    for field in ("PROVIDER", "BASE_URL", "MODEL", "API_KEY_ENV"):
        monkeypatch.delenv("SELLEROS_" + field, raising=False)
    defaults = LlmSettings.from_env()
    assert defaults.provider == "anthropic"
    assert defaults.model == "claude-opus-5-5"
    monkeypatch.setenv("SELLEROS_PROVIDER", "openai_compatible")
    monkeypatch.setenv("SELLEROS_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("SELLEROS_MODEL", "test:free")
    monkeypatch.setenv("SELLEROS_API_KEY_ENV", "TEST_PROVIDER_KEY")
    settings = LlmSettings.from_env()
    assert settings.provider == "openai_compatible"
    assert settings.model == "test:free"
    assert settings.base_url == "https://provider.example/v1"
    assert settings.api_key_env == "TEST_PROVIDER_KEY"
    monkeypatch.setenv("SELLEROS_PROVIDER", "invalid")
    with pytest.raises(ValidationError):
        LlmSettings.from_env()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "openai_compatible"])
async def test_agent_provider_round_trip(store, monkeypatch, provider):
    seen = []
    expected = {"agent": "selleros", "answered": False, "findings": []}
    if provider == "anthropic":
        install_mock(
            [
                message("get_costs", {"seller_ids": ["S-03"]}),
                message("submit", expected, 1),
            ],
            seen,
        )
    else:
        monkeypatch.setenv("TEST_PROVIDER_KEY", "offline-placeholder")
        script = [
            completion("get_costs", {"seller_ids": ["S-03"]}),
            completion(args=expected),
        ]

        def handle(req):
            assert req.headers["Authorization"] == "Bearer offline-placeholder"
            assert str(req.url) == "https://provider.example/v1/chat/completions"
            seen.append(json.loads(req.content))
            return httpx.Response(200, json=script.pop(0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = LLMClient(
                LlmSettings(
                    provider=provider,
                    base_url="https://provider.example/v1",
                    model="test:free",
                    api_key_env="TEST_PROVIDER_KEY",
                ),
                http_client=http,
            )
            configure_client(client)
            result = await answer(store, request("get_costs S-03"))
        assert result.answered is False
        assert client.status()["tokens_in"] == 20
        assert seen[0]["model"] == "test:free"
        assert seen[1]["messages"][-1]["role"] == "tool"
        assert "betas" not in seen[0]
        return
    result = await answer(store, request("get_costs S-03"))
    assert result.answered is False
    assert seen[0]["model"] == "claude-opus-5-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,attempts", [(400, 1), (429, 3), (503, 3)])
async def test_http_failures(monkeypatch, status, attempts):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-placeholder")
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = LLMClient(LlmSettings(provider="openai_compatible"), http_client=http)
        with pytest.raises(LLMUnavailable, match=f"HTTP {status}"):
            await client.create(api_request())
    assert len(seen) == attempts


@pytest.mark.asyncio
async def test_missing_key(monkeypatch):
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    client = LLMClient(
        LlmSettings(provider="openai_compatible", api_key_env="MISSING_PROVIDER_KEY")
    )
    with pytest.raises(LLMUnavailable, match="Missing API key environment variable"):
        await client.create(api_request())


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"choices": []}])
async def test_invalid_response(monkeypatch, payload):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-placeholder")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    ) as http:
        client = LLMClient(LlmSettings(provider="openai_compatible"), http_client=http)
        with pytest.raises(LLMUnavailable, match="Invalid Chat Completions response"):
            await client.create(api_request())


@pytest.mark.asyncio
async def test_record_replay_provider_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-placeholder")
    path = tmp_path / "cache.jsonl"
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        settings = LlmSettings(
            provider="openai_compatible", mode="record", cache_path=path
        )
        result = await LLMClient(settings, http_client=http).create(api_request())
        replay = LLMClient(
            settings.model_copy(update={"mode": "replay"}), http_client=http
        )
        assert await replay.create(api_request()) == result
        assert replay.status()["calls_per_hour"] == 0
        other = settings.model_copy(
            update={"mode": "replay", "base_url": "https://other.example/v1"}
        )
        await LLMClient(other, http_client=http).create(api_request())
    assert len(seen) == 2
