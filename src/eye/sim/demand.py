"""Generate one hour of traffic and order counts without per-shopper draws."""

from dataclasses import dataclass, field
from datetime import datetime
from itertools import product

import numpy as np
from numpy.random import Generator
from numpy.typing import NDArray

from eye.contracts import Channel, Country, Platform
from eye.sim.store import Batches
from eye.sim.world import CATEGORIES, CATEGORY_SHARES, Offer, World

COUNTRIES: tuple[Country, ...] = ("DE", "FR")
PLATFORMS: tuple[Platform, ...] = ("android", "ios", "web")
CHANNELS: tuple[Channel, ...] = ("search", "feed", "other")
PSPS = ("PSP-1", "PSP-2")
_SEGMENTS = tuple(product(COUNTRIES, PLATFORMS))
_BASE_SESSIONS = 20_000 * np.outer([0.60, 0.40], [0.45, 0.35, 0.20]).ravel()
_HOUR_PROFILE = (
    0.25,
    0.15,
    0.10,
    0.08,
    0.08,
    0.12,
    0.25,
    0.45,
    0.60,
    0.65,
    0.68,
    0.70,
    0.75,
    0.72,
    0.70,
    0.70,
    0.75,
    0.82,
    0.90,
    0.97,
    1.00,
    0.95,
    0.75,
    0.45,
)
_WEEKDAY_PROFILE = (1.00, 0.98, 0.97, 1.00, 1.05, 1.15, 1.20)
_RELEVANCE = 0.85


@dataclass
class DemandModifiers:
    campaign_mult: float = 1.0
    marketing_mult: dict[Country, float] = field(default_factory=dict)
    relevance: dict[Country, float] = field(default_factory=dict)
    checkout_ok: dict[Platform, float] = field(default_factory=dict)
    psp_success: dict[str, float] = field(default_factory=dict)
    psp_latency: dict[str, float] = field(default_factory=dict)


def _cheapest_available(offers: list[Offer]) -> Offer | None:
    return min(
        (offer for offer in offers if offer.stock > 0),
        key=lambda offer: (offer.price, offer.seller_id),
        default=None,
    )


def _retry_unavailable(
    rng: Generator, clicks: NDArray[np.int64], available: NDArray[np.bool_]
) -> NDArray[np.int64]:
    retries = rng.binomial(clicks * ~available, 0.30)
    # One retry at the next popularity rank in this category (cyclic).
    return (clicks + np.roll(retries, 1, axis=-1)) * available


