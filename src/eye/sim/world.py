"""Seeded catalog and in-memory marketplace state."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

import numpy as np
from numpy.random import Generator

from eye.contracts import Category, Platform, SellerTier
from eye.sim.store import Batches

_CATEGORIES: dict[Category, tuple[str, int, float, float, float]] = {
    "electronics": ("E", 12, 60, 600, 0.35),
    "home": ("H", 10, 20, 150, 0.25),
    "fashion": ("F", 10, 15, 120, 0.25),
    "toys": ("T", 8, 10, 80, 0.15),
}
CATEGORIES = tuple(_CATEGORIES)
CATEGORY_SHARES = tuple(spec[4] for spec in _CATEGORIES.values())
HYPED_SKUS = frozenset({"E11", "F09", "T07"})
_NO_FLOOR = frozenset({"S-03", "S-07", "S-12"})
_AD_SELLERS = ("L-01", "L-02", "L-03", "L-04", "L-05", "S-02", "S-05", "S-09")
APP_COMPONENTS: tuple[tuple[Platform, str, str, str], ...] = (
    ("android", "checkout", "checkout-web", "checkout-web v5.7"),
    ("ios", "checkout", "native", "app-ios v1.0"),
    ("web", "checkout", "checkout-web", "checkout-web v5.7"),
)


@dataclass(frozen=True)
class Sku:
    sku: str
    category: Category
    ref_price: float
    popularity: float
    attractiveness: float
    supplier_id: str
    cogs_ratio: float


@dataclass
class Seller:
    seller_id: str
    tier: SellerTier
    rating: float
    joined_ts: datetime | None


@dataclass
class Offer:
    sku: str
    seller_id: str
    price: float
    stock: int
    repricer_mode: Literal["fixed", "match", "beat"]
    floor: float | None
    cost: float
    inbound: list[tuple[datetime, int]] = field(default_factory=list)
    fba_capacity_week: int | None = None


@dataclass(frozen=True)
class Advertiser:
    advertiser_id: str
    seller_id: str
    category: Category
    daily_budget: float
    base_bid: float


@dataclass
class World:
    skus: dict[str, Sku]
    sellers: dict[str, Seller]
    offers: dict[str, list[Offer]]
    suppliers: tuple[str, ...]
    advertisers: dict[str, Advertiser]

    def static_rows(self) -> Batches:
        return {
            "skus": [
                (
                    s.sku,
                    s.category,
                    s.ref_price,
                    s.popularity,
                    s.attractiveness,
                    s.supplier_id,
                )
                for s in self.skus.values()
            ],
            "sellers": [
                (s.seller_id, s.tier, s.rating, s.joined_ts)
                for s in self.sellers.values()
            ],
            "app_components": list(APP_COMPONENTS),
        }


def _seller_catalogs(rng: Generator, skus: list[str]) -> dict[str, list[str]]:
    electronics = {f"E{i:02d}" for i in range(1, 11)}
    first_party = electronics | {f"H{i:02d}" for i in range(1, 11)}
    catalogs = {"1P": sorted(first_party)}
    shuffled = [skus[i] for i in rng.permutation(len(skus))]
    for i in range(5):
        catalogs[f"L-{i + 1:02d}"] = shuffled[i * 8 : (i + 1) * 8]
    counts = dict.fromkeys(skus, 1)
    for sku in first_party:
        counts[sku] += 1

    # Cover every SKU before adding optional offers, keeping each small seller in range.
    small = [f"S-{i + 1:02d}" for i in rng.permutation(24)]
    catalogs.update({seller_id: [] for seller_id in small})
    slot = 0
    for sku in shuffled:
        minimum = 2 if sku in electronics else 3 - counts[sku]
        for _ in range(minimum):
            catalogs[small[slot % len(small)]].append(sku)
            counts[sku] += 1
            slot += 1

    for seller_id in small:
        catalog = catalogs[seller_id]
        extra = int(rng.integers(3, 5)) - len(catalog)
        eligible = [sku for sku in skus if counts[sku] < 5 and sku not in catalog]
        for i in rng.choice(len(eligible), extra, replace=False):
            sku = eligible[i]
            catalog.append(sku)
            counts[sku] += 1

    return catalogs


def _build_offer(rng: Generator, seller: Seller, sku: Sku) -> Offer:
    mode: Literal["fixed", "match", "beat"] = "match"
    if seller.tier == "1P":
        price = sku.ref_price
        floor = cost = round(sku.ref_price * sku.cogs_ratio, 2)
    else:
        price = round(sku.ref_price * rng.uniform(1, 1.06), 2)
        # The spec leaves third-party procurement costs unspecified.
        cost = round(sku.ref_price * rng.uniform(0.65, 0.80), 2)
        floor = None if seller.seller_id in _NO_FLOOR else round(cost * 1.05, 2)
        if seller.tier == "small_3P" and rng.random() < 0.40:
            mode = "beat"
    # Until T02 sizes and decrements stock, a single unit marks an available offer.
    return Offer(
        sku.sku,
        seller.seller_id,
        price,
        int(sku.sku not in HYPED_SKUS),
        mode,
        floor,
        cost,
    )


def build_world(seed: int, *, joined_ts: datetime) -> World:
    rng = np.random.default_rng(seed)
    suppliers = tuple(f"SUP-{i}" for i in range(1, 7))
    skus: dict[str, Sku] = {}
    for category, (prefix, count, low, high, share) in _CATEGORIES.items():
        weights = np.arange(1, count + 1) ** -1.1
        weights *= share / weights.sum()
        for rank, weight in enumerate(weights, 1):
            sku = f"{prefix}{rank:02d}"
            skus[sku] = Sku(
                sku,
                category,
                round(rng.uniform(low, high), 2),
                float(weight),
                rng.uniform(1.4 if sku in HYPED_SKUS else 0.5, 1.5),
                suppliers[int(rng.integers(len(suppliers)))],
                rng.uniform(0.65, 0.80),
            )
    catalogs = _seller_catalogs(rng, list(skus))
    sellers = {"1P": Seller("1P", "1P", 0.99, joined_ts)}
    for i in range(1, 6):
        seller_id = f"L-{i:02d}"
        sellers[seller_id] = Seller(
            seller_id, "large_3P", rng.uniform(0.93, 0.98), joined_ts
        )
    for i in range(1, 25):
        seller_id = f"S-{i:02d}"
        sellers[seller_id] = Seller(
            seller_id, "small_3P", rng.uniform(0.85, 0.97), joined_ts
        )
    sellers["S-99"] = Seller("S-99", "small_3P", 0.90, None)

    offers: dict[str, list[Offer]] = {sku: [] for sku in skus}
    for seller_id, catalog in catalogs.items():
        for sku in catalog:
            offers[sku].append(_build_offer(rng, sellers[seller_id], skus[sku]))

    advertisers: dict[str, Advertiser] = {}
    for i, seller_id in enumerate(_AD_SELLERS, 1):
        advertiser_id = f"ADV-{i}"
        categories = sorted({skus[sku].category for sku in catalogs[seller_id]})
        category = categories[int(rng.integers(len(categories)))]
        advertisers[advertiser_id] = Advertiser(
            advertiser_id,
            seller_id,
            category,
            round(rng.uniform(400, 3000), 2),
            round(rng.uniform(0.40, 1.60), 2),
        )
    remaining = dict.fromkeys(CATEGORIES, 2)
    assignments: dict[str, Category] = {}

    def assign(index: int) -> bool:
        if index == len(advertisers):
            return True
        adv = list(advertisers.values())[index]
        eligible = {
            skus[sku].category
            for sku in catalogs[adv.seller_id]
            if sku not in HYPED_SKUS
        }
        for category in sorted(eligible, key=lambda c: (c != adv.category, c)):
            if remaining[category]:
                remaining[category] -= 1
                assignments[adv.advertiser_id] = category
                if assign(index + 1):
                    return True
                remaining[category] += 1
        return False

    if not assign(0):
        raise ValueError("seller catalogs cannot support two advertisers per category")
    advertisers = {
        aid: replace(adv, category=assignments[aid]) for aid, adv in advertisers.items()
    }
    return World(skus, sellers, offers, suppliers, advertisers)
