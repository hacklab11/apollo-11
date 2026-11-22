"""Grounded narrative generation through the shared agent runtime."""

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from agent_runtime import (
    Budget,
    RunLimits,
    RunScope,
    RunUsageSummary,
    end_state_outputs,
    run_agent,
)
from agent_runtime.configured_models import build_model, price_table
from eye.config import LlmSettings
from eye.contracts_v2 import InvestigationResult


class Narrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=89)
    summary: str = Field(min_length=1)


@dataclass(frozen=True)
class ReporterResult:
    title: str
    summary: str
    usage: RunUsageSummary


_LOG = logging.getLogger(__name__)
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
        self.agent = Agent(
            model or build_model(self.settings, "reporter"),
            deps_type=set[Decimal],
            instructions=files("eye.agents")
            .joinpath("prompts/reporter.md")
            .read_text(),
            output_type=end_state_outputs(Narrative),
            retries=1,
            model_settings={"max_tokens": min(self.settings.max_tokens, 2048)},
        )

        @self.agent.output_validator
        def grounded(ctx: RunContext[set[Decimal]], output: object) -> object:
            if isinstance(output, Narrative) and not (
                _numbers(output.title + " " + output.summary) <= ctx.deps
            ):
                raise ModelRetry("Use only the exact numbers supplied in the input.")
            return output

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
    ) -> ReporterResult:
        payload = result.model_dump(mode="json")
        payload["display_numbers"] = {
            "delta_percent": result.entry.delta_pct * 100,
            "impact_eur_per_hour": result.entry.impact_eur_per_hour,
        }
        allowed = _numbers(json.dumps(payload))
        outcome = await run_agent(
            self.agent,
            json.dumps(payload),
            scope=scope or RunScope(run_id=str(uuid4()), tenant_id="default"),
            limits=RunLimits(
                max_steps=3, max_tool_calls=0, max_tokens=8192, max_wall_clock_s=30
            ),
            prices=price_table(self.settings),
            budget=budget,
            deps=allowed,
        )
        if outcome.result.status == "done":
            narrative = outcome.result.result
            return ReporterResult(
                narrative.title, narrative.summary, outcome.result.usage
            )
        _LOG.warning(
            "agent=reporter end_state=%s reason=%s",
            outcome.result.status,
            outcome.result.reason,
        )
        title, summary = self.template(result)
        return ReporterResult(title, summary, outcome.result.usage)
