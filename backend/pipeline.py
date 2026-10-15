"""Leaf's agent pipeline: a prompt chain with routing and a parallel search step.

    photo --vision model--> Observation
          --Nemotron Ultra (reasoning on)--> Diagnosis (+ follow-ups, search queries)
          --Tavily x N in parallel--> Sources
          --Nemotron Lightning (reasoning off)--> CarePlan
Safety rules that matter are enforced in code, not left to the model: the verdict
(confident, likely, or send to a lab) comes from backend/verdict.py.
"""

from __future__ import annotations

import json
import logging
import re

from backend.actives import apply_garden, clear_code_fields, garden_prompt
from backend.config import Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import CHAT_SYSTEM, DIAGNOSE_SYSTEM, PLAN_SYSTEM, sanitize
from backend.schemas import (
    CarePlan,
    ChatRequest,
    ChatResponse,
    DiagnoseResponse,
    Diagnosis,
    Garden,
    ModelCall,
    Observation,
)
from backend.search import PAUSED_NOTICE, Searcher, search_all, search_paused
from backend.verdict import QUICK, Decision, decide
from backend.vision import describe

log = logging.getLogger("leaf.pipeline")

AMBIGUITY_MARGIN = 0.15  # top two causes closer than this: Leaf cannot tell them apart


def apply_verdict(plan: CarePlan, decision: Decision) -> None:
    """The verdict, not the model, decides whether this goes to a lab."""
    plan.escalate = decision.verdict == "lab"
    plan.escalate_reason = decision.reason if plan.escalate else ""


