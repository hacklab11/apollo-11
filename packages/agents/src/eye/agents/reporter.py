"""Grounded narrative generation through the shared agent runtime."""

import json
import re
from decimal import Decimal
from importlib.resources import files
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model

from agent_runtime import (
    Budget,
    RunLimits,
    RunOutcome,
    RunScope,
    end_state_outputs,
    run_agent,
)
from eye.agents.models import build_model, price_table
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
        model: Model | None = None,
        *,
        settings: LlmSettings | None = None,
    ) -> None:
        self.settings = settings or LlmSettings()
        self.model = model
        self.outcome: RunOutcome[Narrative] | None = None

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

    async def write(
        self,
        result: InvestigationResult,
        *,
        scope: RunScope | None = None,
        budget: Budget | None = None,
    ) -> tuple[str, str]:
        payload = result.model_dump(mode="json")
        payload["display_numbers"] = {
            "delta_percent": result.entry.delta_pct * 100,
            "impact_eur_per_hour": result.entry.impact_eur_per_hour,
        }
        allowed = _numbers(json.dumps(payload))
        prompt = (files("eye.llm").joinpath("prompts/reporter.md")).read_text()
        agent = Agent(
            self.model or build_model(self.settings, "reporter"),
            instructions=prompt,
            output_type=end_state_outputs(Narrative),
            retries=1,
            model_settings={"max_tokens": min(self.settings.max_tokens, 2048)},
        )

        @agent.output_validator
        def grounded(output: object) -> object:
            if isinstance(output, Narrative) and not (
                _numbers(output.title + " " + output.summary) <= allowed
            ):
                raise ModelRetry("Use only the exact numbers supplied in the input.")
            return output

        self.outcome = await run_agent(
            agent,
            json.dumps(payload),
            scope=scope or RunScope(run_id=str(uuid4()), tenant_id="default"),
            limits=RunLimits(
                max_steps=3, max_tool_calls=0, max_tokens=8192, max_wall_clock_s=30
            ),
            prices=price_table(self.settings),
            budget=budget,
        )
        if self.outcome.result.status == "done":
            narrative = self.outcome.result.result
            return narrative.title, narrative.summary
        return self.template(result)
