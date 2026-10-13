"""A Deep-mode case: everything the investigator has gathered, serializable for any store.

Ids are short and human-readable inside a case (P1 photo, E1 evidence, S1 source); the case
id itself is 128 random bits, so a share link cannot be guessed.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.prompts import sanitize
from backend.schemas import ModelCall, Observation, _unit


def new_case_id() -> str:
    return secrets.token_urlsafe(16)


class Photo(BaseModel):
    id: str
    label: str = "photo"
    full_b64: str  # up to 2048 px, for look_closer crops
    view_b64: str  # up to 1024 px, for the first look
    observation: Observation | None = None


class SourceRef(BaseModel):
    id: str
    url: str
    title: str
    snippet: str = ""
    trusted: bool = False
    passages: list[str] = Field(default_factory=list)  # filled by read_source


class Evidence(BaseModel):
    id: str
    tool: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    photo_id: str | None = None


class Hypothesis(BaseModel):
    name: str
    probability: float = Field(..., ge=0.0, le=1.0)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)


class StepRecord(BaseModel):
    """One tool call, as the gardener sees it in the run log."""

    n: int
    tool: str
    reason: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    evidence_id: str | None = None
    summary: str = ""
    ms: int = 0
    skipped: str = ""  # why it did not run (limit, invalid arguments, tool failure)


class Case(BaseModel):
    id: str = Field(default_factory=new_case_id)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    status: Literal["running", "needs_answers", "done", "failed"] = "running"
    notes: str = ""
    region: str = ""
    lat: float | None = None
    lon: float | None = None
    crop: str | None = None
    photos: list[Photo] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    differential: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    trace: list[ModelCall] = Field(default_factory=list)
    calls_used: int = 0
    step_seq: int = 0
    tool_counts: dict[str, int] = Field(default_factory=dict)
    look_closer_used: int = 0
    differential_updates: int = 0
    cost_usd: float = 0.0
    tavily_credits: int = 0
    questions: list[str] = Field(default_factory=list)
    pending_call_id: str | None = None  # the ask_user call waiting for answers
    asked: int = 0
    summary: str = ""

    # ---- lookups and registration (synchronous, so concurrent tool calls stay consistent)
    def photo(self, photo_id: str) -> Photo | None:
        return next((p for p in self.photos if p.id == photo_id), None)

    def source(self, source_id: str) -> SourceRef | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def add_evidence(self, tool: str, summary: str, **kw: Any) -> Evidence:
        ev = Evidence(id=f"E{len(self.evidence) + 1}", tool=tool, summary=summary, **kw)
        self.evidence.append(ev)
        return ev

    def add_source(self, url: str, title: str, snippet: str, trusted: bool) -> SourceRef:
        existing = next((s for s in self.sources if s.url == url), None)
        if existing:
            return existing
        src = SourceRef(
            id=f"S{len(self.sources) + 1}", url=url, title=title, snippet=snippet, trusted=trusted
        )
        self.sources.append(src)
        return src

    def evidence_ids(self) -> set[str]:
        return {e.id for e in self.evidence}

    def add_trace(self, call: ModelCall) -> None:
        self.trace.append(call)
        self.cost_usd = round(self.cost_usd + (call.cost_usd or 0.0), 6)


MAX_PROBABILITY = 0.9  # photos and web pages never justify near-certainty


def normalise_differential(raw: list[dict[str, Any]], known_evidence: set[str]) -> list[Hypothesis]:
    """Code, not the model, decides what a valid differential is: at most 5 named hypotheses,
    probabilities in 0-1 summing to at most 1, evidence ids that really exist, best first."""
    hyps: list[Hypothesis] = []
    seen: set[str] = set()
    for h in raw[:10]:
        name = sanitize(str(h.get("name", "")), 120).strip()
        if not name or name.lower() in seen:
            continue
        p = _unit(h.get("probability", 0.0))
        if not isinstance(p, float):
            continue
        seen.add(name.lower())
        hyps.append(
            Hypothesis(
                name=name,
                probability=p,
                evidence_for=[e for e in h.get("evidence_for", []) or [] if e in known_evidence],
                evidence_against=[
                    e for e in h.get("evidence_against", []) or [] if e in known_evidence
                ],
            )
        )
    hyps.sort(key=lambda x: x.probability, reverse=True)
    hyps = hyps[:5]
    total = sum(x.probability for x in hyps)
    if total > 1.0:
        for x in hyps:
            x.probability = round(x.probability / total, 3)
    for x in hyps:
        x.probability = min(x.probability, MAX_PROBABILITY)
    return hyps
