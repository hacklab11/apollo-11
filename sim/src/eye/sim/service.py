"""Continuous private simulator with an hourly public Parquet lake."""

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_runtime.tracing import instrument_app, setup_telemetry
from eye.config import Settings, load_settings
from eye.sim.lake import HOUR, LakeWriter
from eye.sim.scenarios import ScenarioState
from eye.sim.source import open_sim_source


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIM_")
    host: str = "127.0.0.1"
    port: int = Field(8100, ge=1024, le=65535)
    lake_dir: Path = Path("data/lake")
    world_db: Path = Path("data/world.duckdb")
    seconds_per_sim_hour: float | None = Field(None, gt=0)


class Health(BaseModel):
    status: Literal["ok", "failed"]


class Status(BaseModel):
    sim_clock: datetime
    last_landed_hour: datetime | None
    active_scenarios: list[ScenarioState]


class ScenarioInfo(BaseModel):
    id: str
    title: str


def create_app(
    settings: Settings | None = None, service_settings: ServiceSettings | None = None
) -> FastAPI:
    config = settings or load_settings()
    service = service_settings or ServiceSettings()
    interval = service.seconds_per_sim_hour or config.sim.seconds_per_sim_hour

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock = asyncio.Lock()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim")
        loop = asyncio.get_running_loop()

        async def work[T](function: Callable[..., T], *args: Any) -> T:
            return await loop.run_in_executor(executor, partial(function, *args))

        context = open_sim_source(config, service.world_db)
        try:
            source = await work(context.__enter__)
        except BaseException:
            executor.shutdown(wait=True)
            raise
        writer = LakeWriter(service.lake_dir)
        app.state.source = source
        app.state.writer = writer
        app.state.lock = lock
        app.state.work = work

        def land_history() -> None:
            with source.cursor() as con:
                hours = [
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT ts FROM traffic_hourly ORDER BY ts"
                    ).fetchall()
                ]
            for hour in hours:
                writer.land(source.sim.store, hour)

        def step() -> None:
            hour = source.now
            source.step()
            # S8's partial first hour is repaired by the following tick.
            previous = hour - HOUR
            if writer.last_hour is not None:
                writer.land(source.sim.store, previous)
            writer.land(source.sim.store, hour)

        async def run() -> None:
            while True:
                await asyncio.sleep(interval)
                async with lock:
                    await work(step)

        task = None
        try:
            await work(land_history)
            task = asyncio.create_task(run())
            app.state.task = task
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # Closing is queued behind any in-flight DuckDB work.
            try:
                await work(context.__exit__, None, None, None)
            finally:
                executor.shutdown(wait=True)

    app = FastAPI(title="Eye Simulator", lifespan=lifespan)

    @app.get("/health")
    async def health() -> Health:
        if app.state.task.done():
            raise HTTPException(503, "simulation loop failed")
        return Health(status="ok")

    @app.get("/status")
    async def status() -> Status:
        async with app.state.lock:
            source = app.state.source
            return Status(
                sim_clock=source.now,
                last_landed_hour=app.state.writer.last_hour,
                active_scenarios=source.scenarios.active(),
            )

    @app.get("/scenarios")
    async def scenarios() -> list[ScenarioInfo]:
        async with app.state.lock:
            return [
                ScenarioInfo(id=key.lower(), title=plugin.title)
                for key, plugin in app.state.source.scenarios.plugins.items()
            ]

    async def change(id: str, active: bool) -> list[ScenarioState]:
        async with app.state.lock:
            source = app.state.source
            manager = source.scenarios
            key = id.upper()
            if key not in manager.plugins:
                raise HTTPException(404, "unknown or disabled scenario")

            def apply() -> list[ScenarioState]:
                if active:
                    manager.inject(key)
                else:
                    manager.clear(key)
                source.record_scenario(key, source.now, active)
                return manager.timeline()

            try:
                return await app.state.work(apply)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post("/scenarios/{id}")
    async def trigger(id: str) -> list[ScenarioState]:
        return await change(id, True)

    @app.delete("/scenarios/{id}")
    async def clear(id: str) -> list[ScenarioState]:
        return await change(id, False)

    return app


def serve() -> None:
    setup_telemetry()

    settings = ServiceSettings()
    uvicorn.run(
        instrument_app(create_app(service_settings=settings)),
        host=settings.host,
        port=settings.port,
    )
