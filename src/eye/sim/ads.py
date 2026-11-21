"""Sponsored-product auctions and paid acquisition."""

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
from numpy.random import Generator

from eye.sim.demand import _HOUR_PROFILE, COUNTRIES
from eye.sim.store import Batches
from eye.sim.world import CATEGORIES, World


@dataclass
class Ads:
    version: str = "pacing-svc v4.0"
    spent: dict[str, float] = field(default_factory=dict)
    bids: dict[str, float] = field(default_factory=dict)
    out_of_stock_hours: dict[str, int] = field(default_factory=dict)
    recovered_hours: dict[str, int] = field(default_factory=dict)
    day: date | None = None

    def auction(
        self,
        ts: datetime,
        rng: Generator,
        world: World,
        searches: np.ndarray,
        batches: Batches,
    ) -> dict[tuple[int, str], int]:
        if self.day != ts.date():
            self.spent.clear()
            self.day = ts.date()
        planned = sum(_HOUR_PROFILE[: ts.hour + 1]) / sum(_HOUR_PROFILE)
        landed: dict[tuple[int, str], int] = {}
        participants: dict[str, bool] = {}
        for adv in world.advertisers.values():
            hours = self.out_of_stock_hours.get(adv.seller_id, 0)
            bid = self.bids.get(adv.advertiser_id, adv.base_bid)
            if hours > 6:
                bid = adv.base_bid * min(2.5, 1.25 ** ((hours - 1) // 6))
            elif self.recovered_hours.get(adv.seller_id, 0) >= 24:
                bid = adv.base_bid
            self.bids[adv.advertiser_id] = bid
            spent = self.spent.get(adv.advertiser_id, 0.0)
            participants[adv.advertiser_id] = (
                spent < adv.daily_budget
                and rng.random()
                < np.clip(1 - (spent / adv.daily_budget - planned) * 5, 0, 1)
            )
        facts = batches.setdefault("ads_hourly", [])
        for k, category in enumerate(CATEGORIES):
            ranked = sorted(
                (
                    a
                    for a in world.advertisers.values()
                    if a.category == category and participants[a.advertiser_id]
                ),
                key=lambda a: (-self.bids[a.advertiser_id], a.advertiser_id),
            )
            for adv in (
                a for a in world.advertisers.values() if a.category == category
            ):
                aid = adv.advertiser_id
                rank = ranked.index(adv) if adv in ranked else 2
                cpc = (
                    max(0.10, self.bids[ranked[rank + 1].advertiser_id] + 0.01)
                    if rank < 2 and rank + 1 < len(ranked)
                    else 0.10
                )
                for c, country in enumerate(COUNTRIES):
                    segments = searches[c * 3 : c * 3 + 3, k]
                    impressions = round(float(segments.sum()) * 0.9) if rank < 2 else 0
                    clicks = min(
                        int(rng.binomial(impressions, 0.02)),
                        int((adv.daily_budget - self.spent.get(aid, 0)) / cpc),
                    )
                    spend = round(clicks * cpc, 2)
                    self.spent[aid] = round(self.spent.get(aid, 0) + spend, 2)
                    facts.append(
                        (
                            ts,
                            aid,
                            country,
                            adv.seller_id,
                            category,
                            impressions,
                            clicks,
                            spend,
                            cpc if clicks else 0.0,
                            self.bids[aid],
                            adv.daily_budget,
                            self.spent[aid],
                            planned,
                        )
                    )
                    eligible = [
                        sku
                        for sku in world.skus.values()
                        if sku.category == category
                        and any(
                            o.seller_id == adv.seller_id for o in world.offers[sku.sku]
                        )
                    ]
                    if clicks and eligible and segments.sum():
                        allocation = rng.multinomial(clicks, segments / segments.sum())
                        weights = np.array([s.popularity for s in eligible])
                        for p, count in enumerate(allocation):
                            for sku, qty in zip(
                                eligible,
                                rng.multinomial(count, weights / weights.sum()),
                                strict=True,
                            ):
                                key = (c * 3 + p, sku.sku)
                                landed[key] = landed.get(key, 0) + int(qty)
        # spent_today is advertiser-wide, identical on both country rows.
        for index, row in enumerate(facts):
            facts[index] = (*row[:11], self.spent[row[1]], row[12])
        return landed


def marketing(ts: datetime, batches: Batches, multipliers: dict[str, float]) -> None:
    rows = batches.setdefault("marketing_hourly", [])
    for country, share in zip(COUNTRIES, (0.6, 0.4), strict=True):
        sessions = sum(
            r[3] for r in batches.get("traffic_hourly", []) if r[1] == country
        )
        for channel in ("paid_social", "search_ads_external"):
            rows.append(
                (
                    ts,
                    country,
                    channel,
                    1500
                    * _HOUR_PROFILE[ts.hour]
                    * share
                    * multipliers.get(country, 1.0)
                    / 2,
                    round(sessions * 0.1),
                )
            )
