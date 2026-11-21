"""Version adoption and client error telemetry."""

from dataclasses import dataclass, field
from datetime import datetime

from numpy.random import Generator

from eye.sim.demand import PLATFORMS
from eye.sim.store import Batches

PAGES = ("home", "search", "product", "cart", "checkout")
ERRORS = ("NetworkError", "TimeoutError", "TypeError", "ResourceLoadError")


@dataclass
class Apps:
    versions: dict[str, str] = field(
        default_factory=lambda: {
            "web": "web v1.0",
            "android": "app-android v1.0",
            "ios": "app-ios v1.0",
            "checkout-web": "checkout-web v5.7",
        }
    )
    releases: dict[str, tuple[datetime, str, str]] = field(default_factory=dict)
    error_rates: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def release(self, system: str, version: str, ts: datetime) -> None:
        if system not in self.versions:
            raise ValueError(f"unknown app: {system}")
        self.releases[system] = (ts, self.versions[system], version)
        self.versions[system] = version

    def adoption(self, system: str, ts: datetime) -> dict[str, float]:
        if system not in self.releases:
            return {self.versions[system]: 1.0}
        released, old, new = self.releases[system]
        if ts < released:
            return {old: 1.0}
        share = (
            1.0
            if system in ("web", "checkout-web")
            else min(1.0, 0.2 * (1 + (ts - released).days))
        )
        return {old: 1 - share, new: share} if share < 1 else {new: 1.0}

    def errors(self, ts: datetime, rng: Generator, batches: Batches) -> None:
        rows = batches.setdefault("client_errors_hourly", [])
        for platform in PLATFORMS:
            sessions = sum(
                r[3] for r in batches.get("traffic_hourly", []) if r[2] == platform
            )
            for page in PAGES:
                system = (
                    "checkout-web"
                    if page == "checkout" and platform in ("web", "android")
                    else platform
                )
                for version, share in self.adoption(system, ts).items():
                    for error in ERRORS:
                        rate = self.error_rates.get((platform, page, error), 0.002 / 20)
                        rows.append(
                            (
                                ts,
                                platform,
                                version,
                                page,
                                error,
                                int(rng.poisson(sessions * share * rate)),
                            )
                        )
