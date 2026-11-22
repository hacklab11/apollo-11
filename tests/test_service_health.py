import httpx
import pytest

from eye.config import Settings
from eye.mcp_server import create_server, transport_security
from eye.specialists import app as specialists_module
from eye.specialists.app import create_app as specialists_app
from eye.specialists.tools import DataClient
from selleros.server import create_app as selleros_app


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["specialists", "selleros", "mcp"])
async def test_health_without_data_or_credentials(
    service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    if service == "specialists":
        app = specialists_app(Settings())
    elif service == "selleros":
        app = selleros_app()
    else:
        app = create_server("http://unreachable.invalid").streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert (await client.post("/healthz")).status_code == 405
        assert (await client.get("/missing-health")).status_code == 404


@pytest.mark.asyncio
async def test_specialists_configured_core_url(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    original = specialists_module.DataClient

    def capture(http: httpx.AsyncClient, base_url: str) -> DataClient:
        urls.append(base_url)
        return original(http, base_url)

    monkeypatch.setattr(specialists_module, "DataClient", capture)
    monkeypatch.setenv("EYE_CORE_URL", "http://eye-core:8000")
    app = specialists_app(Settings())
    async with app.router.lifespan_context(app):
        assert urls == ["http://eye-core:8000/data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed_hosts", "host", "expected_status"),
    [
        ("", "localhost:8100", 200),
        ("", "eye-mcp:8100", 421),
        ("eye-mcp:8100", "eye-mcp:8100", 200),
        ("eye-mcp:8100", "localhost:8100", 200),
        ("eye-mcp:8100", "untrusted.example:8100", 421),
    ],
)
async def test_mcp_transport_host_allowlist(
    monkeypatch: pytest.MonkeyPatch, allowed_hosts: str, host: str, expected_status: int
) -> None:
    monkeypatch.setenv("EYE_MCP_ALLOWED_HOSTS", allowed_hosts)
    app = create_server("http://unreachable.invalid").streamable_http_app(
        transport_security=transport_security()
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://{host}"
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "host-test", "version": "1"},
                },
            },
        )
        assert response.status_code == expected_status
