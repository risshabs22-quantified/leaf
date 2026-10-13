"""Data contracts between pipeline steps and the API. The prompts ask models for these shapes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Models drift slightly off-spec (82 instead of 0.82, "Good", a 4th query). Normalise
# those instead of failing a whole pipeline step; real garbage still fails validation.


def _unit(v: Any) -> Any:
    """0-1 probability; accepts percentages (82 -> 0.82) and clamps."""
    if isinstance(v, bool):
        raise ValueError("a boolean is not a probability")  # pydantic would read True as 1.0
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip()
    try:
        x = float(v)
    except (TypeError, ValueError):
        return v
    if 1 < x <= 100:
        x /= 100
    return min(1.0, max(0.0, x))


def _lower(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _clip_list(v: Any, n: int) -> Any:
    return v[:n] if isinstance(v, list) else v


class Observation(BaseModel):
    """What the vision step sees in the photo. Facts only, no diagnosis."""

    plant_guess: str = "unknown"
    plant_confidence: float = Field(0.0, ge=0.0, le=1.0)
    symptoms: list[str] = Field(default_factory=list)
    affected_parts: list[str] = Field(default_factory=list)
    pattern: str = ""
    image_quality: Literal["good", "fair", "poor"] = "fair"
    quality_notes: str = ""

    @field_validator("plant_confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _unit(v)

    @field_validator("image_quality", mode="before")
    @classmethod
    def _quality(cls, v: Any) -> Any:
        v = _lower(v)
        return v if v in ("good", "fair", "poor") else "fair"

    @field_validator("plant_guess", mode="before")
    @classmethod
    def _guess(cls, v: Any) -> Any:
        return v if isinstance(v, str) and v.strip() else "unknown"


class Candidate(BaseModel):
    name: str
    category: Literal["fungal", "bacterial", "viral", "pest", "nutrient", "abiotic", "other"] = (
        "other"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _unit(v)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: Any) -> Any:
        v = _lower(v)
        known = ("fungal", "bacterial", "viral", "pest", "nutrient", "abiotic")
        return v if v in known else "other"


class Diagnosis(BaseModel):
    """The reasoning step's output (Nemotron Ultra)."""

    candidates: list[Candidate] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list, max_length=3)
    search_queries: list[str] = Field(default_factory=list, max_length=3)
    reasoning_summary: str = ""

    @field_validator("follow_up_questions", "search_queries", mode="before")
    @classmethod
    def _at_most_three(cls, v: Any) -> Any:
        return _clip_list(v, 3)


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

    @field_validator("check_in_days", mode="before")
    @classmethod
    def _days(cls, v: Any) -> Any:
        try:
            return min(30, max(1, int(float(v))))
        except (TypeError, ValueError):
            return v


class ModelCall(BaseModel):
    """One model call, shown in the UI's 'How Leaf thought' panel and logged for the README."""

    step: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
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
