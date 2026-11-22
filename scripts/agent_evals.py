"""Offline S8 production-agent evals; --live explicitly enables OpenRouter."""

import argparse
import asyncio
import hashlib
import json
import os
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import duckdb
import httpx
import yaml
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from agent_runtime import ModelPrice, PriceTable
from eye.agents.cartographer import Cartographer, Submission
from eye.agents.detective import Detective
from eye.agents.reporter import Reporter
from eye.agents.sentinel import Sentinel
from eye.agents.skeptic import Skeptic
from eye.config import LlmSettings, Settings
from eye.contracts import Anomaly, Hypothesis, InvestigationResult, MetricDef
from eye.contracts import SpecialistRequest as EyeSpecialistRequest
from eye.graph.store import GraphStore
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.specialists.app import SPECIALISTS
from eye.specialists.app import answer as specialist_answer
from eye.specialists.tools import DataClient as SpecialistDataClient
from eye.warehouse import operations as ops
from selleros.agent import answer
from selleros.client import CLAUDE, OPENROUTER_MODELS
from selleros.data import Snapshot
from selleros.models import SpecialistRequest

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests/evals/cases"
AGENTS = {
    "sentinel",
    "detective",
    "skeptic",
    "reporter",
    "cartographer",
    "selleros",
    *(specialist.NAME for specialist in SPECIALISTS),
}


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    payload: dict[str, Any]
    responses: list[dict[str, Any]] = Field(min_length=1)
    reads: dict[str, Any] = Field(default_factory=dict)
    snapshot: Snapshot | None = None


@dataclass
class DecisionScore(Evaluator[Inputs, dict[str, Any]]):
    """Compare stable decision fields, leaving narrative wording unconstrained."""

    def evaluate(self, ctx: EvaluatorContext) -> float:
        expected = ctx.expected_output
        if not expected:
            raise ValueError("Every case must specify expected decision fields")

        def matches(actual: Any, wanted: Any) -> bool:
            if isinstance(wanted, dict):
                return isinstance(actual, dict) and all(
                    key in actual and matches(actual[key], value)
                    for key, value in wanted.items()
                )
            if isinstance(wanted, list):
                return (
                    isinstance(actual, list)
                    and len(actual) == len(wanted)
                    and all(matches(a, b) for a, b in zip(actual, wanted, strict=True))
                )
            return actual == wanted

        return float(matches(ctx.output, expected))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prompt_for(agent: str) -> str:
    path = (
        ROOT / "packages/selleros/selleros/prompt.md"
        if agent == "selleros"
        else ROOT / f"packages/llm/src/eye/llm/prompts/{agent}.md"
    )
    return path.read_text()


def load_cases(directory: Path = CASES) -> Dataset:
    cases = []
    for path in sorted(directory.rglob("*")):
        if path.suffix not in {".yaml", ".yml", ".json"}:
            continue
        dataset = Dataset[Inputs, dict[str, Any], dict[str, Any]].from_file(path)
        cases.extend(dataset.cases)
    if not cases:
        raise ValueError(f"No eval cases found in {directory}")
    if len({case.name for case in cases}) != len(cases):
        raise ValueError("Eval case names must be unique")
    unknown = {case.inputs.agent for case in cases} - AGENTS
    if unknown:
        raise ValueError(f"Unknown eval agents: {sorted(unknown)}")
    missing = AGENTS - {case.inputs.agent for case in cases}
    if missing:
        raise ValueError(f"Missing agent coverage: {sorted(missing)}")
    return Dataset(
        name="s8-agent-regressions", cases=cases, evaluators=[DecisionScore()]
    )


def model_matrix(settings_path: Path) -> dict[str, list[str]]:
    settings = LlmSettings.model_validate(
        yaml.safe_load(settings_path.read_text())["llm"]
    )
    unknown = settings.fallback_models.keys() - AGENTS
    if unknown:
        raise ValueError(f"Fallback agents need eval coverage: {sorted(unknown)}")
    return {
        agent: list(
            dict.fromkeys(
                ([CLAUDE, *OPENROUTER_MODELS] if agent == "selleros" else [])
                + [settings.model_for(agent)]
                + settings.fallback_models.get(agent, [])
            )
        )
        for agent in sorted(AGENTS)
    }


def replay(inputs: Inputs, model_id: str) -> FunctionModel:
    responses = iter(inputs.responses)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        try:
            response = next(responses)
        except StopIteration:
            raise ValueError(
                "Recording exhausted; offline replay never calls a provider"
            ) from None
        name = response.get("tool", info.output_tools[0].name)
        arguments = response.get("arguments", response)
        return ModelResponse(parts=[ToolCallPart(name, arguments)], model_name=model_id)

    return FunctionModel(respond, model_name=model_id)


