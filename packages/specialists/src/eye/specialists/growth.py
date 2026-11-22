"""Growth specialist: ads pacing, auctions, marketing traffic, and discounts."""

from eye.llm.runtime import ToolSpec
from eye.specialists.tools import (
    AdsPacingInput,
    CategoryInput,
    CountryInput,
    DataClient,
    changes_tool,
    data_tool,
    metric_tool,
)

NAME = "growth"
TITLE = "Growth"
DESCRIPTION = "Ads pacing, auction health, marketing traffic, and platform discounts"
SKILLS = {
    "ads_pacing": "Is an advertiser's spend on schedule",
    "auction_health": "CPC, bidders, slot fill",
    "traffic_sources": "Marketing spend and attributed sessions",
    "discounts": "Platform-funded discounts",
}


def tools(client: DataClient) -> list[ToolSpec]:
    return [
        data_tool(
            client,
            "ads_pacing",
            "ads/pacing",
            AdsPacingInput,
            "Hourly ad spend per advertiser: spent_today, daily_budget, "
            "planned_cum_share, planned_spend, and participating. Use it to compare "
            "spend with the cumulative schedule.",
        ),
        data_tool(
            client,
            "ads_auction",
            "ads/auction",
            CategoryInput,
            "Hourly auction health per category: bidders, avg_bid, cpc, slots, "
            "slot_fill_rate, top_advertisers.",
        ),
        data_tool(
            client,
            "marketing",
            "marketing",
            CountryInput,
            "Hourly marketing spend and attributed sessions per country and channel.",
        ),
        data_tool(
            client,
            "discounts",
            "discounts",
            CategoryInput,
            "Hourly platform-funded discount EUR per category and SKU.",
        ),
        metric_tool(client),
        changes_tool(client),
    ]
