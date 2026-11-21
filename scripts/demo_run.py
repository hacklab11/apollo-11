"""Drive the Black Friday rehearsal through the public API."""

import argparse
import asyncio
import math
import sys
from typing import Literal

import httpx
from pydantic import ValidationError

from eye.contracts import AddMetricRequest, AddMetricResult, ScenarioId, ScenarioState
from eye.orchestrator import Status

REFUND_RATE = AddMetricRequest(
    name="refund_rate",
    description="Number of refunds divided by units ordered in the same hour.",
    unit="ratio",
    sql="""
        SELECT ts, country, category, seller_tier, sum(num) AS num, sum(den) AS den
        FROM (
            SELECT ts, country, category, seller_tier, refunds AS num, 0 AS den
            FROM refunds_hourly
            UNION ALL
            SELECT ts, country, category, seller_tier, 0 AS num, units AS den
            FROM orders_hourly
        ) AS counts
        GROUP BY ts, country, category, seller_tier
    """.strip(),
)


def say(line: str) -> None:
    print(line, flush=True)


async def request(
    client: httpx.AsyncClient, method: str, path: str, *, json: dict | None = None
) -> httpx.Response:
    response = await client.request(method, path, json=json)
    return response.raise_for_status()


async def set_scenario(
    client: httpx.AsyncClient,
    scenario: ScenarioId,
    action: Literal["inject", "clear"] = "inject",
) -> None:
    response = await request(client, "POST", f"/api/scenarios/{scenario}/{action}")
    state = ScenarioState.model_validate_json(response.content)
    timestamp = state.injected_ts if action == "inject" else state.cleared_ts
    say(f"  {state.id} active={state.active} at {timestamp}")


async def advance(client: httpx.AsyncClient, hours: int, seconds: float) -> None:
    # Wall time paces the talk. The clock API, not wall time, controls sim hours.
    for _ in range(hours):
        await asyncio.sleep(seconds)
        await request(client, "POST", "/api/clock/step")


async def demo(client: httpx.AsyncClient, seconds: float, record: bool) -> None:
    previous = Status.model_validate_json(
        (await request(client, "GET", "/api/status")).content
    )
    await request(client, "POST", "/api/clock/pause")
    if record:
        response = await request(
            client, "POST", "/api/llm/mode", json={"mode": "record"}
        )
        status = Status.model_validate_json(response.content)
        say(f"Core LLM mode: {status.llm_mode}")

    say(
        "Opening: nobody asks it anything. It decides what to watch. "
        "Point out the live feed, four A2A agents, and formula, breakdown, and causal edges."
    )
    say(
        "+0h — I1: Black Friday traffic is planned. Sessions rise 40%; "
        "look for an INNOCENT decision, not a fault alert."
    )
    await set_scenario(client, "I1")

    await advance(client, 3, seconds)
    say(
        "+3h — S7: GMV falls even with higher traffic. Follow the web/Android checkout funnel, "
        "use iOS as a control, and rule out PSP-2. Look for checkout-web v5.8 client errors, "
        "a rollback recommendation, and lost euros per hour."
    )
    await set_scenario(client, "S7")

    await advance(client, 3, seconds)
    say(
        "+6h — S6: totals hide shrinking small-seller share. Show the 1P price cut, "
        "the repricer loop, and how hijacker S-99 enters later. Look for a MIXED verdict, a striped planned "
        "contribution, and SellerOS's acknowledgement for S-03, S-07, and S-12."
    )
    await set_scenario(client, "S6")

    await advance(client, 4, seconds)
    say(
        "+10h — Add refund_rate with SQL alone. Show the dry run, refunds / orders identity, "
        "breakdowns, and the proposed 72-hour Buy Box lag."
    )
    response = await request(
        client, "POST", "/api/metrics", json=REFUND_RATE.model_dump(exclude_none=True)
    )
    result = AddMetricResult.model_validate_json(response.content)
    say(
        f"  Added {result.metric.name}: {len(result.accepted)} accepted, "
        f"{len(result.ambiguous)} ambiguous, {len(result.rejected)} rejected edges."
    )

    await advance(client, 2, seconds)
    say(
        "+12h — Clear S7: show persisting, then resolved after three normal ticks. "
        "Check that occurrences increased without duplicate alerts."
    )
    await set_scenario(client, "S7", "clear")
    await advance(client, 3, seconds)

    say(
        "Close: ask an MCP client and then the UI chat, "
        "'What is broken right now and who is affected?' Show the cited incident IDs. "
        "Recap: A2A between agents, MCP for any agent, one Docker entry point."
    )
    if not previous.paused:
        await request(client, "POST", "/api/clock/resume")


def seconds_per_hour(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = -1
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("expected finite, nonnegative seconds")
    return seconds


async def run(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=300.0) as client:
        await demo(client, args.speed, args.record)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", choices=["demo"], required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--speed",
        type=seconds_per_hour,
        default=5.0,
        help="seconds per simulated hour (default: 5)",
    )
    parser.add_argument(
        "--record", action="store_true", help="record core LLM responses for replay"
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except httpx.HTTPStatusError as exc:
        print(
            f"Demo stopped: {exc.request.method} {exc.request.url.path} "
            f"returned HTTP {exc.response.status_code}.",
            file=sys.stderr,
        )
        return 1
    except (httpx.RequestError, httpx.InvalidURL, ValidationError) as exc:
        print(
            f"Demo stopped: {type(exc).__name__}. Check the base URL and core API.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
