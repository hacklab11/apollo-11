import httpx
import pytest
from mcp.server import MCPServer
from mcp_fake_core import Calls, fake_core

from eye.mcp_server import create_server


@pytest.fixture
def calls() -> Calls:
    return []


@pytest.fixture
def server(calls: Calls) -> MCPServer:
    transport = httpx.ASGITransport(app=fake_core(calls))
    return create_server("http://core", transport=transport)
