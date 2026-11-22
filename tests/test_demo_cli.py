from collections.abc import Callable
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from eye_demo import cli

runner = CliRunner()
STAMP = "2026-01-01T10:00:00Z"
SCENARIO = {
    "id": "S8",
    "title": "Price typo",
    "active": True,
    "injected_ts": STAMP,
    "cleared_ts": None,
}
SIM = {
    "sim_clock": STAMP,
    "last_landed_hour": None,
    "active_scenarios": [SCENARIO],
}
INCIDENT = {
    "id": "inc-1",
    "title": "Reporter price alert",
    "summary": "Price dropped 91%",
    "status": "open",
    "verdict": "HYPOTHESIS",
    "investigation_ids": ["inv-1"],
}


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    client_type = httpx.AsyncClient

    def install(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> list[httpx.Request]:
        calls = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(
            cli.httpx,
            "AsyncClient",
            lambda **kwargs: client_type(
                transport=httpx.MockTransport(respond), **kwargs
            ),
        )
        return calls

    return install


def test_trigger(transport: Callable[..., Any]) -> None:
    calls = transport(lambda _: httpx.Response(200, json=[SCENARIO]))
    result = runner.invoke(cli.app, ["--sim-url", "http://sim:9000/", "trigger", "s8"])
    assert result.exit_code == 0, result.output
    assert '"id": "S8"' in result.output
    assert [(r.method, str(r.url)) for r in calls] == [
        ("POST", "http://sim:9000/scenarios/s8")
    ]


@pytest.mark.parametrize(
    "code, message", [(404, "unknown"), (409, "conflict"), (500, "request failed")]
)
def test_http_errors(transport: Callable[..., Any], code: int, message: str) -> None:
    transport(lambda _: httpx.Response(code))
    result = runner.invoke(cli.app, ["trigger", "s8"])
    assert result.exit_code == 1
    assert message in result.output


@pytest.mark.parametrize("args", [["trigger", "s8"], ["status"], ["narrate"]])
def test_connection_refused(transport: Callable[..., Any], args: list[str]) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport(refused)
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "Cannot connect" in result.output
    assert "Connection refused" in result.output


@pytest.mark.parametrize("body", [[], {}, [{"id": "S8"}]])
def test_trigger_bad_response(transport: Callable[..., Any], body: Any) -> None:
    transport(lambda _: httpx.Response(200, json=body))
    result = runner.invoke(cli.app, ["trigger", "s8"])
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_status_and_env(transport: Callable[..., Any]) -> None:
    calls = transport(
        lambda r: httpx.Response(
            200,
            json=SIM
            if r.url.path == "/status"
            else [INCIDENT, {**INCIDENT, "id": "old", "status": "resolved"}],
        )
    )
    result = runner.invoke(
        cli.app,
        ["status"],
        env={"DEMO_SIM_URL": "http://sim", "DEMO_API_URL": "http://core"},
    )
    assert result.exit_code == 0, result.output
    assert "Sim clock:" in result.output
    assert "Last landed hour: none" in result.output
    assert "Active scenarios: S8" in result.output
    assert "Open incidents: 1" in result.output
    assert "Reporter price alert: Price dropped 91%" in result.output
    assert [r.url.host for r in calls] == ["sim", "core"]


def test_narration_lifecycle(transport: Callable[..., Any]) -> None:
    snapshots = iter(
        [
            [],
            [INCIDENT],
            [INCIDENT],
            [{**INCIDENT, "status": "resolved", "verdict": "CONFIRMED"}],
        ]
    )
    calls = transport(
        lambda r: httpx.Response(
            200, json=SIM if r.url.path == "/status" else next(snapshots)
        )
    )
    result = runner.invoke(cli.app, ["narrate", "--interval", "0.01", "--timeout", "1"])
    assert result.exit_code == 0, result.output
    assert result.output.count("S8 triggered") == 1
    assert result.output.count("Incident opened") == 1
    assert "Reporter price alert: Price dropped 91%" in result.output
    assert "Investigation: inv-1" in result.output
    assert "Verdict: CONFIRMED" in result.output
    assert "Incident resolved: inc-1" in result.output
    assert all(r.method == "GET" for r in calls)


@pytest.mark.parametrize("until, code", [("timeout", 0), ("resolved", 1)])
def test_empty_timeout(transport: Callable[..., Any], until: str, code: int) -> None:
    transport(
        lambda r: httpx.Response(
            200, json={**SIM, "active_scenarios": []} if r.url.path == "/status" else []
        )
    )
    result = runner.invoke(cli.app, ["narrate", "--timeout", "0.01", "--until", until])
    assert result.exit_code == code
    assert "Narration timeout reached" in result.output
    assert "Incident opened" not in result.output


def test_empty_status(transport: Callable[..., Any]) -> None:
    transport(
        lambda r: httpx.Response(
            200, json={**SIM, "active_scenarios": []} if r.url.path == "/status" else []
        )
    )
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "Active scenarios: none" in result.output
    assert "Open incidents: 0" in result.output


@pytest.mark.parametrize("args", [["status"], ["narrate"]])
def test_invalid_snapshot(transport: Callable[..., Any], args: list[str]) -> None:
    transport(lambda _: httpx.Response(200, json={}))
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "Error:" in result.output
