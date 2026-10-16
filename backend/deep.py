"""Deep mode: run one case through the investigator, then write the care plan.

    photos --vision (thinking off, in parallel)--> first looks
           --investigator (Ultra + tools)--> differential with evidence ids
           --Lightning--> care plan from the case's sources
Any failure in the investigation falls back to the Quick pipeline, and the response says so.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.actives import apply_garden
from backend.agent import AgentError, AgentResult, Investigator
from backend.case import Case, Evidence, Hypothesis, Photo, StepRecord, normalise_differential
from backend.claims import (
    ClaimFlag,
    check_summary,
    code_checks,
    ensure_passages,
    judge,
    write_plan,
)
from backend.conditions import WeatherClient
from backend.critic import (
    Confidence,
    CriticOpinion,
    add_alternative,
    blend,
    combine,
    second_opinion,
)
from backend.crops import infer_crop
from backend.handoff import LabHandoff, build_handoff
from backend.models import ModelError, complete_json
from backend.pipeline import Leaf, apply_verdict
from backend.prompts import DIAGNOSE_SYSTEM, sanitize
from backend.radar.prior import REGIONAL, apply_prior_to_hypotheses
from backend.schemas import (
    Candidate,
    CarePlan,
    DiagnoseResponse,
    Diagnosis,
    Garden,
    ModelCall,
    Observation,
    Source,
)
from backend.search import PAUSED_NOTICE, _trust, search_paused
from backend.store import CaseStore
from backend.verdict import Decision, decide, deep_thresholds, more_cautious
from backend.vision import describe

log = logging.getLogger("leaf.deep")

Emit = Callable[[dict[str, Any]], Awaitable[None]]

FIRST_LOOK_TIMEOUT_S = 25
SEED_TIMEOUT_S = 20
CRITIC_TIMEOUT_S = 35  # Super with reasoning: ~18 s measured; runs alongside the plan
READ_TIMEOUT_S = 18  # may search once, then read two pages
PLAN_TIMEOUT_S = 20
CLAIM_TIMEOUT_S = 12
QUICK_FALLBACK_TIMEOUT_S = 40  # the request deadline (wall clock + 45 s) leaves room for it


async def _ignore(_: dict[str, Any]) -> None:
    return None


# Fitted on the tune split (44 logged Deep cases, docs/AGENT_V2.md): a second opinion sends a
# case to a lab on its own only when it gives the leader under even odds.
CRITIC_ESCALATES_BELOW = 0.5


def deep_decision(
    case: Case,
    confidence: Confidence,
    critic: CriticOpinion | None,
    vision_model: str | None = None,
) -> Decision:
    """Deep's verdict: the shared rules, scored on the combined confidence."""
    disagreement = ""
    hyps = case.differential
    strong = critic is None or critic.leading_probability < CRITIC_ESCALATES_BELOW
    if hyps and confidence.disagreement and strong:
        alt = f" ({hyps[0].name} vs {critic.alternative})" if critic and critic.alternative else ""
        disagreement = (
            f"Leaf and its second opinion disagree{alt}. A diagnostic lab can tell them apart."
        )
    # Photos no model could read are as unusable as a poor photo.
    quality = "poor" if case.photos and confidence.quality == "none" else confidence.quality
    also = (
        [(critic.alternative, critic.alternative_probability)]
        if critic and critic.alternative
        else []
    )
    return decide(
        [(h.name, h.probability) for h in hyps],
        quality,
        deep_thresholds(vision_model),
        also_named=also,
        top_score=confidence.value,
        disagreement=disagreement,
        contested_by=(critic.alternative if critic else "") if confidence.disagreement else None,
    )


class CaseStateError(Exception):
    """The case is not in a state that allows this action (e.g. answers sent twice)."""


class PhotoInfo(BaseModel):
    id: str
    label: str
    observation: Observation | None = None


class AutoDecision(BaseModel):
    """Auto mode: whether Leaf went on from Quick to a full investigation, and why."""

    investigated: bool
    reason: str


class DeepResponse(DiagnoseResponse):
    """A superset of the Quick response, so the same page can render either."""

    mode: Literal["deep", "quick"] = "deep"
    case_id: str = ""
    status: Literal["done", "needs_answers", "fallback"] = "done"
    notice: str = ""
    questions: list[str] = Field(default_factory=list)
    differential: list[Hypothesis] = Field(default_factory=list)
    first_differential: list[Hypothesis] = Field(default_factory=list)
    investigator_differential: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    photos: list[PhotoInfo] = Field(default_factory=list)
    summary: str = ""
    limit: str = ""
    cost_usd: float = 0.0
    tavily_credits: int = 0
    confidence: Confidence | None = None
    critic: CriticOpinion | None = None
    claim_flags: list[ClaimFlag] = Field(default_factory=list)
    handoff: LabHandoff | None = None
    auto: AutoDecision | None = None


