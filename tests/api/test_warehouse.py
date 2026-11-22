import socket
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import duckdb
import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eye.config import SentinelSettings, Settings
from eye.main import core_app
from eye.schema import SCHEMA, create_tables
from eye.warehouse import operations as ops
from eye.warehouse.client import DataClient, DataUnavailable
from eye.warehouse.server import Warehouse, create_app

TS = datetime(2026, 11, 27, tzinfo=UTC)
HOUR = timedelta(hours=1)
CONFIG = Path("config").resolve()


def publish_hour(
    con: duckdb.DuckDBPyConnection,
    root: Path,
    ts: datetime,
    sessions: int,
    *,
    loaded: int = 1,
    manifest: bool = True,
) -> None:
    for table in SCHEMA:
        con.execute(f"DELETE FROM {table}")
    con.execute(
        "INSERT INTO traffic_hourly VALUES (?, 'DE', 'web', ?, 0, 0, 0)",
        [ts, sessions],
    )
    con.execute(
        "INSERT INTO partition_status VALUES ('traffic_hourly', 'DE', ?, 1, ?, ?)",
        [ts, loaded, ts + HOUR],
    )
    con.execute(
        "INSERT INTO skus VALUES ('sku-1', 'books', ?, 1, 1, 'supplier-1')",
        [sessions],
    )
    for table in SCHEMA:
        if table == "partition_status" and not manifest:
            continue
        file = root / table / f"{ts:hour=%Y-%m-%dT%H}" / "data.parquet"
        file.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY {table} TO ? (FORMAT PARQUET)", [str(file)])


def test_reads_only_published_hours_and_refreshes(tmp_path: Path, monkeypatch) -> None:
    lake = tmp_path / "lake"
    now = [100.0]
    monkeypatch.setattr("eye.warehouse.landing.monotonic", lambda: now[0])
    with duckdb.connect() as writer, duckdb.connect() as reader:
        create_tables(writer)
        warehouse = Warehouse(
            reader, SentinelSettings(), lake=lake, graph_path=tmp_path / "graph.json"
        )
        with TestClient(create_app(warehouse)) as client:
            assert client.get("/healthz").status_code == 503
            publish_hour(writer, lake, TS, 12)
            publish_hour(writer, lake, TS + HOUR, 23, manifest=False)
            now[0] += 2
            assert client.get("/healthz").status_code == 200
            window = ops.PartitionWindow.model_validate(
                client.get("/v1/partitions").json()
            )
            assert window.earliest == window.latest == TS
            args = ops.SeriesInput(metric="sessions", start=TS, end=TS + 2 * HOUR)
            series = client.post("/v1/series", json=args.model_dump(mode="json"))
            assert series.status_code == 200, series.text
            assert [p["value"] for p in series.json()[0]["points"]] == [12]
            publish_hour(writer, lake, TS + HOUR, 23, loaded=0)
            now[0] += 2
            window = ops.PartitionWindow.model_validate(
                client.get("/v1/partitions").json()
            )
            assert window.latest == TS
            complete = client.post(
                "/v1/complete-hour", json={"ts": (TS + HOUR).isoformat()}
            )
            assert complete.json() is False
            assert reader.sql("SELECT ref_price FROM skus").fetchall() == [(12.0,)]
            publish_hour(writer, lake, TS + HOUR, 25)
            now[0] += 2
            assert reader.sql("SELECT ref_price FROM skus").fetchall() == [(12.0,)]
            window = ops.PartitionWindow.model_validate(
                client.get("/v1/partitions").json()
            )
            assert window.latest == TS + HOUR
            assert reader.sql("SELECT ref_price FROM skus").fetchall() == [(25.0,)]
            publish_hour(writer, lake, TS + 2 * HOUR, 34)
            now[0] += 2
            window = ops.PartitionWindow.model_validate(
                client.get("/v1/partitions").json()
            )
            assert window.latest == TS + 2 * HOUR
            assert reader.sql("SELECT ref_price FROM skus").fetchall() == [(34.0,)]


def test_warehouse_graph_and_validated_queries(tmp_path: Path) -> None:
    with duckdb.connect() as reader:
        create_tables(reader)
        warehouse = Warehouse(
            reader, SentinelSettings(), graph_path=tmp_path / "graph.json"
        )
        with TestClient(create_app(warehouse)) as client:
            graph = client.get("/v1/graph")
            assert graph.status_code == 200, graph.text
            assert any(n["id"] == "sessions" for n in graph.json()["nodes"])
            assert client.get("/v1/graph?node=missing").status_code == 422
            assert (
                client.post("/v1/series", json={"sql": "SELECT 1"}).status_code == 422
            )
            assert client.get("/data/scenario_log").status_code == 404
            long_window = {
                "metric": "sessions",
                "start": TS.isoformat(),
                "end": (TS + timedelta(days=61)).isoformat(),
            }
            assert client.post("/v1/series", json=long_window).status_code == 422


@contextmanager
def live_server(app: FastAPI) -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = monotonic() + 5
            while not server.started and worker.is_alive() and monotonic() < deadline:
                sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            worker.join(timeout=5)


def test_core_reads_from_http_without_opening_warehouse(
    tmp_path: Path, monkeypatch
) -> None:
    lake = tmp_path / "lake"
    now = [100.0]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("eye.warehouse.landing.monotonic", lambda: now[0])
    with duckdb.connect() as writer, duckdb.connect() as reader:
        create_tables(writer)
        publish_hour(writer, lake, TS, 12)
        app = create_app(
            Warehouse(
                reader,
                SentinelSettings(),
                lake=lake,
                config_dir=CONFIG,
                graph_path=tmp_path / "graph.json",
            )
        )

        def reject_local_source(*args: object) -> None:
            raise AssertionError("core must not open the warehouse")

        async def scripted_sentinel(*args: object, **kwargs: object) -> list:
            return []

        monkeypatch.setattr("eye.main.open_source", reject_local_source)
        monkeypatch.setattr("eye.main.Sentinel.run", scripted_sentinel)
        with (
            live_server(app) as url,
            core_app(
                Settings.model_validate(
                    {"sim": {"autostart": False}, "llm": {"mode": "replay"}}
                ),
                data_url=url,
            ) as public_app,
            TestClient(public_app) as core,
        ):
            interval = {"start": TS.isoformat(), "end": (TS + HOUR).isoformat()}
            assert core.get("/api/status").json()["tick"] == 0
            graph = core.get("/api/graph")
            assert graph.status_code == 200, graph.text
            assert any(n["id"] == "sessions" for n in graph.json()["nodes"])
            series = core.get("/api/metrics/sessions", params=interval)
            assert series.status_code == 200, series.text
            assert series.json()["points"][0]["value"] == 12
            response = core.get("/data/changes", params=interval)
            assert response.status_code == 200, response.text
            assert response.json()["rows"] == []
            assert core.post("/api/clock/step").json()["tick"] == 0
            publish_hour(writer, lake, TS + HOUR, 23)
            now[0] += 2
            assert core.post("/api/clock/step").json()["tick"] == 1


def test_client_rejects_an_invalid_response() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"latest": "not-a-timestamp"})
    )
    with (
        closing(DataClient("http://warehouse", transport=transport)) as client,
        pytest.raises(DataUnavailable, match="Invalid warehouse response"),
    ):
        client.partitions()
