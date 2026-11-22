from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient

from eye.config import SentinelSettings
from eye.schema import SCHEMA, create_tables
from eye.warehouse import operations as ops
from eye.warehouse.server import Warehouse, create_app

TS = datetime(2026, 11, 27, tzinfo=UTC)
HOUR = timedelta(hours=1)


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
            publish_hour(writer, lake, TS + 2 * HOUR, 34)
            now[0] += 2
            window = ops.PartitionWindow.model_validate(
                client.get("/v1/partitions").json()
            )
            assert window.latest == TS + 2 * HOUR


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
