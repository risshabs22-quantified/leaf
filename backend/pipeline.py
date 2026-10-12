"""Leaf's agent pipeline: a prompt chain with routing and a parallel search step.

    photo --vision model--> Observation
          --Nemotron Ultra (reasoning on)--> Diagnosis (+ follow-ups, search queries)
          --Tavily x N in parallel--> Sources
          --Nemotron Lightning (reasoning off)--> CarePlan
Safety rules that matter are enforced in code, not left to the model.
"""

from __future__ import annotations

import json

from backend.config import Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import CHAT_SYSTEM, DIAGNOSE_SYSTEM, PLAN_SYSTEM
from backend.schemas import (
    CarePlan,
    ChatRequest,
    ChatResponse,
    DiagnoseResponse,
    Diagnosis,
    ModelCall,
    Observation,
)
from backend.search import Searcher, search_all
from backend.vision import describe

LOW_CONFIDENCE = 0.6


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
    ) -> DiagnoseResponse:
        trace: list[ModelCall] = []
        s = self.settings

        # 1. Perception
        if image_b64 and self.vision_model:
            obs = await describe(self.llm, self.vision_model, image_b64, mime, notes, trace)
        elif image_b64:
            obs = Observation(
                image_quality="poor",
                quality_notes="No vision model is available right now; using your notes only.",
            )
        else:
            obs = Observation(quality_notes="No photo; using your notes only.")

        # 2. Reasoning (Ultra, then Super, then Lightning)
        context = (
            f"<observations>{obs.model_dump_json()}</observations>\n"
            f"<gardener_notes>{notes or 'none'}</gardener_notes>\n"
            f"<region>{region or 'unknown'}</region>\n"
            f"<follow_up_answers>{answers or 'none'}</follow_up_answers>"
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
        top = diagnosis.candidates[0].confidence if diagnosis.candidates else 0.0
        # A poor photo is never a confident diagnosis, whatever the model scored.
        low = top < LOW_CONFIDENCE or obs.image_quality == "poor"

        # 3. Grounding (parallel web search)
        queries = diagnosis.search_queries or [
            f"{obs.plant_guess} {c.name}" for c in diagnosis.candidates[:2]
        ]
        if region:
            queries = [f"{q} {region}" for q in queries]
        sources = await search_all(self.searcher, queries)

        # 4. Writing (Lightning, fast)
        plan_input = (
            f"<diagnosis>{diagnosis.model_dump_json()}</diagnosis>\n"
            f"<sources>{json.dumps([src.model_dump() for src in sources])}</sources>\n"
            f"<region>{region or 'unknown'}</region>"
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
                escalate=True,
                escalate_reason="Plan generation failed; check with your local extension office.",
            )

        # Poka-yoke: uncertainty always escalates, whatever the model said.
        valid_ids = {src.id for src in sources}
        for step in plan.steps:
            step.source_ids = [i for i in step.source_ids if i in valid_ids]
        if low and not plan.escalate:
            plan.escalate = True
            plan.escalate_reason = (
                "Leaf isn't confident from this photo. Answer the follow-up questions or "
                "take a sample to your local extension office."
            )

        return DiagnoseResponse(
            observation=obs,
            diagnosis=diagnosis,
            plan=plan,
            sources=sources,
            trace=trace,
            low_confidence=low,
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        ctx = req.context
        context = {
            "diagnosis": ctx.diagnosis.model_dump(),
            "plan": ctx.plan.model_dump(),
            "sources": [{"id": src.id, "title": src.title, "url": src.url} for src in ctx.sources],
        }
        messages = [
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": f"<context>{json.dumps(context)}</context>"},
            {"role": "assistant", "content": "Got it. What would you like to know?"},
            *[t.model_dump() for t in req.history[-10:]],
            {"role": "user", "content": req.message},
        ]
        trace: list[ModelCall] = []
        for i, model in enumerate([self.settings.fast_model, self.settings.fallback_model]):
            try:
                res = await self.llm.complete(
                    model=model, messages=messages, thinking=False, max_tokens=800
                )
                trace.append(res.to_trace("chat", "fallback" if i else ""))
                return ChatResponse(reply=res.text, trace=trace)
            except ModelError as exc:
                trace.append(ModelCall(step="chat", model=model, latency_ms=0, note=str(exc)))
        raise ModelError("chat: all models failed")
