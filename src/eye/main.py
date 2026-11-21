"""Build and run the core service."""

import json
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any
from uuid import uuid4

import uvicorn
from anthropic import AnthropicError

from eye.a2a_client import A2AClient
from eye.agents.reporter import Reporter
from eye.agents.sentinel import Sentinel
from eye.api.app import create_app
from eye.config import Settings, load_settings
from eye.contracts import InvestigationResult, ScenarioId, TrailStep
from eye.incidents.store import IncidentStore
from eye.llm.client import BudgetExceeded, LLMClient, configure_client
from eye.orchestrator import HOUR, Orchestrator, Scenarios, StepSink
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.sim.engine import Simulator
from eye.sim.store import WORLD_DB, Store
from eye.tools.stats import expected


def _optional_module(name: str) -> ModuleType | None:
    try:
        return import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


@contextmanager
def _world(
    settings: Settings, scenario_module: ModuleType | None, path: Path = WORLD_DB
) -> Iterator[tuple[Simulator, Scenarios | None]]:
    with closing(Store(path)) as stored, stored.cursor() as reader:
        exists = reader.execute("SELECT 1 FROM skus LIMIT 1").fetchone() is not None
        latest = reader.execute("SELECT max(ts) FROM traffic_hourly").fetchone()[0]
        reader.execute(
            "CREATE TABLE IF NOT EXISTS scenario_commands (sequence BIGINT, ts TIMESTAMPTZ, id VARCHAR, active BOOLEAN)"
        )
        commands = reader.execute(
            "SELECT ts, id, active FROM scenario_commands ORDER BY sequence"
        ).fetchall()
        if not exists:
            with closing(Simulator(settings, db_path=path)) as sim:
                sim.backfill()
                yield (
                    sim,
                    scenario_module.ScenarioManager(sim) if scenario_module else None,
                )
            return

        # The simulator opens only fresh databases. Replay into a scratch store to
        # reconstruct its mutable inventory and agent export without rewriting facts.
        with closing(Simulator(settings, db_path=":memory:")) as sim:
            export_path = sim.selleros_export.path
            with TemporaryDirectory(dir=path.parent) as scratch:
                sim.selleros_export.path = Path(scratch) / export_path.name
                start = settings.sim.start_ts
                sim.now = start - HOUR * (24 * settings.sim.history_days)
                while latest is not None and sim.now < start and sim.now <= latest:
                    sim.step()
                if sim.now < start:
                    scratch_store = sim.store
                    sim.store = stored
                    try:
                        while sim.now < start:
                            sim.step()
                    finally:
                        sim.store = scratch_store
                scenarios = (
                    scenario_module.ScenarioManager(sim) if scenario_module else None
                )
                remaining = iter(commands)
                next_command = next(remaining, None)
                while True:
                    while next_command is not None and next_command[0] == sim.now:
                        if scenarios is None:
                            raise RuntimeError(
                                "cannot restore existing scenarios without T04"
                            )
                        _, id, active = next_command
                        if active:
                            scenarios.inject(id)
                        else:
                            scenarios.clear(id)
                        next_command = next(remaining, None)
                    if latest is None or sim.now > latest:
                        break
                    sim.step()
                if next_command is not None:
                    raise RuntimeError(
                        "scenario commands do not align with simulated hours"
                    )
            sim.selleros_export.path = export_path
            sim.selleros_export.write(sim.now)
            scratch_store = sim.store
            sim.store = stored
            try:
                yield sim, scenarios
            finally:
                sim.store = scratch_store


