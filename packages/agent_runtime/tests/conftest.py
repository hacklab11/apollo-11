from collections.abc import Callable, Iterator
from typing import Any

import pytest
from agent_runtime import ModelPrice, PriceTable, RunScope
from pydantic import BaseModel
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

models.ALLOW_MODEL_REQUESTS = False

MODEL_NAME = "scripted"


class Diagnosis(BaseModel):
    cause: str


Step = Callable[[AgentInfo], ToolCallPart]


def call(tool: str, **args: Any) -> Step:
    return lambda info: ToolCallPart(tool, args)


def finish(output_type: type[BaseModel], **args: Any) -> Step:
    def step(info: AgentInfo) -> ToolCallPart:
        name = next(t.name for t in info.output_tools if output_type.__name__ in t.name)
        return ToolCallPart(name, args)

    return step


def scripted(*steps: Step, model_name: str = MODEL_NAME) -> FunctionModel:
    """A model that replays `steps` in order, one tool call per request; never touches a provider."""
    remaining = list(steps)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return ModelResponse(parts=[step(info)], model_name=model_name)

    return FunctionModel(respond, model_name=model_name)


@pytest.fixture
def prices() -> PriceTable:
    return PriceTable(
        prices={MODEL_NAME: ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0)}
    )


@pytest.fixture
def scope() -> RunScope:
    return RunScope(run_id="run-1", tenant_id="tenant-a")


@pytest.fixture(autouse=True)
def _no_real_models() -> Iterator[None]:
    assert models.ALLOW_MODEL_REQUESTS is False
    yield