def _join(*notices: str) -> str:
    return " ".join(n for n in notices if n)


def _source_int(source_id: str) -> int:
    return int(source_id.lstrip("S") or 0)


class DeepLeaf:
    def __init__(self, leaf: Leaf, store: CaseStore, weather: WeatherClient | None = None):
        self.leaf = leaf
        self.store = store
        self.investigator = Investigator(
            leaf.llm, leaf.searcher, leaf.settings, leaf.vision_model, weather=weather
        )

    # ------------------------------------------------------------------ entry points

    async def start(
        self,
        *,
        photos: list[tuple[str, str, str]],  # (label, full_b64, view_b64)
        notes: str = "",
        region: str = "",
        lat: float | None = None,
        lon: float | None = None,
        emit: Emit = _ignore,
        garden: Garden | None = None,
    ) -> DeepResponse:
        case = Case(
            notes=notes,
            region=region,
            garden=garden or Garden(),
            lat=None if lat is None else round(lat, 1),  # never keep a precise location
            lon=None if lon is None else round(lon, 1),
            photos=[
                Photo(id=f"P{i + 1}", label=label, full_b64=full, view_b64=view)
                for i, (label, full, view) in enumerate(photos)
            ],
        )
        await emit({"type": "case", "case_id": case.id})
        # One deadline for the whole case: first looks and investigation share the wall clock.
        deadline = self.investigator.clock() + self.leaf.settings.deep_wall_s
        await self._first_looks(case, emit, deadline)
        case.crop = infer_crop(
            notes, *(p.observation.plant_guess for p in case.photos if p.observation)
        )
        if self.leaf.settings.deep_seed:
            await self._seed_differential(case, emit, deadline)
        return await self._investigate(
            case, emit, lambda: self.investigator.run(case, emit, deadline)
        )

    async def resume(self, case: Case, answers: list[str], emit: Emit = _ignore) -> DeepResponse:
        # Checked with no await before the investigator marks the case running, so two
        # answer submissions for one case cannot both resume it.
        if case.status != "needs_answers":
            raise CaseStateError("This case is not waiting for answers.")
        deadline = self.investigator.clock() + self.leaf.settings.deep_wall_s
        quick = DiagnoseResponse.model_validate(case.quick_result) if case.quick_result else None
        return await self._investigate(
            case,
            emit,
            lambda: self.investigator.resume(case, answers, emit, deadline),
            quick=quick,
        )

    async def auto(
        self,
        *,
        photos: list[tuple[str, str, str]],
        notes: str = "",
        region: str = "",
        lat: float | None = None,
        lon: float | None = None,
        emit: Emit = _ignore,
        may_investigate: Callable[[], str] = lambda: "",
        garden: Garden | None = None,
    ) -> DeepResponse:
        """Quick first. Only when Quick is unsure does Leaf open a Deep case, which starts from
        Quick's look at the first photo, its differential, evidence and sources instead of
        repeating them. `may_investigate` returns "" to allow that, or why it can't (the
        daily Deep cap), and is called only when an investigation is wanted."""
        await emit({"type": "quick_start"})
        # Bounded, so the investigation that may follow still fits in the request deadline.
        quick = await asyncio.wait_for(
            self.leaf.diagnose(
                image_b64=photos[0][2] if photos else None,
                notes=notes,
                region=region,
                garden=garden,
            ),
            self.leaf.settings.deadline_s,
        )
        if quick.verdict == "confident":
            why = f"Quick was confident: {quick.verdict_reason}"
            await emit({"type": "auto", "investigate": False, "reason": why})
            return self._from_quick(quick, AutoDecision(investigated=False, reason=why))
        why = f"Quick was unsure: {quick.verdict_reason}"
        refusal = may_investigate()
        if refusal:
            why = f"{why} {refusal}"
            await emit({"type": "auto", "investigate": False, "reason": why})
            return self._from_quick(
                quick, AutoDecision(investigated=False, reason=why), notice=refusal
            )
        await emit({"type": "auto", "investigate": True, "reason": why})

        case = Case(
            notes=notes,
            region=region,
            garden=garden or Garden(),
            lat=None if lat is None else round(lat, 1),  # never keep a precise location
            lon=None if lon is None else round(lon, 1),
            photos=[
                Photo(id=f"P{i + 1}", label=label, full_b64=full, view_b64=view)
                for i, (label, full, view) in enumerate(photos)
            ],
        )
        await emit({"type": "case", "case_id": case.id})
        deadline = self.investigator.clock() + self.leaf.settings.deep_wall_s
        for call in quick.trace:
            case.add_trace(call.model_copy(update={"note": f"quick {call.note}".strip()}))
        if case.photos and self.leaf.vision_model:  # Quick already looked at the first photo
            case.photos[0].observation = quick.observation
            await emit(
                {
                    "type": "first_look",
                    "photo_id": "P1",
                    "observation": quick.observation.model_dump(),
                }
            )
        await self._first_looks(case, emit, deadline)  # only the photos Quick did not see
        case.crop = infer_crop(
            notes, *(p.observation.plant_guess for p in case.photos if p.observation)
        )
        cands = quick.diagnosis.candidates
        case.differential = normalise_differential(
            # Quick's scores before any regional prior: Deep decides its own verdict
            [
                {"name": c.name, "probability": round(c.confidence - c.prior_points, 3)}
                for c in cands
            ],
            set(),
        )
        case.first_differential = [h.model_copy() for h in case.differential]
        if case.differential:
            await emit(
                {
                    "type": "differential",
                    "first": True,
                    "hypotheses": [h.model_dump() for h in case.differential],
                }
            )
        for src in quick.sources:
            case.add_source(
                src.url, sanitize(src.title, 200), sanitize(src.snippet, 400), bool(_trust(src.url))
            )
        case.seed_note = json.dumps(
            {
                "why_unsure": quick.verdict_reason,
                "candidates": [
                    {
                        "name": c.name,
                        "evidence_for": c.evidence_for[:4],
                        "evidence_against": c.evidence_against[:4],
                    }
                    for c in cands[:4]
                ],
                "reasoning": quick.diagnosis.reasoning_summary[:800],
            }
        )
        case.auto_reason = why
        case.quick_result = quick.model_dump(mode="json")
        return await self._investigate(
            case, emit, lambda: self.investigator.run(case, emit, deadline), quick=quick
        )

    def _from_quick(
        self, quick: DiagnoseResponse, auto: AutoDecision, notice: str = ""
    ) -> DeepResponse:
        return DeepResponse(
            **quick.model_dump(exclude={"notice"}),
            mode="quick",
            notice=_join(notice, quick.notice),
            cost_usd=round(sum(t.cost_usd or 0 for t in quick.trace), 5),
            auto=auto,
        )

    # ------------------------------------------------------------------ steps

    async def _seed_differential(self, case: Case, emit: Emit, deadline: float) -> None:
        """The first differential, from the structured diagnosis step Quick uses. The
        investigator then tests it; a failure here only means it starts from nothing."""
        s = self.leaf.settings
        looks = [p.observation.model_dump() for p in case.photos if p.observation]
        context = (
            f"<observations>{sanitize(json.dumps(looks))}</observations>\n"
            f"<gardener_notes>{sanitize(case.notes) or 'none'}</gardener_notes>\n"
            f"<region>{sanitize(case.region) or 'unknown'}</region>\n"
            "<follow_up_answers>none</follow_up_answers>"
        )
        trace: list[ModelCall] = []
        budget = max(5.0, min(SEED_TIMEOUT_S, deadline - self.investigator.clock() - 40))
        try:
            diagnosis = await asyncio.wait_for(
                complete_json(
                    self.leaf.llm,
                    step="first_differential",
                    models=s.models("first_differential"),
                    messages=[
                        {"role": "system", "content": DIAGNOSE_SYSTEM},
                        {"role": "user", "content": context},
                    ],
                    schema=Diagnosis,
                    trace=trace,
                    thinking=True,
                    max_tokens=s.budget("first_differential"),
                    repair_model=s.repair_model,
                ),
                budget,
            )
        except (ModelError, TimeoutError) as exc:
            log.warning("first differential unavailable for %s: %s", case.id, exc)
            return
        finally:
            for call in trace:
                case.add_trace(call)
        case.differential = normalise_differential(
            [{"name": c.name, "probability": c.confidence} for c in diagnosis.candidates], set()
        )
        case.first_differential = [h.model_copy() for h in case.differential]
        if case.differential:
            await emit(
                {
                    "type": "differential",
                    "first": True,
                    "hypotheses": [h.model_dump() for h in case.differential],
                }
            )

    async def _first_looks(self, case: Case, emit: Emit, deadline: float) -> None:
        vision = self.leaf.vision_model
        todo = [p for p in case.photos if p.observation is None]
        if not todo or not vision:
            return
        traces: list[list[ModelCall]] = [[] for _ in todo]
        looks = [
            describe(
                self.leaf.llm,
                vision,
                p.view_b64,
                "image/jpeg",
                case.notes,
                t,
                self.leaf.settings.repair_model,
                checklist=self.leaf.settings.checklist,
            )
            for p, t in zip(todo, traces, strict=True)
        ]
        # Never let the first look eat the investigation's time.
        budget = max(5.0, min(FIRST_LOOK_TIMEOUT_S, deadline - self.investigator.clock() - 30))
        try:
            observations = await asyncio.wait_for(asyncio.gather(*looks), budget)
        except TimeoutError:
            log.warning("first looks timed out for case %s", case.id)
            observations = [
                Observation(
                    image_quality="poor",
                    quality_notes="The photo could not be analysed in time.",
                )
                for _ in todo
            ]
        for photo, obs, trace in zip(todo, observations, traces, strict=True):
            photo.observation = obs
            for call in trace:
                case.add_trace(call)
            await emit(
                {"type": "first_look", "photo_id": photo.id, "observation": obs.model_dump()}
            )

    async def _investigate(
        self,
        case: Case,
        emit: Emit,
        work: Callable[[], Awaitable[AgentResult]],
        quick: DiagnoseResponse | None = None,
    ) -> DeepResponse:
        try:
            result = await work()
        except asyncio.CancelledError:
            # The request deadline cancelled us: never leave the case stuck as "running".
            case.status = "failed"
            raise
        except Exception as exc:  # noqa: BLE001 - any agent failure, even a bug, degrades to Quick
            if isinstance(exc, (AgentError, ModelError, TimeoutError)):
                log.warning("deep case %s fell back to Quick: %s", case.id, exc)
            else:
                log.exception("deep case %s crashed; falling back to Quick", case.id)
            case.status = "failed"
            await self.store.put(case)
            await emit({"type": "fallback", "reason": "The investigation could not finish."})
            if quick is not None:  # Auto mode already has a Quick diagnosis: never redo it
                return DeepResponse(
                    **{**quick.model_dump(exclude={"notice"}), "case_id": case.id},
                    mode="quick",
                    status="fallback",
                    notice=_join(
                        "Leaf could not finish its investigation, so this is the Quick diagnosis.",
                        quick.notice,
                    ),
                    steps=case.steps,
                    cost_usd=round(case.cost_usd, 5),
                    tavily_credits=case.tavily_credits,
                    auto=AutoDecision(investigated=True, reason=case.auto_reason),
                )
            return await self._quick_fallback(case)
        await self.store.put(case)
        if result.kind == "ask":
            return self._response(case, status="needs_answers", limit=result.limit)
        return await self._finish_case(case, emit, result.limit)

    async def _finish_case(self, case: Case, emit: Emit, limit: str) -> DeepResponse:
        """Second opinion, confidence, plan from passages, claim check, escalation, handoff."""
        s = self.leaf.settings
        trace: list[ModelCall] = []
        case.investigator_differential = [h.model_copy() for h in case.differential]
        case.differential = blend(case.first_differential, case.differential, s.deep_blend_first)

        async def plan_and_check() -> tuple[CarePlan, list[ClaimFlag], bool]:
            # Reading sources, writing the plan and checking it do not need the second
            # opinion (escalation is applied in code afterwards), so they run alongside it.
            try:
                await asyncio.wait_for(ensure_passages(case, self.leaf.searcher), READ_TIMEOUT_S)
            except TimeoutError:
                log.warning("reading sources timed out for %s", case.id)
            try:
                plan = await asyncio.wait_for(
                    write_plan(self.leaf.llm, s, case, case.region, trace), PLAN_TIMEOUT_S
                )
            except (ModelError, TimeoutError):
                failed = CarePlan(
                    summary="Leaf could not write a care plan right now. Your diagnosis is above."
                )
                return failed, [], True
            flags = code_checks(plan, case)
            try:
                flags += await asyncio.wait_for(
                    judge(self.leaf.llm, s, plan, case, trace), CLAIM_TIMEOUT_S
                )
            except TimeoutError:
                log.warning("claim judge timed out for %s; code checks stand", case.id)
            return plan, flags, False

        critic_result, planned = await asyncio.gather(
            asyncio.wait_for(second_opinion(self.leaf.llm, s, case, trace), CRITIC_TIMEOUT_S),
            plan_and_check(),
            return_exceptions=True,
        )
        if isinstance(planned, BaseException):  # never expected; degrade rather than fail
            log.exception("plan step crashed for %s", case.id, exc_info=planned)
            planned = (CarePlan(summary="Leaf could not write a care plan right now."), [], True)
        plan, flags, plan_failed = planned
        critic = None if isinstance(critic_result, BaseException) else critic_result
        if isinstance(critic_result, BaseException):
            log.warning("second opinion failed for %s: %r", case.id, critic_result)
        add_alternative(case, critic)
        confidence = combine(case, critic)
        await emit(
            {
                "type": "critic",
                "opinion": critic.model_dump() if critic else None,
                "confidence": confidence.model_dump(),
            }
        )
        for call in trace:
            case.add_trace(call)
        sources_text = " ".join(" ".join(s.passages) for s in case.sources)
        flags += check_summary(plan, sources_text)
        apply_garden(plan, case.garden)  # before the verdict, so an emptied plan is not Confident
        decision = self._with_regional(
            case,
            confidence,
            critic,
            deep_decision(case, confidence, critic, self.leaf.vision_model),
        )
        if plan_failed:  # as in Quick: no plan at all, so a lab or extension office decides
            decision = Decision(
                "lab",
                "Leaf could not write a care plan right now; your local extension office "
                "or plant diagnostic lab can advise.",
            )
        elif not plan.steps:
            # No step survived the claim check: the diagnosis stands, but Leaf has no cited
            # plan to offer, so it is never Confident and says where to get one.
            if decision.verdict == "confident":
                decision = Decision("likely", decision.reason)
            plan.sample_if = plan.sample_if or (
                "Leaf found no source that supports specific steps, so ask your local "
                "extension office what to do."
            )
        apply_verdict(plan, decision)
        handoff = (
            build_handoff(case, plan.escalate_reason, critic.model_dump() if critic else None)
            if plan.escalate
            else None
        )
        case.status = "done"
        await self.store.put(case)
        if not case.sources and search_paused(self.leaf.searcher):
            plan.sample_if = plan.sample_if or PAUSED_NOTICE
        response = self._response(
            case,
            plan=plan,
            limit=limit,
            confidence=confidence,
            critic=critic,
            claim_flags=flags,
            handoff=handoff,
            decision=decision,
        )
        await emit({"type": "done", "case_id": case.id})
        return response

    # ------------------------------------------------------------------ conversions

    def _sources(self, case: Case) -> list[Source]:
        return [
            Source(
                id=_source_int(src.id),
                title=src.title,
                url=src.url,
                snippet=(" … ".join(src.passages) or src.snippet)[:1200],
            )
            for src in case.sources
        ]

    def _diagnosis(self, case: Case) -> Diagnosis:
        by_id = {e.id: e.summary for e in case.evidence}
        return Diagnosis(
            candidates=[
                Candidate(
                    name=h.name,
                    confidence=h.probability,
                    evidence_for=[by_id[e] for e in h.evidence_for if e in by_id][:4],
                    evidence_against=[by_id[e] for e in h.evidence_against if e in by_id][:4],
                )
                for h in case.differential
            ],
            follow_up_questions=case.questions[:3] if case.status == "needs_answers" else [],
            reasoning_summary=case.summary,
        )

    def _with_regional(
        self,
        case: Case,
        confidence: Confidence,
        critic: CriticOpinion | None,
        raw: Decision,
    ) -> Decision:
        """Leaf Radar's capped prior on the final differential (at most 5 points a cause). The
        combined confidence stays the raw one, and the verdict is the more cautious of the raw
        and boosted decisions, so the prior can send a case to a lab but never make it
        Confident."""
        ctx = REGIONAL.get()
        guess = next(
            (
                p.observation.plant_guess
                for p in case.photos
                if p.observation and p.observation.plant_guess.lower() != "unknown"
            ),
            "",
        )
        for h in case.differential:
            h.prior_points = 0.0
        if ctx is None:
            return raw
        case.regional = apply_prior_to_hypotheses(case.differential, guess or case.notes, ctx)
        if case.regional is None:
            return raw
        case.differential.sort(key=lambda h: h.probability, reverse=True)
        return more_cautious(raw, deep_decision(case, confidence, critic, self.leaf.vision_model))

    def _response(
        self,
        case: Case,
        *,
        status: Literal["done", "needs_answers"] = "done",
        plan: CarePlan | None = None,
        limit: str = "",
        confidence: Confidence | None = None,
        critic: CriticOpinion | None = None,
        claim_flags: list[ClaimFlag] | None = None,
        handoff: LabHandoff | None = None,
        decision: Decision | None = None,
    ) -> DeepResponse:
        first = next((p.observation for p in case.photos if p.observation), None)
        return DeepResponse(
            observation=first or Observation(quality_notes="No photo; using your notes only."),
            diagnosis=self._diagnosis(case),
            plan=plan or CarePlan(summary="Answer the questions so Leaf can finish this case."),
            sources=self._sources(case),
            trace=case.trace,
            notice=PAUSED_NOTICE
            if status == "done" and not case.sources and search_paused(self.leaf.searcher)
            else "",
            low_confidence=decision is None or decision.verdict != "confident",
            verdict=decision.verdict if decision else "likely",
            verdict_reason=decision.reason if decision else "",
            mode="deep",
            case_id=case.id,
            status=status,
            garden=case.garden,
            questions=case.questions if status == "needs_answers" else [],
            regional=case.regional,
            differential=case.differential,
            first_differential=case.first_differential,
            investigator_differential=case.investigator_differential,
            evidence=case.evidence,
            steps=case.steps,
            photos=[
                PhotoInfo(id=p.id, label=p.label, observation=p.observation) for p in case.photos
            ],
            summary=case.summary,
            limit=limit,
            cost_usd=round(case.cost_usd, 5),
            tavily_credits=case.tavily_credits,
            confidence=confidence,
            critic=critic,
            claim_flags=claim_flags or [],
            handoff=handoff,
            auto=AutoDecision(investigated=True, reason=case.auto_reason)
            if case.auto_reason
            else None,
        )

    async def _quick_fallback(self, case: Case, notice: str = "") -> DeepResponse:
        try:
            quick = await asyncio.wait_for(
                self.leaf.diagnose(
                    image_b64=case.photos[0].view_b64 if case.photos else None,
                    notes=case.notes,
                    region=case.region,
                    garden=case.garden,
                ),
                QUICK_FALLBACK_TIMEOUT_S,
            )
        except (TimeoutError, ModelError) as exc:  # even the fallback failed: say so, no 504
            log.warning("quick fallback failed for %s: %s", case.id, exc)
            return DeepResponse(
                observation=Observation(quality_notes="Leaf could not analyse this case."),
                diagnosis=Diagnosis(),
                plan=CarePlan(
                    summary="Leaf could not finish this case. Please try again in a minute.",
                    escalate=True,
                    escalate_reason="Leaf's models are busy; your local extension office can help.",
                ),
                sources=[],
                trace=case.trace,
                low_confidence=True,
                verdict="lab",
                verdict_reason="Leaf's models are busy; your local extension office can help.",
                mode="quick",
                case_id=case.id,
                status="fallback",
                notice="Leaf's models are busy right now, so this case could not be finished.",
                steps=case.steps,
                cost_usd=round(case.cost_usd, 5),
                tavily_credits=case.tavily_credits,
            )
        return DeepResponse(
            **{**quick.model_dump(exclude={"notice"}), "case_id": case.id},
            mode="quick",
            status="fallback",
            notice=_join(
                notice or "Deep mode could not finish this case, so this is a Quick diagnosis.",
                quick.notice,
            ),
            steps=case.steps,
            cost_usd=round(case.cost_usd + sum(t.cost_usd or 0 for t in quick.trace), 5),
            tavily_credits=case.tavily_credits,
        )

    async def quick_only(
        self,
        *,
        photos: list[tuple[str, str, str]],
        notes: str,
        region: str,
        notice: str,
        garden: Garden | None = None,
    ) -> DeepResponse:
        """Used when Deep is unavailable (daily cap): a Quick diagnosis that says why."""
        case = Case(
            notes=notes,
            region=region,
            garden=garden or Garden(),
            photos=[Photo(id="P1", label=lbl, full_b64=f, view_b64=v) for lbl, f, v in photos[:1]],
        )
        return await self._quick_fallback(case, notice=notice)