def _reporter(
    llm: LLMClient, settings: Settings, orchestrator: Orchestrator
) -> Reporter:
    def for_result(result: InvestigationResult) -> Reporter:
        n = max(
            (step.n for step in orchestrator.incidents.trail(result.investigation_id)),
            default=0,
        )

        async def narrative(**request: Any) -> dict:
            nonlocal n
            request["system"] = [
                {
                    "type": "text",
                    "text": request["system"],
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            schema = request["output_config"]["format"]["schema"]
            for prop in schema["properties"].values():
                prop.pop("minLength", None)
                prop.pop("maxLength", None)
            request.update(
                tools=[], betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
            started = time.monotonic()
            n += 1
            try:
                response = await llm.create(request)
            except AnthropicError as exc:
                await orchestrator.record_step(
                    TrailStep(
                        id=f"stp_{uuid4().hex}",
                        investigation_id=result.investigation_id,
                        n=n,
                        real_ts=datetime.now(UTC),
                        actor="reporter",
                        action="llm_call",
                        output_summary=f"Reporter unavailable: {type(exc).__name__}",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    ),
                    result.entry.ts,
                )
                raise RuntimeError("Reporter LLM unavailable") from exc
            await orchestrator.record_step(
                TrailStep(
                    id=f"stp_{uuid4().hex}",
                    investigation_id=result.investigation_id,
                    n=n,
                    real_ts=datetime.now(UTC),
                    actor="reporter",
                    action="llm_call",
                    output_summary=f"Reporter completion: {response.stop_reason}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                ),
                result.entry.ts,
            )
            if response.stop_reason != "end_turn":
                raise ValueError("Reporter did not finish the narrative")
            return json.loads(
                "".join(
                    block.text for block in response.content if block.type == "text"
                )
            )

        return Reporter(narrative, settings=settings.llm)

    class CoreReporter(Reporter):
        async def write(self, result: InvestigationResult) -> tuple[str, str]:
            if result.stop_reason == "error":
                return self.template(result)[0], result.summary
            try:
                return await for_result(result).write(result)
            except BudgetExceeded:
                return self.template(result)

    return CoreReporter(settings=settings.llm)


def main() -> None:
    settings = load_settings()
    bootstrap = import_module("eye.graph.bootstrap")
    scenario_module = _optional_module("eye.sim.scenarios")
    detective = _optional_module("eye.agents.detective")
    cartographer = _optional_module("eye.agents.cartographer")
    llm = LLMClient(settings.llm)
    configure_client(llm)

    with ExitStack() as stack:
        sim, scenarios = stack.enter_context(_world(settings, scenario_module))
        connection = stack.enter_context(sim.store.cursor())
        registry = load_registry(connection)
        engine = MetricEngine(connection, registry)
        scenario_hours = [
            start + i * HOUR
            for state in (scenarios.timeline() if scenarios else [])
            if (start := state.injected_ts) is not None
            for i in range(((state.cleared_ts or sim.now) - start) // HOUR)
        ]
        graph = bootstrap.load_or_bootstrap(engine, scenario_hours=scenario_hours)

        async def refine(clean_hours: Sequence[datetime], on_step: StepSink) -> None:
            assert cartographer is not None
            seeds = [edge for edge in graph.edges() if edge.created_by == "seed"]
            if any(not edge.evidence.get("llm_refined") for edge in seeds):
                await cartographer.refine_causal_seeds(clean_hours, on_step)
                for edge in graph.edges():
                    if edge.created_by == "seed":
                        graph.set_edge_status(
                            edge.id, edge.status, {"llm_refined": True}
                        )

        def unavailable(name: str, reason: str) -> None:
            orchestrator.agent_unavailable(name, reason)

        a2a = A2AClient(settings.a2a, on_unavailable=unavailable)
        incidents = stack.enter_context(
            closing(
                IncidentStore(
                    path_exists=graph.path_exists,
                    notify=a2a.notify_seller_os,
                    settings=settings.reporter,
                )
            )
        )

        def z_lookup(
            name: str, segment: dict[str, str], ts: datetime
        ) -> tuple[float, float]:
            reading = expected(name, ts, segment, con=connection, registry=registry)
            return reading.z if reading.z is not None else float(
                "inf"
            ), reading.completeness

        def record_scenario(id: ScenarioId, ts: datetime, active: bool) -> None:
            connection.execute(
                "INSERT INTO scenario_commands SELECT coalesce(max(sequence), 0) + 1, ?, ?, ? FROM scenario_commands",
                [ts, id, active],
            )

        orchestrator = Orchestrator(
            settings,
            sim,
            Sentinel(
                connection,
                registry,
                settings=settings.sentinel,
                llm=settings.llm,
                graph=graph,
            ),
            incidents,
            llm,
            z_lookup,
            scenarios=scenarios,
            scenario_titles=scenario_module._TITLES if scenario_module else {},
            a2a=a2a,
            investigate=detective.investigate if detective else None,
            add_metric=cartographer.add_metric if cartographer else None,
            refine=refine if cartographer else None,
            invalidate=engine.invalidate,
            record_scenario=record_scenario,
        )
        incidents.reporter = _reporter(llm, settings, orchestrator).write
        graph.on_change(lambda view: orchestrator.emit("graph_changed", view))
        stack.callback(graph.save)
        uvicorn.run(
            create_app(orchestrator, connection, registry, graph, engine),
            host="127.0.0.1",
            port=settings.ports.core,
            access_log=False,
            log_config=None,
        )
