"""DuckDB schema and atomic batch writes for simulator data."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock
from typing import Any

import duckdb

from eye.schema import SCHEMA

WORLD_DB = Path("data/world.duckdb")

_SCHEMA = {
    **SCHEMA,
    "scenario_log": "id VARCHAR, ts TIMESTAMPTZ, scenario_id VARCHAR, action VARCHAR, params JSON",
    "seller_actions": """action_key VARCHAR PRIMARY KEY, ts TIMESTAMPTZ, ordinal BIGINT,
                         seller_id VARCHAR, sku VARCHAR, kind VARCHAR, price DOUBLE""",
}
TABLES = tuple(_SCHEMA)

type Row = tuple[Any, ...]
type Batches = dict[str, list[Row]]


class Store:
    def __init__(self, path: str | Path = WORLD_DB) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._con = duckdb.connect(str(path))
        self._con.execute("SET TimeZone = 'UTC'")
        self._con.execute(
            ";".join(
                f"CREATE TABLE IF NOT EXISTS {table} ({columns})"
                for table, columns in _SCHEMA.items()
            )
        )

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def append(self, batches: Mapping[str, Sequence[Row]]) -> None:
        """Publish a whole tick at once; readers never see a partial hour."""
        if unknown := batches.keys() - _SCHEMA.keys():
            raise ValueError(f"unknown tables: {', '.join(sorted(unknown))}")
        with self._lock:
            self._con.begin()
            try:
                for table, rows in batches.items():
                    if not rows:
                        continue
                    if table == "purchase_orders":
                        self._con.execute(
                            "DELETE FROM purchase_orders WHERE po_id IN (SELECT unnest(?))",
                            [[row[0] for row in rows]],
                        )
                    columns = [list(column) for column in zip(*rows, strict=True)]
                    values = ", ".join("unnest(?)" for _ in columns)
                    self._con.execute(f"INSERT INTO {table} SELECT {values}", columns)
                self._con.commit()
            except BaseException:
                self._con.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._con.close()