class RecordedData:
    def __init__(self, inputs: Inputs) -> None:
        self.reads = inputs.reads

    def read(self, operation: ops.Operation, args: BaseModel) -> Any:
        if operation.path not in self.reads:
            raise ValueError(f"Unrecorded data operation: {operation.path}")
        return operation.output.validate_python(self.reads[operation.path])


class RecordedSellerStore:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    def read(self) -> Snapshot:
        return self.snapshot.model_copy(deep=True)

    def paused_repricers(self) -> set[tuple[str, str]]:
        return set()

    def reverted_prices(self) -> set[tuple[str, str, datetime, float]]:
        return set()


class EvalModel(WrapperModel):
    def __init__(
        self,
        wrapped: Model,
        usage: RunUsage,
        live: bool,
        schemas: list[list[dict[str, Any]]],
    ) -> None:
        super().__init__(wrapped)
        self.usage = usage
        self.live = live
        self.schemas = schemas
        self.limits = UsageLimits(request_limit=30, total_tokens_limit=100_000)
        self.completed = False
        self.error: Exception | None = None

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        schema = [
            asdict(tool)
            for tool in [
                *model_request_parameters.function_tools,
                *model_request_parameters.output_tools,
            ]
        ]
        self.schemas.append(schema)
        self.completed = False
        try:
            if self.live:
                self.limits.check_before_request(self.usage)
                self.usage.requests += 1
            response = await self.wrapped.request(
                messages,
                {**(model_settings or {}), "max_tokens": 1024},
                model_request_parameters,
            )
            if self.live:
                self.usage.incr(response.usage)
                self.limits.check_tokens(self.usage)
            self.completed = any(
                isinstance(part, ToolCallPart)
                and part.tool_name == model_request_parameters.output_tools[0].name
                for part in response.parts
            )
        except Exception as exc:
            self.error = exc
            raise
        return response


async def discard(step: Any) -> None:
    pass


async def run_case(inputs: Inputs, model: Model, model_id: str) -> dict[str, Any]:
    prices = {
        name: {"input_per_mtok": 0.0, "output_per_mtok": 0.0}
        for name in {model_id, model.model_name}
    }
    settings = LlmSettings(
        provider="openai_compatible",
        models={"default": model_id},
        prices=prices,
    )
    data = RecordedData(inputs)
    if inputs.agent == "sentinel":
        decisions = await Sentinel(data=data, llm=settings, model=model).run(
            datetime.fromisoformat(inputs.payload["ts"]), on_step=discard
        )
        return {"decisions": [item.model_dump(mode="json") for item in decisions]}
    if inputs.agent == "skeptic":
        result = await Skeptic(data=data, llm=settings, model=model).review(
            Anomaly.model_validate(inputs.payload["entry"]),
            "eval-s8",
            discard,
            hypothesis=Hypothesis.model_validate(inputs.payload["hypothesis"])
            if inputs.payload.get("hypothesis") is not None
            else None,
        )
        return result.model_dump(mode="json")
    if inputs.agent == "detective":
        graph = GraphStore(None)
        graph.add_metric(
            MetricDef(
                name="aov",
                description="Average order value",
                unit="eur",
                domain="orders",
                kind="sql",
                sql="SELECT ts, 1 AS num FROM orders_hourly",
                dimensions=["category"],
            )
        )
        result = await Detective(
            None, None, graph, data=data, llm=settings, model=model
        ).investigate(Anomaly.model_validate(inputs.payload["entry"]), discard)
        return result.model_dump(mode="json")
    if inputs.agent == "reporter":
        investigation = InvestigationResult.model_validate(inputs.payload)
        result = await Reporter(model=model, settings=settings).write(investigation)
        return {"title": result.title, "summary": result.summary}
    if inputs.agent == "cartographer":
        registry = Registry(
            metrics={}, source_tables={}, has_den={}, identities=[], causal_seeds=[]
        )
        with closing(duckdb.connect(":memory:")) as con:
            engine = MetricEngine(con, registry)
            result = await Cartographer(
                engine, GraphStore(None), model=model, llm=settings
            )._propose(
                Submission, engine, json.dumps(inputs.payload), "eval-s8", discard
            )
        return result.model_dump(mode="json")
    if inputs.agent in {specialist.NAME for specialist in SPECIALISTS}:

        def recorded(request: httpx.Request) -> httpx.Response:
            endpoint = request.url.path.removeprefix("/data/")
            if endpoint not in inputs.reads:
                raise ValueError(f"Unrecorded specialist data operation: {endpoint}")
            return httpx.Response(200, json=inputs.reads[endpoint])

        async with httpx.AsyncClient(transport=httpx.MockTransport(recorded)) as http:
            result, _ = await specialist_answer(
                next(item for item in SPECIALISTS if item.NAME == inputs.agent),
                SpecialistDataClient(http, "http://recorded/data"),
                EyeSpecialistRequest.model_validate(inputs.payload),
                Settings(llm=settings),
                model=model,
            )
        return result.model_dump(mode="json")
    if inputs.snapshot is None:
        raise ValueError("SellerOS case requires a scoped data snapshot")
    result = await answer(
        RecordedSellerStore(inputs.snapshot),
        SpecialistRequest.model_validate(inputs.payload),
        model=model,
        prices=PriceTable(
            prices={name: ModelPrice(**price) for name, price in prices.items()}
        ),
    )
    return result.model_dump(mode="json")


