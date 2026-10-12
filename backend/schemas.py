"""Data contracts between pipeline steps and the API. The prompts ask models for these shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Observation(BaseModel):
    """What the vision step sees in the photo. Facts only, no diagnosis."""

    plant_guess: str = "unknown"
    plant_confidence: float = Field(0.0, ge=0.0, le=1.0)
    symptoms: list[str] = Field(default_factory=list)
    affected_parts: list[str] = Field(default_factory=list)
    pattern: str = ""
    image_quality: Literal["good", "fair", "poor"] = "fair"
    quality_notes: str = ""


class Candidate(BaseModel):
    name: str
    category: Literal["fungal", "bacterial", "viral", "pest", "nutrient", "abiotic", "other"] = (
        "other"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)


class Diagnosis(BaseModel):
    """The reasoning step's output (Nemotron Ultra)."""

    candidates: list[Candidate] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list, max_length=3)
    search_queries: list[str] = Field(default_factory=list, max_length=3)
    reasoning_summary: str = ""


class Source(BaseModel):
    id: int
    title: str
    url: str
    snippet: str = ""


class PlanStep(BaseModel):
    action: str
    why: str = ""
    source_ids: list[int] = Field(default_factory=list)


class CarePlan(BaseModel):
    """The writing step's output (Nemotron Lightning)."""

    summary: str
    steps: list[PlanStep] = Field(default_factory=list)
    check_in_days: int = Field(5, ge=1, le=30)
    escalate: bool = False
    escalate_reason: str = ""


class ModelCall(BaseModel):
    """One model call, shown in the UI's 'How Leaf thought' panel and logged for the README."""

    step: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    note: str = ""


class DiagnoseResponse(BaseModel):
    observation: Observation
    diagnosis: Diagnosis
    plan: CarePlan
    sources: list[Source]
    trace: list[ModelCall]
    low_confidence: bool


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    context: DiagnoseResponse
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    message: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    reply: str
    trace: list[ModelCall]
