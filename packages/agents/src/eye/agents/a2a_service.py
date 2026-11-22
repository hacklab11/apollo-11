"""Typed skill execution over the SDK's A2A 1.0 JSON-RPC transport."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from a2a.client import Client, ClientCallContext, ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.helpers.proto_helpers import new_data_part, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ValidationError

from eye.contracts import TrailStep
from eye.contracts_v2 import DEFAULT_TENANT, AgentResult, RunUsageSummary, TaskMetadata
from eye.llm.client import BudgetExceeded
from eye_auth.client import oauth_client
from eye_auth.trust import verify_card

_LOG = logging.getLogger(__name__)
_STATES = {
    "completed": TaskState.TASK_STATE_COMPLETED,
    "input-required": TaskState.TASK_STATE_INPUT_REQUIRED,
    "failed": TaskState.TASK_STATE_FAILED,
}


def seconds_left(metadata: TaskMetadata) -> float:
    return (metadata.budget.deadline - datetime.now(UTC)).total_seconds()


@dataclass
class Run:
    agent: str
    metadata: TaskMetadata
    trail: list[TrailStep] = field(default_factory=list)
    usage: RunUsageSummary = field(default_factory=RunUsageSummary)
    delegations: int = 0

    async def record(self, step: TrailStep) -> None:
        if step.actor == self.agent:
            self.usage.requests += int(step.action == "llm_call")
            self.usage.tool_calls += int(step.action == "tool_call")
            self.usage.input_tokens += step.tokens_in
            self.usage.output_tokens += step.tokens_out
        self.trail.append(
            step.model_copy(
                update={
                    "input": {},
                    "output": None,
                    "reason": "",
                    **(
                        {"output_summary": "Model turn finished"}
                        if step.action == "llm_call"
                        else {}
                    ),
                }
            )
        )
        self.check_tokens()

    def check_tokens(self) -> None:
        if self.usage.total_tokens >= self.metadata.budget.tokens:
            raise BudgetExceeded("Token budget exhausted")

    def delegate(self) -> TaskMetadata:
        budget = self.metadata.budget
        self.check_tokens()
        if budget.depth >= budget.max_depth or self.delegations >= budget.max_fan_out:
            raise BudgetExceeded("Delegation limit reached")
        self.delegations += 1
        return self.metadata.model_copy(
            update={
                "budget": budget.model_copy(
                    update={
                        "depth": budget.depth + 1,
                        "tokens": budget.tokens - self.usage.total_tokens,
                        "cost_usd": max(0.0, budget.cost_usd - self.usage.cost_usd),
                    }
                )
            }
        )

    def add_usage(self, usage: RunUsageSummary) -> None:
        self.usage.requests += usage.requests
        self.usage.tool_calls += usage.tool_calls
        self.usage.input_tokens += usage.input_tokens
        self.usage.output_tokens += usage.output_tokens
        self.usage.cost_usd += usage.cost_usd


@dataclass(frozen=True)
class Skill[I: BaseModel, O: BaseModel]:
    id: str
    description: str
    input: type[I]
    output: type[O]
    invoke: Callable[[I, Run], Awaitable[O]]


def agent_card(name: str, url: str, skill: Skill) -> AgentCard:
    return AgentCard(
        name=name,
        description=skill.description,
        version="0.1.0",
        capabilities=AgentCapabilities(),
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=skill.id,
                name=skill.id,
                description=skill.description,
                tags=[skill.id],
            )
        ],
    )


class SkillExecutor(AgentExecutor):
    def __init__(self, agent: str, skill: Skill) -> None:
        self.agent = agent
        self.skill = skill

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                )
            )
        result_model = AgentResult[self.skill.output]
        run = None
        try:
            metadata = TaskMetadata.model_validate(context.metadata)
            message = context.message
            if (
                not message
                or message.role != Role.ROLE_USER
                or len(message.parts) != 1
                or not message.parts[0].HasField("data")
                or metadata.context_id != context.context_id
                or metadata.tenant_id != DEFAULT_TENANT
                or context.current_task is not None
            ):
                raise ValueError("Invalid skill message or metadata")
            payload = self.skill.input.model_validate(
                MessageToDict(message.parts[0].data)
            )
            run = Run(self.agent, metadata)
            remaining = seconds_left(metadata)
            if (
                remaining <= 0
                or metadata.budget.cost_usd <= 0
                or metadata.budget.depth > metadata.budget.max_depth
            ):
                raise BudgetExceeded("Task budget exhausted")
            run.check_tokens()
            await updater.start_work()
            async with asyncio.timeout(remaining):
                output = await self.skill.invoke(payload, run)
            run.check_tokens()
            result = result_model(status="done", result=output, usage=run.usage)
        except (BudgetExceeded, TimeoutError):
            result = result_model(
                status="gave_up",
                reason="Task budget exhausted",
                **({"usage": run.usage} if run is not None else {}),
            )
        except Exception as exc:  # noqa: BLE001 -- always terminate an accepted A2A task
            _LOG.warning("agent=%s task_failed=%s", self.agent, type(exc).__name__)
            result = result_model(
                status="failed",
                reason="Invalid task request"
                if run is None
                else "Agent execution failed",
                **({"usage": run.usage} if run is not None else {}),
            )

        await updater.add_artifact(
            [
                new_data_part(
                    result.model_dump(mode="json"), media_type="application/json"
                )
            ],
            name=self.skill.id,
            last_chunk=True,
        )
        detail = result.question or result.reason
        await updater.update_status(
            _STATES[result.a2a_state], new_text_message(detail) if detail else None
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


async def send_skill[T: BaseModel](
    http: httpx.AsyncClient,
    url: str,
    skill: str,
    request: BaseModel,
    metadata: TaskMetadata,
    output: type[T],
) -> AgentResult[T]:
    async with asyncio.timeout(seconds_left(metadata)):
        card = await A2ACardResolver(http, url).get_agent_card()
        await verify_card(
            card,
            {
                "detect": "sentinel",
                "investigate": "detective",
                "challenge": "skeptic",
                "report": "reporter",
                "map": "cartographer",
            }[skill],
        )
        if skill not in {item.id for item in card.skills}:
            raise ValueError(f"Agent does not advertise {skill}")
        rpc = SendMessageRequest(
            message=Message(
                message_id=uuid4().hex,
                context_id=metadata.context_id,
                role=Role.ROLE_USER,
                parts=[
                    new_data_part(
                        request.model_dump(mode="json"), media_type="application/json"
                    )
                ],
            ),
            metadata=metadata.model_dump(mode="json"),
        )
        context = ClientCallContext(
            service_parameters={"traceparent": metadata.traceparent}
        )
        async with oauth_client({f"{card.name}:{skill}"}, http=http) as authenticated:
            factory = ClientFactory(
                ClientConfig(streaming=False, httpx_client=authenticated)
            )
            async with factory.create(card) as client:
                return await _skill_result(client, rpc, context, metadata, output)
    raise ValueError("Empty agent response")


async def _skill_result[T: BaseModel](
    client: Client,
    rpc: SendMessageRequest,
    context: ClientCallContext,
    metadata: TaskMetadata,
    output: type[T],
) -> AgentResult[T]:
    async for event in client.send_message(rpc, context=context):
        task = event.task
        if (
            not event.HasField("task")
            or task.context_id != metadata.context_id
            or len(task.artifacts) != 1
            or len(task.artifacts[0].parts) != 1
            or not task.artifacts[0].parts[0].HasField("data")
        ):
            raise ValueError("Invalid agent response")
        try:
            result = AgentResult[output].model_validate(
                MessageToDict(task.artifacts[0].parts[0].data)
            )
        except ValidationError:
            raise ValueError("Invalid agent result") from None
        if _STATES[result.a2a_state] != task.status.state:
            raise ValueError("Agent result does not match task state")
        return result
    raise ValueError("Empty agent response")
