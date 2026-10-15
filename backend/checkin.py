"""Check-ins: compare a new photo with the last one and update the advice.

    before + now --vision (thinking off)--> spreading / stable / recovering / unclear
                 --Lightning--> what to do now, citing the report's own sources
Code, not a model, decides escalation: still spreading after a plan means a lab should look.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.actives import apply_garden, garden_prompt
from backend.claims import CHEMICAL, FOLLOW_LABEL, unsourced_amounts
from backend.config import ROUTES, Settings
from backend.journal import MAX_CHECKINS, CheckIn, JournalEntry, thumbnail
from backend.models import LLM, ModelError, complete_json
from backend.prompts import CHECKIN_PLAN_SYSTEM, CHECKIN_VISION_SYSTEM, sanitize
from backend.schemas import CarePlan, Garden, ModelCall, PlanStep, _unit

SPREADING_REASON = (
    "The problem is still spreading. A diagnostic lab can confirm the cause before you treat "
    "further; your local extension office can help."
)


class Progress(BaseModel):
    status: Literal["spreading", "stable", "recovering", "unclear"] = "unclear"
    change: str = ""
    confidence: float = Field(0.5, ge=0.0, le=1.0)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> Any:
        v = v.strip().lower() if isinstance(v, str) else v
        return v if v in ("spreading", "stable", "recovering", "unclear") else "unclear"

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _unit(v)


class _Advice(BaseModel):
    advice: str = ""
    steps: list[PlanStep] = Field(default_factory=list)


async def compare(
    llm: LLM, model: str, before_b64: str, now_b64: str, notes: str, trace: list[ModelCall]
) -> Progress:
    text = "BEFORE is the first image, NOW is the second."
    if notes:
        text += f"\n<gardener_notes>{sanitize(notes, 1000)}</gardener_notes>"
    messages = [
        {"role": "system", "content": CHECKIN_VISION_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{before_b64}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{now_b64}"}},
            ],
        },
    ]
    return await complete_json(
        llm,
        step="checkin_compare",
        models=[model],
        messages=messages,
        schema=Progress,
        trace=trace,
        thinking=False,
        max_tokens=ROUTES["checkin_compare"].max_tokens,
        temperature=0.3,
    )


async def check_in(
    llm: LLM,
    settings: Settings,
    vision_model: str | None,
    entry: JournalEntry,
    now_b64: str,
    notes: str,
    trace: list[ModelCall],
) -> CheckIn:
    if len(entry.checkins) >= MAX_CHECKINS:
        raise ValueError("This case already has the maximum number of check-ins.")
    before = entry.latest_photo()
    now_thumb = await asyncio.to_thread(thumbnail, now_b64)  # image work stays off the loop
    if before and vision_model:
        try:
            progress = await compare(llm, vision_model, before, now_thumb, notes, trace)
        except ModelError:
            progress = Progress(change="Leaf could not compare the photos this time.")
    else:
        progress = Progress(change="There is no earlier photo to compare with.")

    report = entry.report
    sources = [
        {"id": s.get("id"), "title": sanitize(s.get("title", ""), 200)}
        for s in report.get("sources", [])
    ]
    plan = report.get("plan") or {}
    top = (report.get("differential") or [{}])[0].get("name") or (
        (report.get("diagnosis") or {}).get("candidates") or [{}]
    )[0].get("name", "unknown")
    payload = {
        "diagnosis": top,
        "earlier_plan": {"summary": plan.get("summary", ""), "steps": plan.get("steps", [])},
        "change": progress.model_dump(),
        "garden": garden_prompt(Garden.model_validate(report.get("garden") or {})),
    }
    advice = _Advice(advice="Keep following your care plan and check in again in a few days.")
    try:
        advice = await complete_json(
            llm,
            step="checkin_plan",
            models=settings.models("checkin_plan"),
            messages=[
                {"role": "system", "content": CHECKIN_PLAN_SYSTEM},
                {
                    "role": "user",
                    "content": f"<case>{sanitize(json.dumps(payload, ensure_ascii=False))}</case>\n"
                    f"<sources>{sanitize(json.dumps(sources, ensure_ascii=False))}</sources>",
                },
            ],
            schema=_Advice,
            trace=trace,
            thinking=False,
            max_tokens=settings.budget("checkin_plan"),
            temperature=0.5,
        )
    except ModelError:
        pass
    valid = {s["id"] for s in sources}
    source_text = " ".join(
        f"{s.get('title', '')} {s.get('snippet', '')}" for s in report.get("sources", [])
    )
    kept = []
    for step in advice.steps:
        step.source_ids = [i for i in step.source_ids if i in valid]
        text = f"{step.action} {step.why}"
        if not step.source_ids or unsourced_amounts(text, source_text):
            continue  # every step cites a source, and states no amount its sources lack
        if CHEMICAL.search(text) and "label" not in text.lower():
            step.action = f"{step.action.rstrip('. ')}. {FOLLOW_LABEL}"
        kept.append(step)
    # The gardener's preferences hold for check-in advice too, enforced in code.
    garden = Garden.model_validate(report.get("garden") or {})
    checked = CarePlan(summary="", steps=kept)
    apply_garden(checked, garden)
    advice.steps = checked.steps
    if unsourced_amounts(advice.advice, source_text):
        advice.advice = "Follow the steps below; each one cites its source."
    spreading = progress.status == "spreading"
    return CheckIn(
        status=progress.status,
        change=sanitize(progress.change, 400),
        advice=sanitize(advice.advice, 500),
        steps=advice.steps[:4],
        removed=checked.removed,
        cautions=checked.cautions,
        escalate=spreading,
        escalate_reason=SPREADING_REASON if spreading else "",
        photo=now_thumb,
    )
