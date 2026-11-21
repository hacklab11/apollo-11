"""Chat agent using only tools discovered through MCP."""

import json
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from mcp import Client
from mcp.types import Tool
from pydantic import BaseModel, Field, create_model

from eye.config import LlmSettings
from eye.contracts import TrailStep
from eye.llm.client import BudgetExceeded, LLMUnavailable
from eye.llm.runtime import AgentRefused, AgentSpec, ToolSpec, run_agent


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(max_length=20)


class ToolCall(BaseModel):
    tool: str
    input: dict[str, Any]
    summary: str


class ChatAnswer(BaseModel):
    answer: str
    incident_ids: list[str]


class ChatResponse(ChatAnswer):
    tool_calls: list[ToolCall]


class MCPOutput(BaseModel):
    summary: str = Field(max_length=300)
    data: Any


class MCPInput(BaseModel):
    tool_schema: ClassVar[dict[str, Any]]

    @classmethod
    def model_json_schema(cls, **kwargs: Any) -> dict[str, Any]:
        return cls.tool_schema


def tool_spec(session: Client, tool: Tool) -> ToolSpec:
    schema = tool.input_schema
    required = schema.get("required", [])
    model = create_model(
        f"MCP_{tool.name}",
        __base__=MCPInput,
        **{
            name: (Any, ... if name in required else prop.get("default"))
            for name, prop in schema.get("properties", {}).items()
        },
    )
    model.tool_schema = schema

    async def call(args: BaseModel) -> MCPOutput:
        result = await session.call_tool(
            tool.name, args.model_dump(mode="json", exclude_unset=True)
        )
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if result.is_error:
            raise RuntimeError(text[:300])
        data = result.structured_content
        return MCPOutput(
            summary=str(data.get("summary", text) if data else text)[:300],
            data=data if data is not None else text,
        )

    return ToolSpec(tool.name, tool.description or tool.name, model, call, strict=False)


def create_router(
    settings: LlmSettings, mcp_url: str = "http://127.0.0.1:8100/mcp"
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/chat")
    async def chat(request: ChatRequest) -> ChatResponse:
        calls: list[ToolCall] = []

        async def collect(step: TrailStep) -> None:
            if step.action == "tool_call" and step.tool != "submit":
                calls.append(
                    ToolCall(
                        tool=step.tool or "",
                        input=step.input,
                        summary=step.output_summary,
                    )
                )

        try:
            async with Client(mcp_url) as session:
                tools: list[ToolSpec] = []
                cursor = None
                while True:
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(tool_spec(session, tool) for tool in page.tools)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                answer = await run_agent(
                    AgentSpec(
                        name="chat",
                        system_prompt_file=str(
                            Path(__file__).parents[1] / "llm/prompts/chat.md"
                        ),
                        tools=tools,
                        submit_model=ChatAnswer,
                        effort="medium",
                        model=settings.model_for("chat"),
                        max_steps=8,
                        max_tokens=settings.max_tokens,
                    ),
                    json.dumps(request.model_dump(mode="json")),
                    f"chat-{uuid4()}",
                    collect,
                )
        except BudgetExceeded as exc:
            raise HTTPException(503, "Chat budget exhausted") from exc
        except AgentRefused as exc:
            raise HTTPException(422, "Chat request refused") from exc
        except (LLMUnavailable, OSError) as exc:
            raise HTTPException(503, "Chat service unavailable") from exc
        return ChatResponse(**answer.model_dump(), tool_calls=calls)

    return router
