"""Seller price typo with a planned coupon-day confounder."""

import json
from datetime import datetime, timedelta
from typing import Any, Literal

from eye.sim.changelog import add_change
from eye.sim.engine import Hour, Simulator
from eye.sim.store import Batches
from eye.sim.world import Offer


class S8:
    id = "s8"
    title = "Price typo: the €9 headphones"

    def __init__(self, sim: Simulator, params: dict[str, Any]) -> None:
        self.sim = sim
        self.params = params
        self.start_ts: datetime | None = None
        self.typo_ts: datetime | None = None
        self.original: list[tuple[Offer, float, Literal["fixed", "match", "beat"]]] = []
        self.discounts = sim.finance.platform_discount.copy()
        self.late: tuple[datetime, Batches] | None = None
        self.campaign_started = False
        self.runs = 0

    def offers(self) -> list[Offer]:
        sim = self.sim
        eligible = {
            seller.seller_id: [
                offer
                for sku, offers in sim.world.offers.items()
                if sku in {f"E{i:02d}" for i in range(1, 11)}
                for offer in offers
                if offer.seller_id == seller.seller_id
            ]
            for seller in sim.world.sellers.values()
            if seller.tier == "small_3P"
            and seller.seller_id not in ("S-03", "S-07", "S-12", "S-99")
        }
        seller_id = min(
            eligible,
            key=lambda seller: (
                -len(eligible[seller]),
                -sum(sim.world.skus[o.sku].popularity for o in eligible[seller]),
                seller,
            ),
            default=None,
        )
        if seller_id is None or not eligible[seller_id]:
            raise ValueError("S8 requires an eligible electronics offer")
        return eligible[seller_id]

    def start(self, ts: datetime) -> None:
        self.offers()
        delay = self.params.get("typo_delay_hours", 1)
        if not isinstance(delay, int) or delay < 0:
            raise ValueError("typo_delay_hours must be a nonnegative integer")
        self.start_ts = ts
        self.typo_ts = ts + timedelta(hours=delay)
        self.campaign_started = False
        self.runs += 1
        if ts == self.sim.now:
            self._campaign()
            if self.typo_ts == ts:
                self._typo()

    def _log(self, action: str) -> None:
        self.sim.store.append(
            {
                "scenario_log": [
                    (
                        f"s8-{self.runs}-{action}-{self.sim.now.isoformat()}",
                        self.sim.now,
                        "S8",
                        action,
                        json.dumps(self.params),
                    )
                ]
            }
        )

    def _campaign(self) -> None:
        self.campaign_started = True
        batches: Batches = {}
        add_change(
            batches,
            self.start_ts,
            "growth",
            kind="campaign",
            planned=True,
            scope={"category": "electronics"},
            description="coupon-day-electronics",
            expected_effect={
                "platform_discounts": "10% of electronics reference-price GMV",
                "duration_hours": 24,
            },
        )
        row = batches["change_log"][0]
        identifier = (
            "coupon-day-electronics"
            if self.runs == 1
            else f"coupon-day-electronics-{self.runs}"
        )
        batches["change_log"] = [(identifier, *row[1:])]
        self.sim.store.append(batches)
        self._log("inject")
        self._discount()

    def _discount(self) -> None:
        self.sim.finance.platform_discount = self.discounts.copy()
        if (
            self.start_ts is not None
            and self.start_ts <= self.sim.now < self.start_ts + timedelta(hours=24)
        ):
            for sku in self.sim.world.skus.values():
                if sku.category == "electronics":
                    self.sim.finance.platform_discount[sku.sku] = sku.ref_price * 0.1

    def _typo(self) -> None:
        sim, p = self.sim, self.params
        offers = self.offers()
        p["seller_id"] = offers[0].seller_id
        for offer in offers:
            self.original.append((offer, offer.price, offer.repricer_mode))
            start = sim.now - timedelta(days=7)
            with sim.store.cursor() as con:
                units = con.execute(
                    "SELECT sum(units) FROM orders_hourly WHERE sku = ? AND ts >= ? AND ts < ?",
                    [offer.sku, start, start + timedelta(hours=p["stock_hours"])],
                ).fetchone()[0]
            if units is None:
                units = (
                    sum(
                        sim.market.average_sales(o, sim.now)
                        for o in sim.world.offers[offer.sku]
                    )
                    * p["stock_hours"]
                    / 24
                )
            offer.stock = max(1, round(units * 2))
            offer.price = round(offer.price * p["price_mult"], 2)
            offer.repricer_mode = "fixed"
            sim.selleros_export.record_manual_price(
                offer.seller_id, offer.sku, offer.price, sim.now
            )
        self._log("typo")

    def stop(self) -> None:
        for offer, price, mode in self.original:
            offer.price = price
            offer.repricer_mode = mode
        self.original.clear()
        self.start_ts = None
        self.typo_ts = None
        self.sim.finance.platform_discount = self.discounts.copy()
        self._log("clear")

    def before(self, sim: Simulator, hour: Hour) -> None:
        if self.start_ts is None or hour.ts < self.start_ts:
            return
        if not self.campaign_started:
            self._campaign()
        self._discount()
        if hour.ts == self.typo_ts and not self.original:
            self._typo()

    def before_store(self, sim: Simulator, hour: Hour) -> None:
        if self.late is not None:
            ts, batches = self.late
            sim.store.append(batches)
            with sim.store.cursor() as con:
                con.execute(
                    "UPDATE partition_status SET loaded_rows = expected_rows WHERE table_name = 'orders_hourly' AND ts = ?",
                    [ts],
                )
            self.late = None
        if hour.ts != self.typo_ts or not self.original:
            return
        rows = hour.batches["orders_hourly"]
        missing: list[tuple] = []
        loaded: list[tuple] = []
        counts = {}
        for country in ("DE", "FR"):
            selected = [r for r in rows if r[1] == country]
            count = round(len(selected) * 0.5)
            counts[country] = count
            loaded.extend(selected[:count])
            missing.extend(selected[count:])
        hour.batches["orders_hourly"] = loaded
        hour.batches["partition_status"] = [
            (*r[:4], counts[r[1]], hour.ts + timedelta(hours=1))
            if r[0] == "orders_hourly"
            else r
            for r in hour.batches["partition_status"]
        ]
        self.late = (hour.ts, {"orders_hourly": missing})
