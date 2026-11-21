"""Partition completeness and latency metadata."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from eye.sim.demand import COUNTRIES
from eye.sim.store import Batches

_COUNTRY_INDEX = {"ads_hourly": 2}


@dataclass
class Pipeline:
    completeness: dict[tuple[str, str], float] = field(default_factory=dict)
    delay_hours: dict[tuple[str, str], float] = field(default_factory=dict)

    def partitions(self, ts: datetime, batches: Batches) -> None:
        partitions = []
        for table, rows in batches.items():
            if table in ("partition_status", "scenario_log"):
                continue
            index = _COUNTRY_INDEX.get(table, 1)
            for country in COUNTRIES:
                # Global tables are replicated metadata, not arbitrarily assigned to a country.
                expected = sum(
                    1
                    for row in rows
                    if len(row) <= index
                    or row[index] not in COUNTRIES
                    or row[index] == country
                )
                key = (table, country)
                share = self.completeness.get(key, 1.0)
                delay = self.delay_hours.get(key, 1.0)
                if not 0 <= share <= 1 or delay < 0:
                    raise ValueError(
                        "partition completeness must be 0..1 and delay nonnegative"
                    )
                partitions.append(
                    (
                        table,
                        country,
                        ts,
                        expected,
                        round(expected * share),
                        ts + timedelta(hours=delay),
                    )
                )
        batches["partition_status"] = partitions
