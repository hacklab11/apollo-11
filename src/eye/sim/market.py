"""Offer selection, inventory and replenishment."""

from collections import defaultdict, deque
from datetime import datetime, timedelta
from math import ceil

from numpy.random import Generator

from eye.sim.store import Batches, Row
from eye.sim.world import HYPED_SKUS, Offer, World


def buy_box(world: World, sku: str) -> Offer | None:
    return min(
        (offer for offer in world.offers[sku] if offer.stock > 0),
        key=lambda o: (
            o.price * (1 + 0.5 * (1 - world.sellers[o.seller_id].rating)),
            -world.sellers[o.seller_id].rating,
            o.seller_id,
        ),
        default=None,
    )


class Market:
    def __init__(self, world: World) -> None:
        self.world = world
        self.sales: dict[tuple[str, str], deque[tuple[datetime, int]]] = defaultdict(
            deque
        )
        self.baseline: dict[tuple[str, str], float] = {}
        self.out_of_stock_hours: dict[tuple[str, str], int] = defaultdict(int)
        self.used: dict[str, int] = defaultdict(int)
        self.blocked: dict[str, int] = defaultdict(int)
        self.week: tuple[int, int] | None = None
        self.pos: list[Row] = []
        self._po_number = 0
        for sku, offers in world.offers.items():
            winner = buy_box(world, sku)
            for offer in offers:
                # Baseline funnel yields about 1,200 orders/day across the catalog.
                daily = max(1.0, 1200 * world.skus[sku].popularity)
                if offer is not winner:
                    daily = max(1.0, daily * 0.1)
                key = (offer.seller_id, sku)
                self.baseline[key] = daily
                tier = world.sellers[offer.seller_id].tier
                offer.stock = (
                    0
                    if sku in HYPED_SKUS
                    else ceil(daily * (10 if tier == "1P" else 3))
                )
                if tier != "1P":
                    offer.fba_capacity_week = ceil(
                        daily * 7 * (3 if tier == "large_3P" else 1.3)
                    )

    def average_sales(self, offer: Offer, ts: datetime) -> float:
        key = (offer.seller_id, offer.sku)
        history = self.sales[key]
        while history and history[0][0] < ts - timedelta(days=7):
            history.popleft()
        if not history:
            return self.baseline[key]
        days = min(7.0, max(1.0, (ts - history[0][0]).total_seconds() / 86400))
        return sum(qty for _, qty in history) / days

    def run_repricers(self) -> None:
        for sku, offers in self.world.offers.items():
            winner = buy_box(self.world, sku)
            prices = {o.seller_id: o.price for o in offers if o.stock > 0}
            updates = []
            ref = self.world.skus[sku].ref_price
            for offer in offers:
                others = [
                    p for seller, p in prices.items() if seller != offer.seller_id
                ]
                if offer.repricer_mode == "fixed" or not others:
                    continue
                lowest = min(others)
                price = offer.price
                floor = offer.floor if offer.floor is not None else 0.40 * ref
                if lowest > price * 1.03 and price < ref:
                    price = min(lowest * 0.99, ref)
                elif lowest < price * 0.97 or (
                    offer.repricer_mode == "beat"
                    and offer is not winner
                    and lowest <= price
                ):
                    price = max(
                        lowest * (0.99 if offer.repricer_mode == "beat" else 1), floor
                    )
                updates.append((offer, round(price, 2)))
            for offer, price in updates:
                offer.price = price

    def before_demand(self, ts: datetime, rng: Generator) -> Batches:
        week = ts.isocalendar()[:2]
        if self.week != week:
            self.used.clear()
            self.week = week
        self.run_repricers()
        for i, po in enumerate(self.pos):
            if po[6] <= ts:
                offer = next(o for o in self.world.offers[po[1]] if o.seller_id == "1P")
                offer.stock += po[7]
        delivered = [po for po in self.pos if po[6] <= ts]
        self.pos = [po for po in self.pos if po[6] > ts]
        # PO rows are published once, when delivery is known (planned at creation).
        del delivered
        if ts.hour == 0:
            self.blocked.clear()
        for sku, offers in self.world.offers.items():
            if sku in HYPED_SKUS:
                continue
            for offer in offers:
                due = sum(qty for arrival, qty in offer.inbound if arrival <= ts)
                offer.stock += due
                offer.inbound[:] = [
                    (arrival, qty) for arrival, qty in offer.inbound if arrival > ts
                ]
        new_pos: list[Row] = []
        for sku, offers in self.world.offers.items():
            if sku in HYPED_SKUS:
                continue
            for offer in offers:
                daily = self.average_sales(offer, ts)
                if offer.seller_id == "1P" and ts.hour == 2:
                    open_qty = sum(po[4] for po in self.pos if po[1] == sku)
                    qty = max(0, ceil(daily * 10 - offer.stock - open_qty))
                    if qty:
                        self._po_number += 1
                        eta = ts + timedelta(hours=48)
                        arrival = eta + timedelta(hours=int(rng.integers(-6, 7)))
                        po = (
                            f"PO-{self._po_number:06d}",
                            sku,
                            self.world.skus[sku].supplier_id,
                            ts,
                            qty,
                            eta,
                            arrival,
                            qty,
                            "purchasing v2.2",
                        )
                        self.pos.append(po)
                        new_pos.append(po)
                elif offer.seller_id != "1P" and ts.hour == 6:
                    inbound = sum(qty for _, qty in offer.inbound)
                    if offer.stock + inbound < daily * 2:
                        wanted = ceil(daily * 7)
                        capacity = sum(
                            o.fba_capacity_week or 0
                            for group in self.world.offers.values()
                            for o in group
                            if o.seller_id == offer.seller_id
                        )
                        qty = min(wanted, max(0, capacity - self.used[offer.seller_id]))
                        self.blocked[offer.seller_id] += wanted - qty
                        self.used[offer.seller_id] += qty
                        if qty:
                            offer.inbound.append((ts + timedelta(hours=48), qty))
        rows = []
        for sku, offers in self.world.offers.items():
            winner = buy_box(self.world, sku)
            for offer in offers:
                key = (offer.seller_id, sku)
                self.out_of_stock_hours[key] = (
                    self.out_of_stock_hours[key] + 1 if offer.stock == 0 else 0
                )
                rows.append(
                    (
                        ts,
                        sku,
                        offer.seller_id,
                        self.world.sellers[offer.seller_id].tier,
                        self.world.skus[sku].category,
                        offer.price,
                        offer.stock,
                        offer.stock > 0,
                        offer is winner,
                    )
                )
        capacity_rows = []
        if ts.hour == 23:
            for seller in self.world.sellers.values():
                if seller.tier == "1P" or seller.seller_id == "S-99":
                    continue
                capacity = sum(
                    o.fba_capacity_week or 0
                    for group in self.world.offers.values()
                    for o in group
                    if o.seller_id == seller.seller_id
                )
                capacity_rows.append(
                    (
                        ts.date(),
                        seller.seller_id,
                        capacity,
                        self.used[seller.seller_id],
                        self.blocked[seller.seller_id],
                    )
                )
        return {
            "offers_hourly": rows,
            "purchase_orders": new_pos,
            "fba_capacity_daily": capacity_rows,
        }

    def record_sales(self, ts: datetime, rows: list[Row]) -> None:
        totals: dict[tuple[str, str], int] = defaultdict(int)
        for row in rows:
            totals[row[5], row[4]] += row[8]
        for key, qty in totals.items():
            self.sales[key].append((ts, qty))
