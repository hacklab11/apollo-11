"""Clock and per-hour simulation hooks."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from numpy.random import Generator, default_rng

from eye.config import Settings
from eye.sim.ads import Ads, marketing
from eye.sim.apps import Apps
from eye.sim.changelog import noise
from eye.sim.demand import BASE_PSP_LATENCY_MS, PSPS, DemandModifiers, generate_demand
from eye.sim.finance import Finance
from eye.sim.market import Market
from eye.sim.pipeline import Pipeline
from eye.sim.seller_actions import (
    JournalSellerActionFeed,
    SellerAction,
    SellerActionFeed,
)
from eye.sim.selleros_export import SellerOSExport
from eye.sim.store import WORLD_DB, Batches, Store
from eye.sim.world import build_world

_HOUR = timedelta(hours=1)


@dataclass
class Hour:
    ts: datetime
    rng: Generator
    demand: DemandModifiers = field(default_factory=DemandModifiers)
    batches: Batches = field(default_factory=dict)


type Hook = Callable[[Simulator, Hour], None]


class Simulator:
    def __init__(
        self,
        settings: Settings,
        *,
        db_path: str | Path = WORLD_DB,
        seller_action_feed: SellerActionFeed | None = None,
    ) -> None:
        self.settings = settings
        self.now = settings.sim.start_ts
        self._start = self.now
        self._history_start = self._start - timedelta(days=settings.sim.history_days)
        self._next_history = self._history_start
        self.world = build_world(settings.sim.seed, joined_ts=self._history_start)
        self.ads = Ads()
        self.apps = Apps()
        self.pipeline = Pipeline()
        self.market = Market(self.world)
        self.finance = Finance(self.world)
        self.selleros_export = SellerOSExport(self.world, settings.sim.seed)
        self.seller_action_feed = seller_action_feed
        self._journal_feed = JournalSellerActionFeed(
            self.selleros_export.path.with_name("actions.jsonl"),
            core_port=settings.ports.core,
        )
        self._seller_action_keys: set[str] = set()
        self.pre_demand_hooks: list[Hook] = []
        self.post_demand_hooks: list[Hook] = []
        self.pre_store_hooks: list[Hook] = []
        self.store = Store(db_path)
        try:
            with self.store.cursor() as reader:
                if reader.execute("SELECT 1 FROM skus LIMIT 1").fetchone():
                    raise ValueError("simulator requires a fresh database")
            self.store.append(self.world.static_rows())
        except BaseException:
            self.store.close()
            raise

    def backfill(self) -> None:
        if self._next_history == self._start:
            self.selleros_export.write(self.now)
            return
        if self.now != self._start:
            raise ValueError("backfill must run before stepping past start_ts")
        try:
            while self._next_history < self._start:
                self.now = self._next_history
                self.step()
                self._next_history = self.now
        finally:
            self.now = self._start
        self.selleros_export.write(self.now)

    def step(self, *, seller_actions: list[SellerAction] | None = None) -> None:
        hour_index = (self.now - self._history_start) // _HOUR
        hour = Hour(self.now, default_rng(self.settings.sim.seed + hour_index))
        ads_rng = default_rng([self.settings.sim.seed, hour_index, 1])
        for hook in self.pre_demand_hooks:
            hook(self, hour)
        feed: SellerActionFeed | None = None
        if seller_actions is None:
            self._journal_feed.path = self.selleros_export.path.with_name(
                "actions.jsonl"
            )
            feed = (
                self.seller_action_feed
                if self.seller_action_feed is not None
                else self._journal_feed
            )
            seller_actions = feed.pending() if hour.ts >= self._start else []
        actions = list(
            {
                action.key: action
                for action in seller_actions
                if action.key not in self._seller_action_keys
            }.values()
        )
        applied_actions = self.market.apply_seller_actions(
            [
                action
                for action in actions
                if action.expected_changed_at is None
                or self.selleros_export.is_current_manual_change(
                    action.seller_id, action.sku, action.expected_changed_at
                )
            ]
        )
        applied_keys = {action.key for action in applied_actions}
        # Replay must not turn a rejected conditional revert into an unconditional one.
        replay_actions = [
            action
            for action in actions
            if action.expected_changed_at is None or action.key in applied_keys
        ]
        if replay_actions:
            hour.batches["seller_actions"] = [
                (
                    action.key,
                    hour.ts,
                    index,
                    action.seller_id,
                    action.sku,
                    action.kind,
                    action.price,
                )
                for index, action in enumerate(replay_actions)
            ]
        for psp_index, psp in enumerate(PSPS):
            latency_rng = default_rng(
                [self.settings.sim.seed, hour_index, 2, psp_index]
            )
            hour.demand.psp_latency.setdefault(
                psp,
                BASE_PSP_LATENCY_MS
                * float(latency_rng.lognormal(-(0.05**2) / 2, 0.05)),
            )
        for table, rows in self.market.before_demand(hour.ts, hour.rng).items():
            hour.batches.setdefault(table, []).extend(rows)
        for adv in self.world.advertisers.values():
            seller = adv.seller_id
            hours = max(
                (
                    duration
                    for (
                        seller_id,
                        _,
                    ), duration in self.market.out_of_stock_hours.items()
                    if seller_id == seller
                ),
                default=0,
            )
            self.ads.out_of_stock_hours[seller] = hours
            self.ads.recovered_hours[seller] = (
                0 if hours else self.ads.recovered_hours.get(seller, 0) + 1
            )
        for table, rows in generate_demand(
            hour.ts,
            hour.rng,
            self.world,
            hour.demand,
            lambda searches: self.ads.auction(
                hour.ts, ads_rng, self.world, searches, hour.batches
            ),
        ).items():
            hour.batches.setdefault(table, []).extend(rows)
        for hook in self.post_demand_hooks:
            hook(self, hour)
        self.market.record_sales(hour.ts, hour.batches.get("orders_hourly", []))
        hour.batches.update(
            self.finance.book(hour.ts, hour.rng, hour.batches.get("orders_hourly", []))
        )
        marketing(
            hour.ts,
            hour.batches,
            hour.demand.marketing_spend_mult or hour.demand.marketing_mult,
        )
        self.apps.errors(hour.ts, hour.rng, hour.batches)
        noise(hour.ts, hour.rng, hour.batches)
        self.pipeline.partitions(hour.ts, hour.batches)
        for hook in self.pre_store_hooks:
            hook(self, hour)
        self.store.append(hour.batches)
        self._seller_action_keys.update(action.key for action in actions)
        for action in applied_actions:
            if action.kind == "set_price":
                assert action.price is not None
                self.selleros_export.record_manual_price(
                    action.seller_id, action.sku, action.price, hour.ts
                )
        self.now += _HOUR
        if feed is not None:
            feed.ack([action.key for action in seller_actions])
        self.selleros_export.write(hour.ts)

    def close(self) -> None:
        self.store.close()
