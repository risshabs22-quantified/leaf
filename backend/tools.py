"""The investigator's tools: typed arguments, JSON schemas generated from them, and handlers.

Every tool is read-only. A handler raises ToolError for anything the model should hear about
(bad photo id, limit reached, nothing found); the agent turns that into a "skipped" result
instead of crashing. Arguments are validated by pydantic before any handler runs, and every
tool requires a one-sentence `reason` that is shown to the gardener.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.case import Case, normalise_differential
from backend.config import Settings
from backend.models import LLM
from backend.prompts import sanitize
from backend.search import Searcher, rank_sources

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class ToolError(Exception):
    """Something the model should be told (shown as a skipped step), not a crash."""


# ----------------------------------------------------------------------------- arguments


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(
        ..., min_length=3, max_length=300, description="One sentence for the gardener: why."
    )


class Region(BaseModel):
    x: float = Field(..., ge=0, le=1, description="Left edge, 0-1 of photo width")
    y: float = Field(..., ge=0, le=1, description="Top edge, 0-1 of photo height")
    w: float = Field(..., gt=0.02, le=1, description="Width, 0-1")
    h: float = Field(..., gt=0.02, le=1, description="Height, 0-1")


class LookCloserArgs(ToolArgs):
    photo_id: str = Field(..., description="P1, P2 or P3")
    region: Region
    question: str = Field(..., min_length=3, max_length=300)


class SearchArgs(ToolArgs):
    query: str = Field(..., min_length=3, max_length=200)


class HypothesisIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(..., min_length=1, max_length=120)
    probability: float = Field(..., description="0-1")
    evidence_for: list[str] = Field(default_factory=list, description="Evidence ids, e.g. E2")
    evidence_against: list[str] = Field(default_factory=list)


class DifferentialArgs(ToolArgs):
    hypotheses: list[HypothesisIn] = Field(..., min_length=1, max_length=8)


class AskUserArgs(ToolArgs):
    questions: list[str] = Field(..., min_length=1, max_length=5)


class FinishArgs(ToolArgs):
    hypotheses: list[HypothesisIn] = Field(..., min_length=1, max_length=8)
    summary: str = Field(..., min_length=3, max_length=800)


# ----------------------------------------------------------------------------- runtime


@dataclass
class ToolContext:
    llm: LLM
    searcher: Searcher
    settings: Settings
    case: Case
    vision_model: str | None
    emit: Emit


@dataclass
class ToolOutcome:
    summary: str  # one line for the gardener and the model
    data: dict[str, Any] = field(default_factory=dict)  # what the model reads
    evidence: bool = True  # record as evidence (E-id)
    source_ids: list[str] = field(default_factory=list)
    photo_id: str | None = None
    terminal: Literal["finish", "ask"] | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[ToolArgs]
    handler: Callable[[Any, ToolContext], Awaitable[ToolOutcome]]
    counts: bool = True  # counts toward the per-case tool-call limit
    timeout_s: float = 25.0

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": inline_schema(self.args.model_json_schema()),
            },
        }


def inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve $refs and drop titles: smaller prompts, and no $ref support needed server-side."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


# ----------------------------------------------------------------------------- handlers


async def _look_closer(args: LookCloserArgs, ctx: ToolContext) -> ToolOutcome:
    from backend.vision import crop_photo, look_closer  # vision imports models; avoid a cycle

    case = ctx.case
    photo = case.photo(args.photo_id)
    if photo is None:
        ids = ", ".join(p.id for p in case.photos) or "none"
        raise ToolError(f"Unknown photo_id {args.photo_id!r}. Uploaded photos: {ids}.")
    if not ctx.vision_model:
        raise ToolError("No vision model is available right now.")
    if case.look_closer_used >= ctx.settings.deep_max_look_closer:
        raise ToolError("look_closer limit reached for this case.")
    case.look_closer_used += 1
    r = args.region
    try:
        crop = await asyncio.to_thread(crop_photo, photo.full_b64, r.x, r.y, r.w, r.h)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    trace: list = []
    try:
        ans = await look_closer(
            ctx.llm, ctx.vision_model, crop, args.question, trace, ctx.settings.fast_model
        )
    finally:
        for call in trace:
            case.add_trace(call)
    return ToolOutcome(
        summary=f"{photo.label} ({photo.id}): {ans.answer}"[:300],
        data={
            "answer": sanitize(ans.answer, 600),
            "visible_features": [sanitize(f, 160) for f in ans.visible_features[:8]],
            "confidence": ans.confidence,
            "crop_quality": ans.image_quality,
        },
        photo_id=photo.id,
    )


async def _search_sources(args: SearchArgs, ctx: ToolContext) -> ToolOutcome:
    results, credits = await ctx.searcher.search_web(args.query, max_results=6)
    ctx.case.tavily_credits += credits
    ranked = rank_sources(results, limit=5)  # trusted hosts first, then Tavily's score
    if not ranked:
        raise ToolError("No results for that query. Try different words.")
    from backend.search import _trust

    found = []
    for r in ranked:
        trusted = bool(_trust(r.url))
        src = ctx.case.add_source(r.url, sanitize(r.title, 200), sanitize(r.snippet, 400), trusted)
        found.append(
            {
                "source_id": src.id,
                "title": src.title,
                "trusted": trusted,
                "snippet": src.snippet[:300],
            }
        )
    n_trusted = sum(f["trusted"] for f in found)
    return ToolOutcome(
        summary=f"{len(found)} sources for “{sanitize(args.query, 80)}” ({n_trusted} trusted)",
        data={"results": found, "note": "Untrusted results are open-web pages; prefer trusted."},
        source_ids=[f["source_id"] for f in found],
    )


async def _update_differential(args: DifferentialArgs, ctx: ToolContext) -> ToolOutcome:
    case = ctx.case
    if case.differential_updates >= 12:
        raise ToolError("Too many differential updates; continue investigating or finish.")
    case.differential_updates += 1
    hyps = normalise_differential([h.model_dump() for h in args.hypotheses], case.evidence_ids())
    if not hyps:
        raise ToolError("The differential needs at least one named hypothesis.")
    case.differential = hyps
    await ctx.emit({"type": "differential", "hypotheses": [h.model_dump() for h in hyps]})
    return ToolOutcome(summary="Differential updated", evidence=False)


async def _ask_user(args: AskUserArgs, ctx: ToolContext) -> ToolOutcome:
    if ctx.case.asked >= 1:
        raise ToolError("You already asked the gardener once. Finish with what you have.")
    questions = [sanitize(q, 200).strip() for q in args.questions if q.strip()][:3]
    if not questions:
        raise ToolError("Ask at least one question.")
    return ToolOutcome(
        summary=f"Asked {len(questions)} question(s)",
        data={"questions": questions},
        evidence=False,
        terminal="ask",
    )


async def _finish(args: FinishArgs, ctx: ToolContext) -> ToolOutcome:
    hyps = normalise_differential(
        [h.model_dump() for h in args.hypotheses], ctx.case.evidence_ids()
    )
    if not hyps:
        raise ToolError("finish needs at least one named hypothesis.")
    ctx.case.differential = hyps
    ctx.case.summary = sanitize(args.summary, 800)
    return ToolOutcome(summary="Finished", evidence=False, terminal="finish")


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            "look_closer",
            "Crop a region of an uploaded photo and ask the vision model one targeted question "
            "about it, e.g. 'Is there a yellow halo around the spots?'",
            LookCloserArgs,
            _look_closer,
            timeout_s=30,
        ),
        ToolSpec(
            "search_sources",
            "Search the web for extension and university guidance. Returns source ids; trusted "
            "(.edu, .gov, extension services) are listed first.",
            SearchArgs,
            _search_sources,
            timeout_s=20,
        ),
        ToolSpec(
            "update_differential",
            "Record your current differential (1-5 hypotheses with probabilities and evidence "
            "ids). Free: does not count toward the tool budget.",
            DifferentialArgs,
            _update_differential,
            counts=False,
        ),
        ToolSpec(
            "ask_user",
            "Stop and ask the gardener up to 3 short questions that would best separate your top "
            "two hypotheses. Use at most once.",
            AskUserArgs,
            _ask_user,
            counts=False,
        ),
        ToolSpec(
            "finish",
            "Give your final differential with probabilities and the evidence ids behind each "
            "hypothesis, plus a 2-3 sentence summary for the gardener.",
            FinishArgs,
            _finish,
            counts=False,
        ),
    ]
}


def tool_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    return [TOOLS[n].schema() for n in (names or list(TOOLS))]