def generate_demand(
    ts: datetime, rng: Generator, world: World, modifiers: DemandModifiers
) -> Batches:
    """Return one batch per fact table. Funnel arrays are segment × channel × SKU."""
    count = np.rint(
        _BASE_SESSIONS
        * _HOUR_PROFILE[ts.hour]
        * _WEEKDAY_PROFILE[ts.weekday()]
        * modifiers.campaign_mult
        * np.array(
            [modifiers.marketing_mult.get(country, 1.0) for country, _ in _SEGMENTS]
        )
        * rng.lognormal(0, 0.05, len(_SEGMENTS))
    ).astype(np.int64)
    channels = rng.multinomial(count, [0.55, 0.25, 0.20])
    searches = rng.multinomial(
        np.rint(channels[:, 0] * 1.6).astype(np.int64), CATEGORY_SHARES
    )
    relevance = np.repeat(
        [modifiers.relevance.get(country, _RELEVANCE) for country in COUNTRIES]
        + rng.uniform(-0.01, 0.01, len(COUNTRIES)),
        len(PLATFORMS),
    )
    relevance_factor = relevance[:, None] / _RELEVANCE
    result_clicks = rng.binomial(searches, np.clip(0.45 * relevance_factor, 0, 1))

    skus = list(world.skus.values())
    category_index = np.array([CATEGORIES.index(sku.category) for sku in skus])
    popularity = np.array([sku.popularity for sku in skus])
    attractiveness = np.array([sku.attractiveness for sku in skus])
    offers = [_cheapest_available(world.offers[sku.sku]) for sku in skus]
    available = np.array([offer is not None for offer in offers])
    prices = np.array(
        [
            sku.ref_price if offer is None else offer.price
            for sku, offer in zip(skus, offers, strict=True)
        ]
    )
    price_factor = np.clip(
        (np.array([sku.ref_price for sku in skus]) / prices) ** 1.5, 0.5, 2.0
    )

    exposure = attractiveness * popularity * np.where(available, 1.0, 0.05)
    impressions = rng.multinomial(12 * channels[:, 1], exposure / exposure.sum())
    feed_clicks = rng.binomial(impressions, 0.04 * attractiveness)

    carts = np.zeros((len(_SEGMENTS), len(CHANNELS), len(skus)), dtype=np.int64)
    for k in range(len(CATEGORIES)):
        mask = category_index == k
        in_stock = available[mask]
        weight = popularity[mask] * np.where(in_stock, 1.0, 0.30)
        landed = rng.multinomial(result_clicks[:, k], weight / weight.sum())
        search = _retry_unavailable(rng, landed, in_stock)
        feed = _retry_unavailable(rng, feed_clicks[:, mask], in_stock)
        probability = 0.12 * price_factor[mask]
        carts[:, 0, mask] = rng.binomial(search, probability * relevance_factor)
        carts[:, 1, mask] = rng.binomial(feed, probability)
    if available.any():
        other_weight = popularity * available
        carts[:, 2, :] = rng.multinomial(
            rng.binomial(channels[:, 2], 0.03), other_weight / other_weight.sum()
        )

    checkout_ok = np.array(
        [modifiers.checkout_ok.get(platform, 1.0) for _, platform in _SEGMENTS]
    )
    starts = rng.binomial(carts, 0.75 * checkout_ok[:, None, None])
    attempts = rng.binomial(starts, 0.90)
    psp_1 = rng.binomial(attempts, 0.60)
    psp_attempts = np.stack((psp_1, attempts - psp_1), axis=-1)
    rates = np.array(
        [
            modifiers.psp_success.get("PSP-1", 0.97),
            modifiers.psp_success.get("PSP-2", 0.96),
        ]
    )
    psp_successes = rng.binomial(psp_attempts, rates)
    # Aggregate the very same successes into both payment facts and order facts.
    units = psp_successes.sum(axis=-1)
    psp_attempt_totals = psp_attempts.sum(axis=(1, 2))
    psp_success_totals = psp_successes.sum(axis=(1, 2))
    cart_totals = carts.sum(axis=(1, 2))
    start_totals = starts.sum(axis=(1, 2))
    attempt_totals = attempts.sum(axis=(1, 2))

    batches: Batches = {
        "traffic_hourly": [],
        "search_hourly": [],
        "feed_hourly": [],
        "checkout_hourly": [],
        "payments_hourly": [],
        "orders_hourly": [],
    }
    for i, (country, platform) in enumerate(_SEGMENTS):
        key = (ts, country, platform)
        batches["traffic_hourly"].append((*key, int(count[i]), *channels[i].tolist()))
        batches["search_hourly"].extend(
            (
                *key,
                category,
                int(searches[i, k]),
                int(result_clicks[i, k]),
                float(relevance[i]),
            )
            for k, category in enumerate(CATEGORIES)
        )
        batches["feed_hourly"].extend(
            (
                *key,
                sku.sku,
                int(impressions[i, s]),
                int(feed_clicks[i, s]),
                bool(available[s]),
            )
            for s, sku in enumerate(skus)
        )
        batches["checkout_hourly"].append(
            (*key, int(cart_totals[i]), int(start_totals[i]), int(attempt_totals[i]))
        )
        batches["payments_hourly"].extend(
            (
                *key,
                psp,
                int(psp_attempt_totals[i, j]),
                int(psp_success_totals[i, j]),
                modifiers.psp_latency.get(psp),
            )
            for j, psp in enumerate(PSPS)
        )
        for channel_idx, sku_idx in np.argwhere(units[i]):
            sku = skus[sku_idx]
            offer = offers[sku_idx]
            assert offer is not None
            seller = world.sellers[offer.seller_id]
            qty = int(units[i, channel_idx, sku_idx])
            batches["orders_hourly"].append(
                (
                    *key,
                    sku.category,
                    sku.sku,
                    seller.seller_id,
                    seller.tier,
                    CHANNELS[channel_idx],
                    qty,
                    round(qty * offer.price, 2),
                    None,
                    None,
                    None,
                    None,
                )
            )
    return batches
