"""Publish seller-private state to the isolated SellerOS data file."""

import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from eye.sim.world import World

SELLEROS_PATH = Path("external/selleros/data/sellers.json")


class SellerOSExport:
    def __init__(self, world: World, seed: int, path: Path = SELLEROS_PATH) -> None:
        self.world = world
        self.seed = seed
        self.path = path
        self.history: dict[str, list[dict]] = {}
        self.last_prices: dict[tuple[str, str], float] = {}

    def record_manual_price(
        self, seller_id: str, sku: str, price: float, ts: datetime
    ) -> None:
        self.history.setdefault(seller_id, []).append(
            {"ts": ts.isoformat(), "sku": sku, "price": price, "source": "manual"}
        )
        self.last_prices[seller_id, sku] = price

    def write(self, ts: datetime) -> None:
        sellers = []
        for seller in self.world.sellers.values():
            if seller.tier != "small_3P" or seller.seller_id == "S-99":
                continue
            offers = [
                o
                for group in self.world.offers.values()
                for o in group
                if o.seller_id == seller.seller_id
            ]
            history = self.history.setdefault(seller.seller_id, [])
            for offer in offers:
                key = (seller.seller_id, offer.sku)
                if self.last_prices.get(key) != offer.price:
                    history.append(
                        {"ts": ts.isoformat(), "sku": offer.sku, "price": offer.price}
                    )
                    self.last_prices[key] = offer.price
            first = offers[0]
            sellers.append(
                {
                    "seller_id": seller.seller_id,
                    "customer_of_selleros": True,
                    "repricer": {
                        "mode": first.repricer_mode,
                        "undercut_pct": 1.0,
                        "floor": first.floor,
                        "status": "active",
                    },
                    "skus": [
                        {
                            "sku": o.sku,
                            "unit_cost": o.cost,
                            "stock_policy_days": 3,
                            "supplier_eta_days": 2,
                            "repricer": {"mode": o.repricer_mode, "floor": o.floor},
                        }
                        for o in offers
                    ],
                    "price_history": history,
                }
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", dir=self.path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"generated_for_seed": self.seed, "sellers": sellers}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
