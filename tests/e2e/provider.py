"""Finite model script for the S8 integration gate; never forwards requests."""

import json
from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

app = FastAPI()
calls: Counter[str] = Counter()
errors: list[str] = []
investigate_after: datetime | None = None


class ScenarioSchedule(BaseModel):
    investigate_after: AwareDatetime


class CompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)


def decode(content: str) -> dict[str, Any]:
    return json.JSONDecoder().raw_decode(content)[0]


def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"scripted-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def results(request: CompletionRequest) -> list[dict[str, Any]]:
    return [decode(m["content"]) for m in request.messages if m["role"] == "tool"]


def hypothesis(entry: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "scripted-price-change",
        "statement": "Scripted electronics price-change hypothesis for the integration gate.",
        "cause_type": "price_change",
        "root_node": "aov",
        "segment": {"category": "electronics"},
        "first_change_ts": entry["ts"],
        "explained_share": 1.0,
        "status": "supported",
        "evidence_for": [
            {
                "group": group,
                "source": name,
                "summary": output["summary"],
                "step_id": f"ref-{i}",
            }
            for i, (group, name, output) in enumerate(
                zip(
                    ["data", "freshness"],
                    ["baseline_compare", "check_freshness"],
                    outputs[:2],
                    strict=True,
                ),
                start=1,
            )
        ],
        "evidence_against": [],
    }


def scripted_response(request: CompletionRequest) -> dict[str, Any]:
    payload = decode(
        next(m["content"] for m in request.messages if m["role"] == "user")
    )
    outputs = results(request)
    if any("error" in output for output in outputs):
        raise ValueError("A runtime tool failed")

    if request.model == "cartographer:free" and payload.get("mode") == "refine_seeds":
        return tool(
            next(
                item["function"]["name"]
                for item in request.tools
                if "CompleteSeedSubmission" in item["function"]["name"]
            ),
            {
                "seeds": [
                    {
                        "src": item["edge"]["src"],
                        "dst": item["edge"]["dst"],
                        "status": "hypothesis",
                        "confidence": item["edge"]["confidence"],
                        "rationale": "Retain seed for the scripted integration gate.",
                    }
                    for item in payload["seeds"]
                ],
                "edges": [],
                "notes": "Scripted bootstrap; no new causal claims.",
            },
        )

    if request.model == "cartographer:free" and "metric" in payload:
        return tool(
            next(
                item["function"]["name"]
                for item in request.tools
                if item["function"]["name"].endswith("_Submission")
            ),
            {
                "edges": [],
                "notes": "Scripted placement; no causal claims.",
                "unit": payload["metric"]["unit"],
            },
        )

    if request.model == "sentinel":
        decisions = []
        selected = False
        for candidate in payload["candidates"]:
            anomaly = candidate["anomaly"]
            action = "ignore"
            if candidate["completeness"] < payload["min_completeness"]:
                action = "defer"
            elif (
                not selected
                and not payload["open_incidents"]
                and investigate_after is not None
                and datetime.fromisoformat(payload["ts"]) >= investigate_after
                and anomaly["metric"]
                in {"aov", "first_party_margin", "contribution_margin"}
                and anomaly["segment"] == {"category": "electronics"}
                and anomaly["delta"] < 0
            ):
                action = "investigate"
                selected = True
            decisions.append(
                {
                    "anomaly": anomaly,
                    "action": action,
                    "reason": "Scripted triage of measured candidates during the S8 integration schedule.",
                }
            )
        return tool("submit", {"decisions": decisions})

    if request.model in {"detective", "skeptic"}:
        entry = payload["entry"]
        args = {"metric": entry["metric"], "segment": entry["segment"]}
        if not outputs:
            return tool("baseline_compare", args)
        if len(outputs) == 1:
            return tool("check_freshness", args)
        if request.model == "skeptic":
            return tool(
                "submit",
                {
                    "verdict": "not_innocent",
                    "explained_share": 0,
                    "reasons": [
                        "Scripted challenge after baseline and freshness tools."
                    ],
                    "evidence": [],
                },
            )
        root = hypothesis(entry, outputs)
        if len(outputs) == 2:
            return tool("update_hypotheses", {"hypotheses": [root]})
        if len(outputs) == 3:
            return tool("ask_skeptic", {"hypothesis_id": root["id"]})
        if len(outputs) == 4:
            return tool(
                "final_result__Submission",
                {
                    "verdict": "CONFIRMED",
                    "summary": "Scripted price-change investigation completed after deterministic tools and Skeptic review.",
                    "root_cause": root,
                    "hypotheses": [root],
                    "contributions": [],
                    "open_questions": [],
                    "suggested_actions": ["Review the manual electronics prices."],
                },
            )

    if request.model == "reporter":
        if any(
            item["function"]["name"] == "final_result_Narrative"
            for item in request.tools
        ):
            return tool(
                "final_result_Narrative",
                {
                    "title": "Electronics price-change investigation",
                    "summary": "Offline integration report: price-change signal investigated with baseline, freshness and Skeptic review.",
                },
            )
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "title": "Electronics price-change investigation",
                    "summary": "Offline integration report: price-change signal investigated with baseline, freshness and Skeptic review.",
                }
            ),
        }
    raise ValueError(f"Unexpected scripted model or turn: {request.model}")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict[str, Any]:
    return {"calls": dict(calls), "errors": errors}


@app.post("/scenario")
async def scenario(schedule: ScenarioSchedule) -> dict[str, str]:
    global investigate_after
    investigate_after = schedule.investigate_after
    return {"investigate_after": investigate_after.isoformat()}


@app.post("/v1/chat/completions")
async def completion(
    request: CompletionRequest, authorization: str = Header(default="")
) -> dict[str, Any]:
    if authorization != "Bearer offline-test-only":
        raise HTTPException(401, "Only the offline test token is accepted")
    try:
        message = scripted_response(request)
        for call in message.get("tool_calls", []):
            if call["function"]["name"] not in {
                item["function"]["name"] for item in request.tools
            }:
                raise ValueError("Script requested an unavailable tool")
    except (KeyError, ValueError, TypeError, StopIteration) as exc:
        errors.append(str(exc))
        raise HTTPException(422, str(exc)) from None
    calls[request.model] += 1
    return {
        "id": "offline-scripted-completion",
        "object": "chat.completion",
        "created": 0,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
