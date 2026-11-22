"""Marketplace specialist: Buy Box, pricing, availability, purchasing, sellers."""

from pydantic_ai import Tool

from eye.specialists.tools import (
    CapacityInput,
    CategoryInput,
    DataClient,
    Evidence,
    NewSellersInput,
    OffersInput,
    changes_tool,
    data_tool,
    metric_tool,
)

NAME = "marketplace"
TITLE = "Marketplace"
DESCRIPTION = "Buy Box, pricing, availability, 1P purchasing, and new sellers"
SKILLS = {
    "buybox": "Who holds the Buy Box and why",
    "pricing": "Price moves and repricer-like patterns",
    "availability": "Stock, stockouts, capacity",
    "purchasing": "1P POs and deliveries",
    "new_sellers": "Recently joined sellers and their offers",
}


def tools(client: DataClient) -> list[Tool[Evidence]]:
    return [
        data_tool(
            client,
            "offers",
            "offers",
            OffersInput,
            "Hourly offers by SKU or category: seller_id, seller_tier, price, stock, "
            "in_stock, is_buybox. Use it for price paths and Buy Box holders.",
        ),
        data_tool(
            client,
            "buybox",
            "buybox",
            CategoryInput,
            "Buy Box wins and share by seller and tier in the window.",
        ),
        data_tool(
            client,
            "stock",
            "stock",
            CategoryInput,
            "In-stock SKUs, stockout SKUs, and hours out of stock per category.",
        ),
        data_tool(
            client,
            "purchase_orders",
            "purchase_orders",
            CategoryInput,
            "1P purchase orders ordered in the window: qty, eta_ts, delivered_ts, "
            "delivered_qty, system_version.",
        ),
        data_tool(
            client,
            "capacity",
            "capacity",
            CapacityInput,
            "Daily FBA capacity, used units, and blocked units per seller.",
        ),
        data_tool(
            client,
            "new_sellers",
            "sellers/new",
            NewSellersInput,
            "Sellers that joined inside the window (and after since, if given), "
            "with their offers in the window.",
        ),
        metric_tool(client),
        changes_tool(client),
    ]