class Leaf:
    def __init__(self, llm: LLM, searcher: Searcher, settings: Settings, vision_model: str | None):
        self.llm = llm
        self.searcher = searcher
        self.settings = settings
        self.vision_model = vision_model

    async def diagnose(
        self,
        *,
        image_b64: str | None,
        mime: str = "image/jpeg",
        notes: str = "",
        region: str = "",
        answers: str = "",
        garden: Garden | None = None,
    ) -> DiagnoseResponse:
        garden = garden or Garden()
        trace: list[ModelCall] = []
        s = self.settings

        # 1. Perception
        if image_b64 and self.vision_model:
            obs = await describe(
                self.llm,
                self.vision_model,
                image_b64,
                mime,
                notes,
                trace,
                s.fast_model,
                checklist=s.checklist,
            )
        elif image_b64:
            obs = Observation(
                image_quality="poor",
                quality_notes="No vision model is available right now; using your notes only.",
            )
        else:
            obs = Observation(quality_notes="No photo; using your notes only.")

        # 2. Reasoning (Ultra, then Super, then Lightning)
        context = (
            f"<observations>{sanitize(obs.model_dump_json())}</observations>\n"
            f"<gardener_notes>{sanitize(notes) or 'none'}</gardener_notes>\n"
            f"<region>{sanitize(region) or 'unknown'}</region>\n"
            f"<follow_up_answers>{sanitize(answers) or 'none'}</follow_up_answers>"
        )
        diagnosis = await complete_json(
            self.llm,
            step="diagnose",
            models=[s.reason_model, s.fallback_model, s.fast_model],
            messages=[
                {"role": "system", "content": DIAGNOSE_SYSTEM},
                {"role": "user", "content": context},
            ],
            schema=Diagnosis,
            trace=trace,
            thinking=True,
            max_tokens=6000,
            repair_model=s.fast_model,
        )
        diagnosis.candidates.sort(key=lambda c: c.confidence, reverse=True)
        decision = decide(
            [(c.name, c.confidence) for c in diagnosis.candidates], obs.image_quality, QUICK
        )

        # 3. Grounding (parallel web search)
        queries = diagnosis.search_queries or [
            f"{obs.plant_guess} {c.name}" for c in diagnosis.candidates[:2]
        ]
        if region:
            queries = [f"{q} {sanitize(region, 100)}" for q in queries]
        sources = await search_all(self.searcher, queries)

        # 4. Writing (Lightning, fast)
        plan_input = (
            f"<diagnosis>{sanitize(diagnosis.model_dump_json())}</diagnosis>\n"
            f"<sources>{json.dumps([_source_for_prompt(src) for src in sources])}</sources>\n"
            f"<region>{sanitize(region) or 'unknown'}</region>\n"
            f"<garden>{garden_prompt(garden)}</garden>"
        )
        try:
            plan = await complete_json(
                self.llm,
                step="plan",
                models=[s.fast_model, s.fallback_model],
                messages=[
                    {"role": "system", "content": PLAN_SYSTEM},
                    {"role": "user", "content": plan_input},
                ],
                schema=CarePlan,
                trace=trace,
                thinking=False,
                max_tokens=2500,
                temperature=0.6,
                repair_model=s.fast_model,
            )
        except ModelError:
            plan = CarePlan(
                summary="Leaf could not write a care plan right now. Your diagnosis is above.",
            )
            if decision.verdict != "lab":
                decision = Decision("lab", "Leaf could not write a care plan right now.")

        valid_ids = {src.id for src in sources}
        for step in plan.steps:
            step.source_ids = [i for i in step.source_ids if i in valid_ids]
        apply_garden(clear_code_fields(plan), garden)  # before the verdict, in code
        if not plan.steps and decision.verdict == "confident":
            decision = Decision("likely", decision.reason)
            plan.sample_if = plan.sample_if or (
                "No step met your garden's preferences, so ask your local extension office "
                "what to do."
            )
        apply_verdict(plan, decision)

        return DiagnoseResponse(
            observation=obs,
            diagnosis=diagnosis,
            plan=plan,
            sources=sources,
            trace=trace,
            low_confidence=decision.verdict != "confident",
            verdict=decision.verdict,
            verdict_reason=decision.reason,
            notice=PAUSED_NOTICE if not sources and search_paused(self.searcher) else "",
            garden=garden,
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        context = _chat_context(req.context)
        messages = [
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": f"<context>{json.dumps(context)}</context>"},
            {"role": "assistant", "content": "Got it. What would you like to know?"},
            *[{"role": t.role, "content": t.content[:2000]} for t in req.history[-10:]],
            {"role": "user", "content": req.message},
        ]
        valid_ids = {src["id"] for src in context["sources"]}
        trace: list[ModelCall] = []
        for i, model in enumerate([self.settings.fast_model, self.settings.fallback_model]):
            try:
                res = await self.llm.complete(
                    model=model, messages=messages, thinking=False, max_tokens=800
                )
                trace.append(res.to_trace("chat", "fallback" if i else ""))
                return ChatResponse(reply=_drop_unknown_citations(res.text, valid_ids), trace=trace)
            except ModelError as exc:
                log.warning("chat call failed: %s", exc)  # detail stays server-side
                trace.append(ModelCall(step="chat", model=model, latency_ms=0, note="failed"))
        raise ModelError("chat: all models failed")


def _source_for_prompt(src) -> dict:
    """Web text is untrusted: keep it from closing the <sources> block."""
    return {
        "id": src.id,
        "title": sanitize(src.title, 200),
        "url": sanitize(src.url, 500),
        "snippet": sanitize(src.snippet, 600),
    }


def _chat_context(ctx: DiagnoseResponse) -> dict:
    """A bounded, sanitised view of a client-supplied diagnosis.

    The client sends this back to us, so every list and string is capped: a forged or
    oversized context can neither blow up the prompt nor smuggle in tags.
    """
    return {
        "diagnosis": {
            "summary": sanitize(ctx.diagnosis.reasoning_summary, 600),
            "candidates": [
                {
                    "name": sanitize(c.name, 120),
                    "category": c.category,
                    "confidence": c.confidence,
                    "evidence_for": [sanitize(e, 200) for e in c.evidence_for[:4]],
                    "evidence_against": [sanitize(e, 200) for e in c.evidence_against[:4]],
                }
                for c in ctx.diagnosis.candidates[:5]
            ],
        },
        "plan": {
            "summary": sanitize(ctx.plan.summary, 800),
            "steps": [
                {"action": sanitize(s.action, 300), "why": sanitize(s.why, 300)}
                for s in ctx.plan.steps[:8]
            ],
            "escalate": ctx.plan.escalate,
            "escalate_reason": sanitize(ctx.plan.escalate_reason, 300),
            "watch_for": [sanitize(w, 300) for w in ctx.plan.watch_for[:3]],
            "sample_if": sanitize(ctx.plan.sample_if, 300),
        },
        "verdict": {
            "verdict": ctx.verdict,
            "reason": sanitize(ctx.verdict_reason, 400),
        },
        "sources": [
            {"id": s.id, "title": sanitize(s.title, 200), "url": sanitize(s.url, 300)}
            for s in ctx.sources[:6]
        ],
    }


def _drop_unknown_citations(reply: str, valid_ids: set[int]) -> str:
    """Remove [n] markers that do not point at a real source."""
    return re.sub(
        r"\s?\[(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in valid_ids else "", reply
    )
