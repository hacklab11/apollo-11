"""Atomic hourly publication of public simulator facts."""

import os
from datetime import datetime, timedelta
from pathlib import Path

from eye.schema import SCHEMA
from eye.sim.store import Store

HOUR = timedelta(hours=1)


class LakeWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.last_hour: datetime | None = None

    def land(self, store: Store, hour: datetime) -> None:
        with store.cursor() as con:
            for table in [t for t in SCHEMA if t != "partition_status"] + [
                "partition_status"
            ]:
                columns = {
                    row[1]
                    for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()
                }
                parameters: list[object] = []
                query = f"SELECT * FROM {table}"
                if "ts" in columns:
                    query += " WHERE ts = ?"
                    parameters = [hour]
                destination = (
                    self.root / table / f"hour={hour:%Y-%m-%dT%H}" / "data.parquet"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".parquet.tmp")
                try:
                    con.execute(
                        f"COPY ({query}) TO ? (FORMAT PARQUET)",
                        [*parameters, str(temporary)],
                    )
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
        self.last_hour = max(hour, self.last_hour) if self.last_hour else hour
