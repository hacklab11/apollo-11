"""Observe the continuous simulation without advancing or resetting its clock."""

import asyncio
import time
from collections.abc import Coroutine
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

import httpx
import typer
from pydantic import BaseModel, TypeAdapter

app = typer.Typer(no_args_is_help=True)


class ScenarioState(BaseModel):
    id: str
    title: str
    active: bool
    injected_ts: datetime | None
    cleared_ts: datetime | None


class SimStatus(BaseModel):
    sim_clock: datetime
    last_landed_hour: datetime | None
    active_scenarios: list[ScenarioState]


class Incident(BaseModel):
    id: str
    title: str
    summary: str
    status: str
    verdict: str | None = None
    investigation_ids: list[str] = []


class Until(StrEnum):
    resolved = "resolved"
    timeout = "timeout"


class Settings(BaseModel):
    sim_url: str
    api_url: str


@app.callback()
def configure(
    ctx: typer.Context,
    sim_url: Annotated[
        str, typer.Option(envvar="DEMO_SIM_URL", help="Continuous simulator URL.")
    ] = "http://127.0.0.1:8100",
    api_url: Annotated[
        str, typer.Option(envvar="DEMO_API_URL", help="Core public API URL.")
    ] = "http://127.0.0.1:8080",
) -> None:
    ctx.obj = Settings(sim_url=sim_url.rstrip("/"), api_url=api_url.rstrip("/"))


async def request(client: httpx.AsyncClient, method: str, url: str) -> Any:
    try:
        response = await client.request(method, url)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        message = {
            404: "not found (unknown or disabled scenario/endpoint)",
            409: "scenario conflict (already active or incompatible)",
        }.get(exc.response.status_code, "request failed")
        raise ValueError(f"{url}: HTTP {exc.response.status_code}: {message}") from exc
    except httpx.RequestError as exc:
        raise ValueError(f"Cannot connect to {url}: {exc}") from exc


def run(coroutine: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(coroutine)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def trigger_scenario(settings: Settings, scenario: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        states = TypeAdapter(list[ScenarioState]).validate_python(
            await request(
                client, "POST", f"{settings.sim_url}/scenarios/{scenario.lower()}"
            )
        )
    matches = [state for state in states if state.id.lower() == scenario.lower()]
    if not matches:
        raise ValueError(f"Simulator response did not contain {scenario.upper()}")
    for state in matches:
        typer.echo(state.model_dump_json(indent=2))


@app.command()
def trigger(ctx: typer.Context, scenario: str) -> None:
    """Trigger a scenario (for example: demo trigger s8)."""
    run(trigger_scenario(ctx.obj, scenario))


async def snapshot(
    client: httpx.AsyncClient, settings: Settings
) -> tuple[SimStatus, list[Incident]]:
    sim = SimStatus.model_validate(
        await request(client, "GET", f"{settings.sim_url}/status")
    )
    incidents = TypeAdapter(list[Incident]).validate_python(
        await request(client, "GET", f"{settings.api_url}/api/incidents")
    )
    return sim, incidents


TERMINAL = {"resolved", "closed", "innocent"}


async def show_status(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        sim, incidents = await snapshot(client, settings)
    typer.echo(f"Sim clock: {sim.sim_clock.isoformat()}")
    typer.echo(f"Last landed hour: {sim.last_landed_hour or 'none'}")
    active = [state for state in sim.active_scenarios if state.active]
    typer.echo("Active scenarios: " + (", ".join(s.id for s in active) or "none"))
    opened = [incident for incident in incidents if incident.status not in TERMINAL]
    typer.echo(f"Open incidents: {len(opened)}")
    for incident in opened:
        typer.echo(
            f"{incident.id} [{incident.status}] {incident.title}: {incident.summary}"
        )


@app.command()
def status(ctx: typer.Context) -> None:
    """Show simulator progress and Core's non-terminal incidents."""
    run(show_status(ctx.obj))


async def follow(
    settings: Settings, interval: float, timeout: float, until: Until
) -> None:
    deadline = time.monotonic() + timeout
    seen: dict[str, Incident] = {}
    s8_injection: datetime | None = None
    typer.echo(
        "Following simulator S8 and all Core incidents (no scenario attribution)."
    )
    async with httpx.AsyncClient(timeout=10) as client:
        while time.monotonic() < deadline:
            sim, incidents = await snapshot(client, settings)
            for state in sim.active_scenarios:
                if (
                    state.id.upper() == "S8"
                    and state.active
                    and state.injected_ts != s8_injection
                ):
                    typer.echo(f"S8 triggered at {state.injected_ts}: {state.title}")
                    s8_injection = state.injected_ts
            resolved = False
            for incident in incidents:
                previous = seen.get(incident.id)
                if previous is None or previous != incident:
                    cue = "Incident opened" if previous is None else "Incident updated"
                    typer.echo(
                        f"{cue}: {incident.id} [{incident.status}] {incident.title}: "
                        f"{incident.summary}"
                    )
                    if incident.investigation_ids:
                        typer.echo(
                            "Investigation: " + ", ".join(incident.investigation_ids)
                        )
                    if incident.verdict:
                        typer.echo(f"Verdict: {incident.verdict}")
                if (
                    previous is not None
                    and previous.status not in TERMINAL
                    and incident.status in {"resolved", "closed"}
                ):
                    typer.echo(f"Incident resolved: {incident.id}")
                    resolved = True
                seen[incident.id] = incident
            if until == Until.resolved and resolved:
                return
            await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))
    typer.echo("Narration timeout reached.")
    if until == Until.resolved:
        raise ValueError("No observed incident resolved before timeout")


@app.command()
def narrate(
    ctx: typer.Context,
    interval: Annotated[float, typer.Option(min=0.01)] = 2,
    timeout: Annotated[float, typer.Option(min=0.01)] = 180,
    until: Annotated[Until, typer.Option()] = Until.resolved,
) -> None:
    """Read live cues; exit on an observed resolution or at the timeout."""
    run(follow(ctx.obj, interval, timeout, until))
