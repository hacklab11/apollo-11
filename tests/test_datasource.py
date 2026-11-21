"""Warehouse contract and real-source clock behavior."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from eye.config import Settings
from eye.datasource import DuckDBSource, open_source
from eye.schema import TABLES, create_tables, validate_schema
from eye.sim.store import Store

HOUR = timedelta(hours=1)
TS = datetime(2026, 11, 27, tzinfo=UTC)


def test_schema_on_sim_world() -> None:
    store = Store(":memory:")
    try:
        with store.cursor() as con:
            validate_schema(con)
        assert "scenario_log" not in TABLES
    finally:
        store.close()


def test_schema_reports_all_errors() -> None:
    with duckdb.connect() as con:
        create_tables(con)
        con.execute("DROP TABLE sellers")
        con.execute("ALTER TABLE skus DROP COLUMN category")
        con.execute("ALTER TABLE skus ALTER COLUMN ref_price TYPE VARCHAR")
        with pytest.raises(ValueError) as error:
            validate_schema(con)
        assert "missing table sellers" in str(error.value)
        assert "missing column skus.category" in str(error.value)
        assert "mistyped column skus.ref_price: expected DOUBLE, got VARCHAR" in str(
            error.value
        )


def partition(con: duckdb.DuckDBPyConnection, ts: datetime, loaded: int) -> None:
    con.execute(
        "INSERT INTO partition_status VALUES ('traffic_hourly', 'DE', ?, 10, ?, ?)",
        [ts, loaded, ts + HOUR],
    )


def test_source_clock_and_readers(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as con:
        create_tables(con)
        partition(con, TS, 10)
        partition(con, TS + HOUR, 9)
        partition(con, TS + 2 * HOUR, 10)
    source = DuckDBSource(path)
    try:
        assert source.now == TS + 3 * HOUR
        source.step()
        assert source.now == TS + 3 * HOUR
        assert source.scenarios is None
        assert source.scenario_titles == {}
        with pytest.raises(RuntimeError, match="not supported"):
            source.record_scenario("S1", TS, True)
        # A same-process producer is used here to exercise fresh catalog reads.
        with duckdb.connect(str(path), read_only=True) as producer:
            producer.execute("ATTACH ':memory:' AS staging")
        with source.cursor() as reader:
            assert (
                reader.execute("SELECT count(*) FROM partition_status").fetchone()[0]
                == 3
            )
    finally:
        source.close()
    with duckdb.connect(str(path)) as con:
        partition(con, TS + 3 * HOUR, 9)
    source = DuckDBSource(path)
    try:
        source.step()
        assert source.now == TS + 3 * HOUR
    finally:
        source.close()


def test_step_advances_only_one_hour(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as con:
        create_tables(con)
        partition(con, TS, 10)
        partition(con, TS + HOUR, 10)
    source = DuckDBSource(path)
    try:
        source.now = TS
        source.step()
        assert source.now == TS + HOUR
        source.step()
        assert source.now == TS + 2 * HOUR
        source.step()
        assert source.now == TS + 2 * HOUR
    finally:
        source.close()


@pytest.mark.parametrize("valid_schema", [True, False])
def test_rejects_empty_or_invalid_warehouse(tmp_path: Path, valid_schema: bool) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as con:
        if valid_schema:
            create_tables(con)
    with pytest.raises(
        ValueError, match="no fully loaded hour" if valid_schema else "missing table"
    ):
        DuckDBSource(path)


def test_duckdb_factory_without_sim_imports(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as con:
        create_tables(con)
        partition(con, TS, 10)
    settings = Settings.model_validate(
        {"data": {"source": "duckdb", "path": str(path)}}
    )
    with open_source(settings) as source:
        assert isinstance(source, DuckDBSource)
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from eye.config import Settings
from eye.datasource import open_source
with open_source(Settings.model_validate({'data': {'source': 'duckdb', 'path': sys.argv[1]}})) as source:
    assert source.scenarios is None
assert not any(name == 'eye.sim' or name.startswith('eye.sim.') for name in sys.modules)
""",
            str(path),
        ],
        check=True,
    )


def test_sim_factory_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from eye import datasource

    @contextmanager
    def open_sim(settings: Settings):
        yield settings

    def import_source(name: str, *, package: str):
        assert name == ".sim.source"
        assert package == "eye"
        return SimpleNamespace(open_sim_source=open_sim)

    monkeypatch.setattr(datasource, "import_module", import_source)
    settings = Settings()
    with open_source(settings) as source:
        assert source is settings
