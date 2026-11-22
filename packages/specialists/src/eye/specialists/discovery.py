"""Discovery & Experience specialist: search, feed, checkout, errors, components."""

from pydantic_ai import Tool

from eye.specialists.tools import (
    CheckoutFunnelInput,
    CountryInput,
    DataClient,
    ErrorsInput,
    Evidence,
    FeedExposureInput,
    PaymentsInput,
    Window,
    changes_tool,
    data_tool,
    metric_tool,
)

NAME = "discovery"
TITLE = "Discovery & Experience"
DESCRIPTION = (
    "Search quality, feed exposure, checkout funnel, client errors, components"
)
SKILLS = {
    "search_quality": "Relevance, CTR, conversion by country/category",
    "feed_exposure": "What the For You feed shows",
    "checkout_funnel": "Carts → checkout → payment by platform",
    "client_errors": "Errors by app version and page",
    "components": "Which platform uses which front-end component",
}


def tools(client: DataClient) -> list[Tool[Evidence]]:
    return [
        data_tool(
            client,
            "search_quality",
            "search/quality",
            CountryInput,
            "Search relevance, CTR, and conversion per country and category.",
        ),
        data_tool(
            client,
            "feed_exposure",
            "feed/exposure",
            FeedExposureInput,
            "Top feed SKUs by impressions with clicks, in_stock, and attractiveness.",
        ),
        data_tool(
            client,
            "checkout_funnel",
            "checkout/funnel",
            CheckoutFunnelInput,
            "Hourly carts, checkout_starts, payment_attempts, and successes. Filter "
            "by platform to compare web, android, and the iOS control.",
        ),
        data_tool(
            client,
            "payments",
            "payments",
            PaymentsInput,
            "Hourly payment attempts, successes, success_rate, and latency per PSP.",
        ),
        data_tool(
            client,
            "errors",
            "errors",
            ErrorsInput,
            "Client error counts in the window by platform, app_version, page, and "
            "error_type.",
        ),
        data_tool(
            client,
            "app_components",
            "app_components",
            Window,
            "Which platform loads which front-end component, its source and version.",
        ),
        metric_tool(client),
        changes_tool(client),
    ]
