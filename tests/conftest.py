"""Offline fakes: unit tests never call Token Factory or Tavily (no credits spent)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.config import Settings
from backend.models import CallResult, ModelError
from backend.pipeline import Leaf
from backend.prompts import CHAT_SYSTEM, DIAGNOSE_SYSTEM, PLAN_SYSTEM, VISION_SYSTEM

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
        DIAGNOSE_SYSTEM: "diagnose",
        PLAN_SYSTEM: "plan",
        CHAT_SYSTEM: "chat",
    }.get(system, "repair")


class FakeLLM:
    """Scripted model. responses[step] is a dict (sent as JSON) or a raw string."""

    def __init__(self, responses: dict[str, Any], failing_models: set[str] | None = None):
        self.responses = responses
        self.failing = failing_models or set()
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, model, messages, **_: Any) -> CallResult:
        step = step_of(messages)
        self.calls.append((step, model))
        if model in self.failing:
            raise ModelError(f"{model}: HTTP 503")
        out = self.responses[step]
        text = out if isinstance(out, str) else json.dumps(out)
        return CallResult(
            text=text, model=model, latency_ms=12, prompt_tokens=100, completion_tokens=50
        )

    async def list_models(self) -> list[dict[str, Any]]:
        return []


class FakeSearcher:
    def __init__(self):
        self.queries: list[str] = []

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
    return Settings(nebius_api_key="test", tavily_api_key="test", vision_model="nvidia/fake-vl")


def make_leaf(settings: Settings, responses: dict[str, Any], failing: set[str] | None = None):
    llm = FakeLLM(responses, failing)
    searcher = FakeSearcher()
    return Leaf(llm, searcher, settings, settings.vision_model), llm, searcher
