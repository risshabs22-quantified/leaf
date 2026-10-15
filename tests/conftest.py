"""Offline fakes: unit tests never call Token Factory or Tavily (no credits spent)."""

from __future__ import annotations

import os

os.environ["LEAF_JOURNAL_PATH"] = "memory"  # tests never write a journal file
os.environ["LEAF_PLATFORM_DB"] = "memory"  # nor a platform database
os.environ.pop("LEAF_PLATFORM_URL", None)  # and never reach a bridge

import copy
import itertools
import json
from typing import Any

import pytest

from backend.config import Settings
from backend.models import CallResult, ModelError, ToolCall
from backend.pipeline import Leaf
from backend.prompts import (
    AGENT_SYSTEM,
    CHAT_SYSTEM,
    CHECKIN_PLAN_SYSTEM,
    CHECKIN_VISION_SYSTEM,
    CLAIM_CHECK_SYSTEM,
    CRITIC_SYSTEM,
    DIAGNOSE_SYSTEM,
    FRONT_DOOR_SYSTEM,
    LOOK_CLOSER_SYSTEM,
    PLAN_DEEP_SYSTEM,
    PLAN_SYSTEM,
    VISION_CHECKLIST_SYSTEM,
    VISION_SYSTEM,
)

OBS = {
    "plant_guess": "tomato",
    "plant_confidence": 0.9,
    "symptoms": ["brown concentric spots on lower leaves", "yellow halo"],
    "affected_parts": ["older leaves"],
    "pattern": "bottom-up",
    "image_quality": "good",
    "quality_notes": "",
}

DIAG_HIGH = {
    "candidates": [
        {
            "name": "Early blight",
            "category": "fungal",
            "confidence": 0.82,
            "evidence_for": ["concentric rings"],
            "evidence_against": [],
        },
        {"name": "Septoria leaf spot", "category": "fungal", "confidence": 0.1},
    ],
    "follow_up_questions": [],
    "search_queries": ["tomato early blight management"],
    "reasoning_summary": "Ringed spots on old leaves point to early blight.",
}

DIAG_LOW = {
    **DIAG_HIGH,
    "candidates": [
        {"name": "Early blight", "category": "fungal", "confidence": 0.4},
        {"name": "Nitrogen deficiency", "category": "nutrient", "confidence": 0.35},
    ],
    "follow_up_questions": ["Are the spots ringed like a target?"],
}

PLAN = {
    "summary": "Likely early blight. Remove affected leaves and improve airflow.",
    "steps": [
        {"action": "Remove spotted lower leaves", "why": "Slows spread", "source_ids": [1, 99]},
        {"action": "Water at the base in the morning", "why": "Dry leaves", "source_ids": []},
    ],
    "check_in_days": 5,
    "escalate": False,
    "escalate_reason": "",
}


def step_of(messages: list[dict[str, Any]]) -> str:
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    return {
        VISION_SYSTEM: "vision",
        VISION_CHECKLIST_SYSTEM: "vision",
        DIAGNOSE_SYSTEM: "diagnose",
        PLAN_SYSTEM: "plan",
        CHAT_SYSTEM: "chat",
        AGENT_SYSTEM: "investigate",
        LOOK_CLOSER_SYSTEM: "look_closer",
        PLAN_DEEP_SYSTEM: "plan",
        CRITIC_SYSTEM: "critic",
        CLAIM_CHECK_SYSTEM: "claim_check",
        CHECKIN_VISION_SYSTEM: "checkin_compare",
        CHECKIN_PLAN_SYSTEM: "checkin_plan",
        FRONT_DOOR_SYSTEM: "front_door",
    }.get(system, "repair")


_call_ids = itertools.count(1)


def assert_tool_protocol(messages: list[dict[str, Any]]) -> None:
    """What OpenAI-compatible servers enforce: every assistant tool call is answered by exactly
    one tool message with its id before the next assistant turn."""
    pending: list[str] = []
    for m in messages:
        if m["role"] == "assistant":
            assert not pending, f"unanswered tool calls before the next turn: {pending}"
            pending = [c["id"] for c in m.get("tool_calls", [])]
        elif m["role"] == "tool":
            assert m["tool_call_id"] in pending, (
                f"tool message for unknown call {m['tool_call_id']}"
            )
            pending.remove(m["tool_call_id"])
    assert not pending, f"unanswered tool calls: {pending}"


