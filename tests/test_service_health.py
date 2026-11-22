import httpx
import pytest

from eye.config import Settings
from eye.mcp_server import create_server
from eye.specialists.app import create_app as specialists_app
from selleros.server import create_app as selleros_app


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["specialists", "selleros", "mcp"])
async def test_health_without_data_or_credentials(service: str, monkeypatch) -> None:
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
async def test_specialists_configured_core_url(monkeypatch) -> None:
    from eye.specialists import app as module

    urls = []
    original = module.DataClient

    def capture(http: httpx.AsyncClient, base_url: str):
        urls.append(base_url)
        return original(http, base_url)

    monkeypatch.setattr(module, "DataClient", capture)
    monkeypatch.setenv("EYE_CORE_URL", "http://eye-core:8000")
    app = specialists_app(Settings())
    async with app.router.lifespan_context(app):
        assert urls == ["http://eye-core:8000/data"]
