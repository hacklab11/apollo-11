"""Narrative generation with an injected structured-output LLM transport."""

import json
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eye.config import LlmSettings
from eye.contracts import InvestigationResult


class Narrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=89)
    summary: str = Field(min_length=1)


_NUMBER = re.compile(r"(?<![\w])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(match.group().replace(",", "")) for match in _NUMBER.finditer(text)}


class Reporter:
    def __init__(
        self,
        llm: Callable[..., Awaitable[dict[str, Any] | Narrative]] | None = None,
        *,
        settings: LlmSettings | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or LlmSettings()

    @staticmethod
    def template(result: InvestigationResult) -> tuple[str, str]:
        entry = result.entry
        title = f"{entry.metric}: {result.verdict}"[:89]
        summary = (
            f"{entry.metric}: actual {entry.actual:g}, expected {entry.expected:g}; "
            f"delta {entry.delta:g} ({entry.delta_pct * 100:g}%). "
            f"Impact EUR {entry.impact_eur_per_hour:g} per hour. "
            f"Verdict: {result.verdict}."
        )
        return title, summary

    async def write(self, result: InvestigationResult) -> tuple[str, str]:
        fallback = self.template(result)
        if self.llm is None:
            return fallback
        payload = result.model_dump(mode="json")
        payload["display_numbers"] = {
            "delta_percent": result.entry.delta_pct * 100,
            "impact_eur_per_hour": result.entry.impact_eur_per_hour,
        }
        allowed = _numbers(json.dumps(payload))
        prompt = (Path(__file__).parents[1] / "llm/prompts/reporter.md").read_text()
        messages = [{"role": "user", "content": json.dumps(payload)}]
        for _ in range(2):
            try:
                response = await self.llm(
                    model=self.settings.model_for("reporter"),
                    max_tokens=self.settings.max_tokens,
                    system=prompt,
                    messages=messages,
                    output_config={
                        "effort": "medium",
                        "format": {
                            "type": "json_schema",
                            "schema": Narrative.model_json_schema(),
                        },
                    },
                )
                narrative = Narrative.model_validate(response)
                if _numbers(narrative.title + " " + narrative.summary) <= allowed:
                    return narrative.title, narrative.summary
            except Exception:
                pass
            messages.append(
                {
                    "role": "user",
                    "content": "The response was invalid or changed a number. Return the required title and summary using only the exact supplied numbers.",
                }
            )
        return fallback