def turn(*calls: tuple[str, Any]) -> list[tuple[str, Any]]:
    """One scripted investigator turn: (tool name, arguments dict or raw string)."""
    return list(calls)


class FakeLLM:
    """Scripted model. responses[step] is a dict (sent as JSON) or a raw string."""

    def __init__(
        self,
        responses: dict[str, Any],
        failing_models: set[str] | None = None,
        tool_script: list[Any] | None = None,
        turn_cost: float = 0.001,
    ):
        self.responses = responses
        self.failing = failing_models or set()
        self.tool_script = list(tool_script or [])  # turns: list of calls, a str, or a callable
        self.tool_kwargs: list[dict[str, Any]] = []
        self.turn_cost = turn_cost
        self.calls: list[tuple[str, str]] = []
        self.sent: list[tuple[str, list[dict[str, Any]]]] = []  # prompts, for injection tests
        self.kwargs: list[tuple[str, dict[str, Any]]] = []  # call settings, e.g. max_tokens

    async def complete(self, *, model, messages, **kwargs: Any) -> CallResult:
        step = step_of(messages)
        self.calls.append((step, model))
        self.sent.append((step, messages))
        self.kwargs.append((step, kwargs))
        if model in self.failing:
            raise ModelError(f"{model}: HTTP 503")
        if step not in self.responses:  # unscripted step: behave like an unavailable model
            raise ModelError(f"{model}: no scripted response for {step}")
        out = self.responses[step]
        text = out if isinstance(out, str) else json.dumps(out)
        return CallResult(
            text=text,
            model=model,
            latency_ms=12,
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.0005,  # so budgets see vision, look_closer and plan spend too
        )

    async def complete_tools(
        self, *, model, messages, tools, tool_choice="auto", **kwargs: Any
    ) -> CallResult:
        names = [t["function"]["name"] for t in tools]
        assert_tool_protocol(messages)
        self.calls.append(("investigate", model))
        self.sent.append(("investigate", copy.deepcopy(messages)))
        self.tool_kwargs.append({"model": model, "tools": names, "tool_choice": tool_choice})
        if model in self.failing:
            raise ModelError(f"{model}: HTTP 503")
        step = self.tool_script.pop(0) if self.tool_script else "I have nothing more to add."
        if callable(step):
            step = step(messages=messages, tools=names, tool_choice=tool_choice)
        if isinstance(step, Exception):
            raise step
        base = dict(
            model=model,
            latency_ms=10,
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=self.turn_cost,
        )
        if isinstance(step, str):
            return CallResult(text=step, finish_reason="stop", **base)
        calls = [
            ToolCall(
                id=f"call_{next(_call_ids)}",
                name=name,
                arguments=args if isinstance(args, str) else json.dumps(args),
            )
            for name, args in step
        ]
        return CallResult(text="", tool_calls=calls, finish_reason="tool_calls", **base)

    async def list_models(self) -> list[dict[str, Any]]:
        return []


class FakeSearcher:
    def __init__(self):
        self.queries: list[str] = []

    async def search_web(self, query: str, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.web_calls = getattr(self, "web_calls", []) + [(query, kwargs)]
        return await self.search(query), 1

    async def extract(self, url: str, query: str) -> tuple[list[str], int]:
        self.extracted = getattr(self, "extracted", []) + [url]
        return [f"Passage about {query} from {url}."], 1

    async def search(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [
            {
                "url": "https://blog.example.com/tomato",
                "title": "Blog",
                "content": "x",
                "score": 0.9,
            },
            {
                "url": "https://extension.umn.edu/early-blight",
                "title": "UMN",
                "content": "y",
                "score": 0.5,
            },
        ]


@pytest.fixture
def settings() -> Settings:
    # Blend 0: agent tests check the investigator's own ranking; the blend has its own tests.
    return Settings(
        nebius_api_key="test",
        tavily_api_key="test",
        vision_model="nvidia/fake-vl",
        deep_blend_first=0.0,
    )


def make_leaf(
    settings: Settings,
    responses: dict[str, Any],
    failing: set[str] | None = None,
    tool_script: list[Any] | None = None,
    turn_cost: float = 0.001,
):
    llm = FakeLLM(responses, failing, tool_script, turn_cost)
    searcher = FakeSearcher()
    return Leaf(llm, searcher, settings, settings.vision_model), llm, searcher
