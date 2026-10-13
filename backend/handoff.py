"""Lab handoff: when Leaf escalates, the gardener gets a packet a diagnostic clinic can use.

Sampling guidance is condensed from land-grant clinic instructions (sources below). The lab link
goes to the National Plant Diagnostic Network directory, filtered by state where known.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.case import Case
from backend.conditions import _STATE_LOOKUP, US_STATES

NPDN_DIRECTORY = "https://www.npdn.org/lab_directory"
# Shown as published on npdn.org (Oct 2026): activities paused or reduced since Sept 1, 2026.
NPDN_NOTICE = (
    "The National Plant Diagnostic Network says its activities may be paused or reduced since "
    "Sept 1, 2026 while federal funding is delayed. Labs are run by their own universities; if "
    "the directory is unavailable, contact your state's land-grant extension office."
)
SAMPLING_SOURCES = [
    "https://plantclinic.cornell.edu/?p=103",
    "https://extension.unh.edu/resource/guidelines-collecting-and-submitting-plant-material-plant-diagnostic-lab",
    "https://bpp.oregonstate.edu/plant-clinic/submit-sample/how-submit-plant-samples",
]
SAMPLING_STEPS = [
    "Send the whole plant if you can: dig it up with roots rather than pulling it.",
    "Include a range of symptoms, especially early ones, plus some healthy tissue. Completely "
    "dead tissue hides the real cause.",
    "Include the edge where diseased tissue meets healthy tissue.",
    "Bag roots and soil separately so soil stays off the leaves.",
    "Do not water the plant or wet the packing. Press leaves between dry paper towels.",
    "Collect before you apply any pesticide.",
    "Pack firmly in a sturdy box and ship early in the week by the fastest method; keep the "
    "sample cool if it cannot go out the same day.",
    "Include the clinic's form: plant and variety, symptom pattern, watering, fertiliser and "
    "spray history, location, collection date, photos and your contact details.",
    "Bag rules differ between clinics (unsealed, punched or no plastic): check your clinic's "
    "instructions before packing.",
]


class LabHandoff(BaseModel):
    case_id: str
    created: str
    crop: str | None = None
    place: str = ""
    history: dict[str, Any] = Field(default_factory=dict)
    photos: list[dict[str, Any]] = Field(default_factory=list)
    weather: dict[str, Any] | None = None
    differential: list[dict[str, Any]] = Field(default_factory=list)
    second_opinion: dict[str, Any] | None = None
    reason: str = ""
    sampling: list[str] = Field(default_factory=lambda: list(SAMPLING_STEPS))
    sampling_sources: list[str] = Field(default_factory=lambda: list(SAMPLING_SOURCES))
    lab_url: str = NPDN_DIRECTORY
    lab_state: str | None = None
    lab_notice: str = NPDN_NOTICE


def state_code(region: str) -> str | None:
    """Two-letter US state from free text like "Ohio", "OH" or "Columbus, OH"."""
    parts = [p.strip().lower().rstrip(".") for p in region.split(",") if p.strip()]
    for part in reversed(parts):
        hit = _STATE_LOOKUP.get(part)
        if hit:
            name = hit[0]
            return next(code for code, (n, _) in US_STATES.items() if n == name)
    return None


def build_handoff(case: Case, reason: str, critic: dict[str, Any] | None) -> LabHandoff:
    from datetime import UTC, datetime

    code = state_code(case.region) if case.region else None
    weather = next((e.data for e in case.evidence if e.tool == "get_conditions"), None)
    by_id = {e.id: e.summary for e in case.evidence}
    answers = next(
        (
            m["content"]
            for m in reversed(case.messages)
            if m.get("role") == "tool" and m.get("name") == "ask_user"
        ),
        "",
    )
    return LabHandoff(
        case_id=case.id,
        created=datetime.fromtimestamp(case.created_at, UTC).date().isoformat(),
        crop=case.crop,
        place=(weather or {}).get("place") or case.region,
        history={"notes": case.notes, "questions": case.questions, "answers": answers[:1200]},
        photos=[
            {
                "id": p.id,
                "label": p.label,
                "seen": p.observation.symptoms[:6] if p.observation else [],
            }
            for p in case.photos
        ],
        weather={
            k: weather[k]
            for k in (
                "place",
                "from",
                "to",
                "rain_mm_total",
                "rainy_days",
                "wet_hours_total",
                "temp_c",
                "indices",
            )
            if k in weather
        }
        if weather
        else None,
        differential=[
            {
                "name": h.name,
                "probability": h.probability,
                "evidence": [by_id[e] for e in h.evidence_for if e in by_id][:4],
            }
            for h in case.differential
        ],
        second_opinion=critic,
        reason=reason,
        lab_url=f"{NPDN_DIRECTORY}?state={code}&region=" if code else NPDN_DIRECTORY,
        lab_state=code,
    )
