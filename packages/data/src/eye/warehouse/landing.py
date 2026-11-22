"""Expose only manifest-published Parquet hours through DuckDB views."""

from datetime import datetime
from pathlib import Path
from time import monotonic

import duckdb

from eye.schema import SCHEMA


class ParquetLanding:
    def __init__(self, root: Path, connection: duckdb.DuckDBPyConnection) -> None:
        self.root = root.resolve()
        self.connection = connection
        self._hours: tuple[str, ...] = ()
        self._last_refresh = float("-inf")
        self._initialized = False

    def refresh(self) -> bool:
        now = monotonic()
        if now - self._last_refresh < 1:
            return False
        hours = tuple(
            sorted(
                path.parent.name
                for path in (self.root / "partition_status").glob("hour=*/data.parquet")
                if _is_hour(path.parent.name)
            )
        )
        if self._initialized and hours == self._hours:
            self._last_refresh = now
            return False

        # A manifest is the commit marker. Do not glob an individual fact table.
        self.connection.begin()
        try:
            for table, columns in SCHEMA.items():
                if hours:
                    paths = [
                        str(self.root / table / hour / "data.parquet") for hour in hours
                    ]
                    self.connection.read_parquet(
                        paths, hive_partitioning=False
                    ).create_view(table, replace=True)
                else:
                    fields = [
                        f"CAST(NULL AS {dtype}) AS {name}"
                        for name, dtype in (col.split() for col in columns.split(","))
                    ]
                    self.connection.execute(
                        f"CREATE OR REPLACE VIEW {table} AS SELECT {', '.join(fields)} WHERE false"
                    )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._hours = hours
        self._last_refresh = now
        self._initialized = True
        return True


def _is_hour(path: str) -> bool:
    try:
        ts = datetime.strptime(path, "hour=%Y-%m-%dT%H")
    except ValueError:
        return False
    return ts.strftime("hour=%Y-%m-%dT%H") == path
