"""The investigator's tools: typed arguments, JSON schemas generated from them, and handlers.

Every tool is read-only. A handler raises ToolError for anything the model should hear about
(bad photo id, limit reached, nothing found); the agent turns that into a "skipped" result
instead of crashing. Arguments are validated by pydantic before any handler runs, and every
tool requires a one-sentence `reason` that is shown to the gardener.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.case import Case, normalise_differential
from backend.conditions import ConditionsError, Place, WeatherClient, summarise
from backend.config import Settings
from backend.crops import infer_crop
from backend.models import LLM
from backend.prompts import sanitize
from backend.search import Searcher, _trust, rank_sources

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class ToolError(Exception):
    """Something the model should be told (shown as a skipped step), not a crash."""


# ----------------------------------------------------------------------------- arguments


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Long text is trimmed when shown, not rejected: a refusal costs the model a whole turn.
    reason: str = Field(
        ..., min_length=3, max_length=2000, description="One sentence for the gardener: why."
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


class ConditionsArgs(ToolArgs):
    crop: str | None = Field(
        None, max_length=60, description="The crop, e.g. tomato; omit if unknown"
    )


class ReadSourceArgs(ToolArgs):
    source_id: str = Field(..., max_length=8, description="An id from search_sources, e.g. S2")
    query: str = Field(..., min_length=3, max_length=200, description="What to look for in it")


class AlertsArgs(ToolArgs):
    region: str = Field(..., min_length=2, max_length=100)


def _alias(data: Any, target: str, *names: str) -> Any:
    """Models sometimes use a near-synonym for an argument name; accept the common ones."""
    if isinstance(data, dict) and target not in data:
        for n in names:
            if n in data:
                return {**data, target: data[n]}
    return data


class HypothesisIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _synonyms(cls, data: Any) -> Any:
        data = _alias(data, "name", "hypothesis", "diagnosis", "disease", "cause")
        return _alias(data, "probability", "p", "prob", "confidence", "likelihood")

    name: str = Field(..., min_length=1, max_length=120)
    probability: float = Field(..., description="0-1")
    evidence_for: list[str] = Field(default_factory=list, description="Evidence ids, e.g. E2")
    evidence_against: list[str] = Field(default_factory=list)


class DifferentialArgs(ToolArgs):
    hypotheses: list[HypothesisIn] = Field(..., min_length=1, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def _synonyms(cls, data: Any) -> Any:
        return _alias(data, "hypotheses", "differential", "diagnoses", "candidates")


class AskUserArgs(ToolArgs):
    questions: list[str] = Field(..., min_length=1, max_length=5)


class FinishArgs(ToolArgs):
    # The summary explains the result, so finish's reason is optional. Hypotheses may be
    # omitted when update_differential already recorded them.
    reason: str = Field("Final differential.", max_length=2000)
    hypotheses: list[HypothesisIn] = Field(default_factory=list, max_length=8)
    summary: str = Field("", max_length=6000)

    @model_validator(mode="before")
    @classmethod
    def _synonyms(cls, data: Any) -> Any:
        data = _alias(data, "hypotheses", "differential", "diagnoses", "candidates", "diagnosis")
        return _alias(data, "summary", "explanation", "rationale", "conclusion", "reason")


# ----------------------------------------------------------------------------- runtime


@dataclass
class ToolContext:
    llm: LLM
    searcher: Searcher
    settings: Settings
    case: Case
    vision_model: str | None
    emit: Emit
    weather: WeatherClient | None = None


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
            ctx.llm, ctx.vision_model, crop, args.question, trace, ctx.settings.repair_model
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


async def _get_conditions(args: ConditionsArgs, ctx: ToolContext) -> ToolOutcome:
    case = ctx.case
    weather = ctx.weather or WeatherClient()
    crop = infer_crop(args.crop) or case.crop
    try:
        if case.lat is not None and case.lon is not None:
            place: Place | None = Place("your location (approximate)", case.lat, case.lon)
        elif case.region:
            place = await weather.geocode(case.region)
            if place is None:
                raise ToolError(
                    f"Could not find {sanitize(case.region, 60)!r}; a city name works best."
                )
        else:
            raise ToolError(
                "No location was given, so weather cannot be checked. Continue without it, or "
                "ask the gardener for a city."
            )
        hours, _tz = await weather.history(place.lat, place.lon)
        summary = summarise(hours, crop, place)
    except ConditionsError as exc:  # geocoding or weather outage: a clean message
        raise ToolError(str(exc)) from exc
    await ctx.emit({"type": "weather", **copy.deepcopy(summary)})  # the UI keeps full data
    daily = summary["daily"]
    data = copy.deepcopy({k: v for k, v in summary.items() if k not in ("daily", "attribution")})
    # Compact arrays instead of 14 verbose rows: the model reads this on every later turn.
    data.update(
        daily_rain_mm=[d["rain_mm"] for d in daily],
        daily_wet_hours=[d["wet_h"] for d in daily],
        daily_tmin_c=[d["tmin"] for d in daily],
        daily_tmax_c=[d["tmax"] for d in daily],
    )
    for idx in data.get("indices", []):
        idx.pop("daily", None)
    line = (
        f"{place.label}: {summary['rain_mm_total']} mm rain on {summary['rainy_days']} days, "
        f"{summary['wet_hours_total']} humid (wet) hours in {summary['days']} days"
    )
    if summary["indices"]:
        first = summary["indices"][0]
        value = first.get("last_7_days", first.get("count"))
        line += (
            f"; {first['name']}: {value} (last 7 days)"
            if "last_7_days" in first
            else (f"; {first['name']}: {value}")
        )
    return ToolOutcome(summary=line, data=data)


def _safe_url(url: str) -> bool:
    """Defence in depth for read_source. The URL already comes from this case's own search
    results, and Tavily fetches pages on its servers, not ours. This refuses non-web schemes,
    credentials in the URL, localhost and internal names, and literal or numeric private
    addresses. It cannot see where a public DNS name resolves."""
    from urllib.parse import urlparse

    parts = urlparse(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return False
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local", ".lan")):
        return False
    if re.fullmatch(r"(0x)?[0-9a-f.x]+", host) and not re.search(r"[g-wyz]", host):
        # numeric forms such as 2130706433, 0x7f000001 or 127.1 are IPv4 in disguise
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return ip.is_global
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a domain name
    if ip.version == 6 and ip.ipv4_mapped:
        return ip.ipv4_mapped.is_global
    return ip.is_global


async def _read_source(args: ReadSourceArgs, ctx: ToolContext) -> ToolOutcome:
    case = ctx.case
    src = case.source(args.source_id.strip().upper())
    if src is None:
        ids = ", ".join(s.id for s in case.sources) or "none yet; call search_sources first"
        raise ToolError(f"Unknown source_id {sanitize(args.source_id, 8)!r}. Known: {ids}.")
    # The URL comes from this case's own search results, never from the model: no SSRF.
    if not _safe_url(src.url):
        raise ToolError("That source cannot be opened.")
    passages, credits = await ctx.searcher.extract(src.url, args.query)
    case.tavily_credits += credits
    clean = [sanitize(p, 900) for p in passages if p.strip()][:3]
    if not clean:
        raise ToolError("That page could not be read. Try another source.")
    src.passages = list(dict.fromkeys(src.passages + clean))[:6]
    return ToolOutcome(
        summary=f"Read {src.id} ({src.title[:70]}): {len(clean)} relevant passage(s)",
        data={"source_id": src.id, "trusted": src.trusted, "passages": clean},
        source_ids=[src.id],
    )


async def _check_alerts(args: AlertsArgs, ctx: ToolContext) -> ToolOutcome:
    case = ctx.case
    crop = case.crop or "plant"
    region = args.region.strip()
    if region.lower() in ("", "unknown", "browser location", "your location"):
        # Only the browser location is known: use the place get_conditions found, if any.
        region = next(
            (e.data.get("place", "") for e in case.evidence if e.tool == "get_conditions"), ""
        )
        if not region or region.startswith("your location"):
            raise ToolError("No named region is known, so regional news cannot be searched.")
    query = f"{crop} disease outbreak {sanitize(region, 100)}"
    results, credits = await ctx.searcher.search_web(
        query, topic="news", time_range="month", max_results=5
    )
    ctx.case.tavily_credits += credits
    dates = {r.get("url"): r.get("published_date", "") for r in results}
    found = []
    for r in rank_sources(results, limit=5):
        src = ctx.case.add_source(
            r.url, sanitize(r.title, 200), sanitize(r.snippet, 400), bool(_trust(r.url))
        )
        found.append(
            {
                "source_id": src.id,
                "title": src.title,
                "published": sanitize(dates.get(r.url) or "", 40),
                "trusted": src.trusted,
                "snippet": src.snippet[:250],
            }
        )
    if not found:  # finding nothing is a result too
        return ToolOutcome(
            summary=f"No outbreak news in the last month for {sanitize(args.region, 60)}",
            data={
                "results": [],
                "note": "No recent reports found; this does not rule anything out.",
            },
        )
    return ToolOutcome(
        summary=f"{len(found)} recent news item(s) for {sanitize(args.region, 60)}",
        data={"results": found, "note": "News is untrusted; treat it as a lead, not proof."},
        source_ids=[f["source_id"] for f in found],
    )


async def _search_sources(args: SearchArgs, ctx: ToolContext) -> ToolOutcome:
    results, credits = await ctx.searcher.search_web(args.query, max_results=6)
    ctx.case.tavily_credits += credits
    ranked = rank_sources(results, limit=5)  # trusted hosts first, then Tavily's score
    if not ranked:
        raise ToolError("No results for that query. Try different words.")

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
    case = ctx.case
    hyps = normalise_differential([h.model_dump() for h in args.hypotheses], case.evidence_ids())
    hyps = hyps or case.differential  # fall back to the last recorded differential
    if not hyps:
        raise ToolError("finish needs at least one named hypothesis with a probability.")
    case.differential = hyps
    case.summary = sanitize(args.summary, 800).strip() or case.summary
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
            "get_conditions",
            "The last 14 days of local weather (rain, humid hours, temperature) and, only for "
            "crops with a published rule, a disease-pressure index. A risk signal, not proof.",
            ConditionsArgs,
            _get_conditions,
            timeout_s=20,
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
            "read_source",
            "Read the passages of one search result most relevant to your question. Only "
            "accepts a source_id returned by search_sources or check_alerts.",
            ReadSourceArgs,
            _read_source,
            timeout_s=25,
        ),
        ToolSpec(
            "check_alerts",
            "Search news from the last month for disease outbreak reports in a region.",
            AlertsArgs,
            _check_alerts,
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