def pins(agent: str, model_id: str) -> dict[str, str]:
    return {
        "agent": agent,
        "model_id": model_id,
        "prompt_sha256": digest(prompt_for(agent)),
        "pydantic-ai-slim": version("pydantic-ai-slim"),
        "pydantic-evals": version("pydantic-evals"),
        "surface": "production-agent-recorded-S8",
    }


async def evaluate(
    directory: Path = CASES,
    settings_path: Path = ROOT / "config/settings.yaml",
    *,
    live: bool = False,
    report_path: Path | None = None,
) -> bool:
    dataset = load_cases(directory)
    matrix = model_matrix(settings_path)
    models.ALLOW_MODEL_REQUESTS = live
    # A single shared counter, sequential execution, and provider max_tokens bound the entire live run.
    usage = RunUsage()
    reports = []
    passed = True
    for agent, model_ids in matrix.items():
        for model_id in model_ids:
            pinned = pins(agent, model_id)
            schemas = []
            subset = Dataset(
                name=f"s8-{agent}",
                cases=[case for case in dataset.cases if case.inputs.agent == agent],
                evaluators=[DecisionScore()],
            )

            async def task(
                inputs: Inputs,
                model_id: str = model_id,
                schemas: list[list[dict[str, Any]]] = schemas,
            ) -> dict[str, Any]:
                model = (
                    OpenAIChatModel(
                        "anthropic/" + model_id if model_id == CLAUDE else model_id,
                        provider=OpenAIProvider(
                            openai_client=AsyncOpenAI(
                                base_url="https://openrouter.ai/api/v1",
                                api_key=os.environ["OPENROUTER_API_KEY"],
                                max_retries=0,
                                timeout=45,
                            ),
                        ),
                    )
                    if live
                    else replay(inputs, model_id)
                )
                wrapped = EvalModel(model, usage, live, schemas)
                output = await run_case(inputs, wrapped, model_id)
                if wrapped.error:
                    raise wrapped.error
                if not wrapped.completed and not (
                    inputs.agent == "sentinel" and not output["decisions"]
                ):
                    raise ValueError(
                        "Agent fell back without a successful model submission"
                    )
                return output

            report = await subset.evaluate(
                task,
                name=f"{agent}/{model_id}",
                max_concurrency=1,
                progress=False,
                metadata=pinned,
                task_timeout=60,
            )
            pinned["tool_schema_sha256"] = (
                digest(schemas[0]) if schemas else "NO_MODEL_REQUEST"
            )
            report.experiment_metadata = pinned
            print(json.dumps({"versions": pinned}, sort_keys=True))
            score = sum(
                c.scores.get("DecisionScore").value
                if "DecisionScore" in c.scores
                else 0
                for c in report.cases
            ) / len(subset.cases)
            ok = not report.failures and bool(schemas) and score >= 1.0
            print(
                f"{agent} | {model_id} | score={score:.3f} threshold=1.000 | {'PASS' if ok else 'FAIL'}"
            )
            for failure in report.failures:
                print(f"  failed case: {failure.name}")
            reports.append(asdict(report))
            passed &= ok
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                reports,
                indent=2,
                default=lambda obj: (
                    obj.model_dump(mode="json")
                    if isinstance(obj, BaseModel)
                    else str(obj)
                ),
            )
            + "\n"
        )
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Opt in to paid provider requests"
    )
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--settings", type=Path, default=ROOT / "config/settings.yaml")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.live and not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("--live requires OPENROUTER_API_KEY")
    return (
        0
        if asyncio.run(
            evaluate(args.cases, args.settings, live=args.live, report_path=args.report)
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
