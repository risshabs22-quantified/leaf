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


Tri = Literal["present", "absent", "unsure"]


def _choice(allowed: tuple[str, ...]):
    def fix(v: Any) -> Any:
        v = v.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(v, str) else v
        return v if v in allowed else "unsure"

    return fix


TRI = ("present", "absent", "unsure")


class Features(BaseModel):
    """The features extension diagnostic keys ask about, read from the photo. Anything the
    model is not sure of is "unsure", which is not evidence either way."""

    lesion_shape: Literal["circular", "angular", "irregular", "none", "unsure"] = "unsure"
    lesion_margin: Literal["sharp", "diffuse", "water_soaked", "none", "unsure"] = "unsure"
    yellow_halo: Tri = "unsure"
    concentric_rings: Tri = "unsure"
    dark_dots_in_lesions: Tri = "unsure"
    powdery_growth: Tri = "unsure"
    fuzzy_growth: Tri = "unsure"
    rust_pustules: Tri = "unsure"
    growth_side: Literal["upper", "lower", "both", "none", "unsure"] = "unsure"
    yellowing_pattern: Literal[
        "interveinal", "veins", "uniform", "margins", "mottled", "none", "unsure"
    ] = "unsure"
    affected_first: Literal["older_lower", "new_growth", "whole_plant", "unsure"] = "unsure"
    insects: Tri = "unsure"
    frass: Tri = "unsure"
    webbing: Tri = "unsure"

    @field_validator(
        "yellow_halo",
        "concentric_rings",
        "dark_dots_in_lesions",
        "powdery_growth",
        "fuzzy_growth",
        "rust_pustules",
        "insects",
        "frass",
        "webbing",
        mode="before",
    )
    @classmethod
    def _tri(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return "present" if v else "absent"
        return _choice(TRI)(v)

    @field_validator("lesion_shape", mode="before")
    @classmethod
    def _shape(cls, v: Any) -> Any:
        return _choice(("circular", "angular", "irregular", "none"))(v)

    @field_validator("lesion_margin", mode="before")
    @classmethod
    def _margin(cls, v: Any) -> Any:
        return _choice(("sharp", "diffuse", "water_soaked", "none"))(v)

    @field_validator("growth_side", mode="before")
    @classmethod
    def _side(cls, v: Any) -> Any:
        return _choice(("upper", "lower", "both", "none"))(v)

    @field_validator("yellowing_pattern", mode="before")
    @classmethod
    def _yellow(cls, v: Any) -> Any:
        return _choice(("interveinal", "veins", "uniform", "margins", "mottled", "none"))(v)

    @field_validator("affected_first", mode="before")
    @classmethod
    def _first(cls, v: Any) -> Any:
        return _choice(("older_lower", "new_growth", "whole_plant"))(v)


class Observation(BaseModel):
    """What the vision step sees in the photo. Facts only, no diagnosis."""

    plant_guess: str = "unknown"
    plant_confidence: float = Field(0.0, ge=0.0, le=1.0)
    symptoms: list[str] = Field(default_factory=list)
    affected_parts: list[str] = Field(default_factory=list)
    pattern: str = ""
    image_quality: Literal["good", "fair", "poor"] = "fair"
    quality_notes: str = ""
    features: Features | None = None  # the structured checklist, when enabled

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


class Garden(BaseModel):
    """The gardener's preferences, applied to the care plan in code (backend/actives.py)."""

    organic: bool = False  # only methods allowed in organic growing
    pets: bool = False  # pets or children use this garden


class PlanStep(BaseModel):
    action: str
    why: str = ""
    source_ids: list[int] = Field(default_factory=list)
    flag: str = ""  # set by the claim check when a source only partly supports the step
    # Set in code from backend/actives.py, never by the model:
    actives: list[str] = Field(default_factory=list)  # products named in the step
    organic: bool | None = None  # None when the step names no product
    pet_risk: str = ""  # "", low, caution or high
    bee_risk: bool = False
    precautions: list[str] = Field(default_factory=list)


class CarePlan(BaseModel):
    """The writing step's output (Nemotron Lightning)."""

    summary: str
    steps: list[PlanStep] = Field(default_factory=list)
    watch_for: list[str] = Field(default_factory=list, max_length=3)
    sample_if: str = ""
    check_in_days: int = Field(5, ge=1, le=30)
    escalate: bool = False  # set in code from the verdict: True means "send to a lab"
    escalate_reason: str = ""
    # Set in code from backend/actives.py:
    removed: list[str] = Field(default_factory=list)  # steps dropped for the gardener's preferences
    cautions: list[str] = Field(default_factory=list)  # plan-wide (oil with sulfur, bees, pets)
    spray: bool = False  # the plan applies a product, so a spray window helps
    heat_limit_c: float = 32.0  # hottest hour to spray in (29 C for oils and sulfur)
    bee_safe_hours: bool = False  # a product harms bees: spray only when they are not foraging

    @field_validator("watch_for", mode="before")
    @classmethod
    def _watch(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [v] if v.strip() else []
        return [str(x) for x in v if str(x).strip()][:3] if isinstance(v, list) else []

    @field_validator("sample_if", mode="before")
    @classmethod
    def _sample(cls, v: Any) -> Any:
        return v if isinstance(v, str) else ""

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
    case_id: str = ""  # journal id: the share link is /c/<case_id>
    owner_token: str = ""  # given only to the creator; allows check-ins (never shared)
    observation: Observation
    diagnosis: Diagnosis
    plan: CarePlan
    sources: list[Source]
    trace: list[ModelCall]
    notice: str = ""  # something the gardener should know about how this answer was made
    low_confidence: bool  # True unless the verdict is "confident"
    verdict: Literal["confident", "likely", "lab"] = "likely"
    verdict_reason: str = ""
    garden: Garden = Field(default_factory=Garden)


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


Intent = Literal["doctor", "care", "planner", "research", "concierge"]
Risk = Literal["none", "prompt_injection", "harmful", "off_topic"]


class FrontDoorOut(BaseModel):
    """What Nano returns at the front door (prompts.FRONT_DOOR_SYSTEM)."""

    intent: Intent = "concierge"
    multi_step: bool = False
    safe: bool = True
    risk: Risk = "none"
    confidence: float = Field(0.5, ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> float:
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.5


class FrontDoorDecision(FrontDoorOut):
    """The routing decision. `agent` handles the request; `orchestrator` is Ultra when the
    request needs several steps. `source` says how it was decided: explicit (the UI said),
    rules (code only), cache, or model."""

    agent: str
    orchestrator: str = ""
    source: Literal["explicit", "rules", "cache", "model"]
    trace: list[ModelCall] = Field(default_factory=list)


class ExtractedFact(BaseModel):
    field: str
    value: Any = None
    quote: str = ""
    passage: str = ""


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)


class FactVerdict(BaseModel):
    i: int
    supported: bool = False
    note: str = ""


class FactVerdicts(BaseModel):
    verdicts: list[FactVerdict] = Field(default_factory=list)


class PlantCandidate(BaseModel):
    scientific_name: str
    common_name: str = ""
    confidence: float = Field(0.0, ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> float:
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.0


class IdentifyOut(BaseModel):
    plant_visible: bool = True
    image_quality: str = "fair"
    features: list[str] = Field(default_factory=list)
    candidates: list[PlantCandidate] = Field(default_factory=list, max_length=5)


class CheckedCandidate(BaseModel):
    scientific_name: str
    probability: float = Field(0.0, ge=0, le=1)
    consistent: bool = True
    reason: str = ""
    source_ids: list[str] = Field(default_factory=list)

    @field_validator("probability", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> float:
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.0


class IdentifyCheck(BaseModel):
    ranking: list[CheckedCandidate] = Field(default_factory=list)
    summary: str = ""
