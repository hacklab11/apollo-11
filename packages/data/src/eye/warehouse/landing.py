"""Expose only manifest-published Parquet hours through DuckDB views."""

from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import duckdb

from eye.schema import SCHEMA

COMPLETE_HOURS = (
    "SELECT ts FROM partition_status GROUP BY ts "
    "HAVING bool_and(coalesce(loaded_rows >= expected_rows, false))"
)
_HOUR_FORMAT = "hour=%Y-%m-%dT%H"
_HOURLY_TABLES = {
    table
    for table, columns in SCHEMA.items()
    if any(column.split()[0] == "ts" for column in columns.split(","))
}


class ParquetLanding:
    def __init__(self, root: Path, connection: duckdb.DuckDBPyConnection) -> None:
        self.root = root.resolve()
        self.connection = connection
        self._manifests: dict[str, int] | None = None
        self._last_refresh = float("-inf")

    def refresh(self) -> bool:
        now = monotonic()
        if now - self._last_refresh < 1:
            return False
        manifests = self._published_manifests()
        if manifests == self._manifests:
            self._last_refresh = now
            return False

        hours = tuple(sorted(manifests))
        # A manifest is the commit marker. Do not glob an individual fact table.
        self.connection.begin()
        try:
            self._view("partition_status", hours)
            [(latest,)] = self.connection.execute(
                f"SELECT max(ts) FROM ({COMPLETE_HOURS})"
            ).fetchall()
            snapshot = (
                (latest.astimezone(UTC).strftime(_HOUR_FORMAT),) if latest else ()
            )
            for table in SCHEMA:
                if table != "partition_status":
                    self._view(table, hours if table in _HOURLY_TABLES else snapshot)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._manifests = manifests
        self._last_refresh = now
        return True

    def _published_manifests(self) -> dict[str, int]:
        manifests = {}
        for path in (self.root / "partition_status").glob("hour=*/data.parquet"):
            if _is_hour(hour := path.parent.name):
                try:
                    manifests[hour] = path.stat().st_mtime_ns
                except FileNotFoundError:
                    continue
        return manifests

    def _view(self, table: str, hours: tuple[str, ...]) -> None:
        if hours:
            paths = [str(self.root / table / hour / "data.parquet") for hour in hours]
            self.connection.read_parquet(paths, hive_partitioning=False).create_view(
                table, replace=True
            )
        else:
            fields = [
                f"CAST(NULL AS {dtype}) AS {name}"
                for name, dtype in (col.split() for col in SCHEMA[table].split(","))
            ]
            self.connection.execute(
                f"CREATE OR REPLACE VIEW {table} AS SELECT {', '.join(fields)} WHERE false"
            )


def _is_hour(path: str) -> bool:
    try:
        ts = datetime.strptime(path, _HOUR_FORMAT)
    except ValueError:
        return False
    return ts.strftime(_HOUR_FORMAT) == path
