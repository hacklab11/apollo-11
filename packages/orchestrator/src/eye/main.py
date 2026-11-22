"""Build the deterministic control service, without importing agent code."""

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
from importlib import import_module

import uvicorn
from fastapi import FastAPI

from eye.api.app import create_app
from eye.config import Settings, load_settings
from eye.datasource import ServiceClock, open_source
from eye.incidents.store import IncidentStore
from eye.orchestrator import HOUR, Orchestrator
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.tools.models import BaselineCompareInput
from eye.warehouse import operations as ops
from eye.warehouse.client import DataClient, RemoteGraph
from eye.warehouse.queries import LocalQueries


@contextmanager
def core_app(settings: Settings, *, data_url: str | None = None) -> Iterator[FastAPI]:
    with ExitStack() as stack:
        client = (
            stack.enter_context(closing(DataClient(data_url))) if data_url else None
        )
        local = stack.enter_context(open_source(settings)) if client is None else None
        source = ServiceClock(client) if client is not None else local
        assert source is not None
        incidents = stack.enter_context(
            closing(IncidentStore(settings=settings.reporter))
        )
        if client is not None:
            connection = registry = engine = None
            graph = RemoteGraph(client)
            queries = client
        else:
            assert local is not None
            bootstrap = import_module("eye.graph.bootstrap")
            connection = stack.enter_context(local.cursor())
            registry = load_registry(connection)
            engine = MetricEngine(connection, registry)
            graph = bootstrap.load_or_bootstrap(
                engine, excluded_hours=sorted(incidents.excluded_hours(source.now))
            )
            queries = LocalQueries(
                connection, registry, graph, settings.sentinel, engine
            )
            stack.callback(graph.save)
        incidents.path_exists = graph.path_exists

        def z_lookup(
            name: str, segment: dict[str, str], ts: datetime
        ) -> tuple[float, float]:
            reading = queries.read(
                ops.BASELINE,
                BaselineCompareInput(
                    name=name, start=ts, end=ts + HOUR, segment=segment
                ),
            ).points[0]
            return reading.z if reading.z is not None else float(
                "inf"
            ), reading.completeness

        orchestrator = Orchestrator(
            settings,
            source,
            incidents,
            z_lookup,
            invalidate=engine.invalidate if engine else lambda: None,
        )
        incidents.notify = orchestrator.notify_seller_os
        yield create_app(orchestrator, connection, registry, graph, engine, data=client)


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(message)s", stream=sys.stdout, force=True
    )
    settings = load_settings()
    with core_app(settings, data_url=os.environ.get("EYE_DATA_URL")) as app:
        uvicorn.run(
            app,
            host=os.environ.get("EYE_BIND_HOST", "127.0.0.1"),
            port=settings.ports.core,
            access_log=False,
            log_config=None,
        )
