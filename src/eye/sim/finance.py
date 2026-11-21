"""Deterministic order accounting and seeded, delayed refunds."""

from collections import defaultdict
from datetime import datetime, timedelta

from numpy.random import Generator

from eye.sim.store import Batches, Row
from eye.sim.world import World

TAKE_RATES = {"electronics": 0.08, "home": 0.15, "fashion": 0.17, "toys": 0.15}


class Finance:
    def __init__(self, world: World) -> None:
        self.world = world
        self.refund_probability: dict[str, float] = {"S-99": 0.35}
        self.platform_discount: dict[str, float] = {}
        self.pending: dict[datetime, list[Row]] = defaultdict(list)

    def book(self, ts: datetime, rng: Generator, rows: list[Row]) -> Batches:
        orders = []
        for row in rows:
            sku = self.world.skus[row[4]]
            units, gmv = row[8:10]
            first_party = row[6] == "1P"
            fee = 0.0 if first_party else round(gmv * TAKE_RATES[sku.category], 2)
            cogs = (
                round(units * sku.ref_price * sku.cogs_ratio, 2) if first_party else 0.0
            )
            discount = round(units * self.platform_discount.get(sku.sku, 0.0), 2)
            orders.append(
                (*row[:10], fee, cogs, discount, units * 5.0 if first_party else 0.0)
            )
            refunds = int(
                rng.binomial(units, self.refund_probability.get(row[5], 0.03))
            )
            if refunds:
                # Group refund counts by lag instead of simulating individual orders.
                counts = rng.multinomial(refunds, [1 / 49] * 49)
                for lag, count in enumerate(counts, 48):
                    if count:
                        refunded_gmv = round(gmv * int(count) / units, 2)
                        due = ts + timedelta(hours=lag)
                        self.pending[due].append(
                            (
                                due,
                                row[1],
                                row[3],
                                row[4],
                                row[5],
                                row[6],
                                int(count),
                                refunded_gmv,
                                round(refunded_gmv * 0.25, 2),
                            )
                        )
        refunds_by_key: dict[tuple, list[float]] = {}
        for row in self.pending.pop(ts, []):
            values = refunds_by_key.setdefault(row[:6], [0, 0.0, 0.0])
            for index, value in enumerate(row[6:]):
                values[index] += value
        return {
            "orders_hourly": orders,
            "refunds_hourly": [
                (*key, int(values[0]), round(values[1], 2), round(values[2], 2))
                for key, values in refunds_by_key.items()
            ],
        }
