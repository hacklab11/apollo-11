"""Refresh a local DuckDB snapshot from manifest-published Parquet hours."""

from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import duckdb

from eye.schema import SCHEMA, create_tables

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
        self._manifests: dict[str, int] = {}
        self._snapshot: tuple[str, int] | None = None
        self._last_refresh = float("-inf")
        create_tables(connection)

    def refresh(self) -> bool:
        now = monotonic()
        if now - self._last_refresh < 1:
            return False
        manifests = self._published_manifests()
        if manifests == self._manifests:
            self._last_refresh = now
            return False

        changed = sorted(
            hour
            for hour, stamp in manifests.items()
            if self._manifests.get(hour) != stamp
        )
        replaced = (self._manifests.keys() - manifests.keys()) | (
            self._manifests.keys() & changed
        )
        timestamps = [
            datetime.strptime(hour, _HOUR_FORMAT).replace(tzinfo=UTC)
            for hour in replaced
        ]
        # Copy on publication so queries do not rescan per-hour files or see
        # new fact files before their manifest is replaced.
        self.connection.begin()
        try:
            for table in sorted(_HOURLY_TABLES):
                if timestamps:
                    self.connection.execute(
                        f"DELETE FROM {table} WHERE ts IN (SELECT unnest(?))",
                        [timestamps],
                    )
                self._insert(table, changed)
            [(latest,)] = self.connection.execute(
                f"SELECT max(ts) FROM ({COMPLETE_HOURS})"
            ).fetchall()
            snapshot = None
            if latest is not None:
                hour = latest.astimezone(UTC).strftime(_HOUR_FORMAT)
                snapshot = (hour, manifests[hour])
            if snapshot != self._snapshot:
                for table in SCHEMA.keys() - _HOURLY_TABLES:
                    self.connection.execute(f"DELETE FROM {table}")
                    self._insert(table, [snapshot[0]] if snapshot else [])
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._manifests = manifests
        self._snapshot = snapshot
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

    def _insert(self, table: str, hours: list[str]) -> None:
        if hours:
            paths = [str(self.root / table / hour / "data.parquet") for hour in hours]
            self.connection.execute(
                f"INSERT INTO {table} BY NAME SELECT * FROM read_parquet(?, hive_partitioning=false)",
                [paths],
            )


def _is_hour(path: str) -> bool:
    try:
        ts = datetime.strptime(path, _HOUR_FORMAT)
    except ValueError:
        return False
    return ts.strftime(_HOUR_FORMAT) == path
