"""The front door: route each free-text request to an agent and screen it, in one model call.

Live requests use the `front_door` route (Lightning first: it is fast); background screening
uses the same steps under its own route (`checkin_screen`, later `radar_screen`), Nano first.

Deterministic first:
1. Code screen: obvious prompt injection and clearly harmful requests are stopped here.
2. A request whose intent the UI already knows skips the model entirely.
3. A cached decision for the same text (24 h, shared across users) skips it too.
4. The route's model decides (one call returns intent and safety together).
5. If the model is unavailable, or the day's budget is in brownout, keyword rules decide.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

from backend.cache import SharedCache
from backend.config import ULTRA, Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import FRONT_DOOR_SYSTEM, sanitize
from backend.schemas import FrontDoorDecision, FrontDoorOut, Intent, ModelCall

log = logging.getLogger("leaf.router")

CACHE_TTL_S = 24 * 3600
MAX_CHARS = 2000

# Which agent serves each intent (agents arrive with their milestones; docs/LEAF_PLATFORM.md).
AGENTS: dict[str, str] = {
    "doctor": "doctor",
    "care": "care",
    "planner": "planner",
    "research": "research",
    "concierge": "concierge",
}

# Fixed replies for screened messages: no model sees the text again.
REFUSALS = {
    "prompt_injection": (
        "I can only help with plants and gardening, and I can't change how I work. "
        "Tell me what's going on with your plants and I'll take a look."
    ),
    "harmful": (
        "I can't help with that. If you have a question about growing or protecting plants, "
        "I'm happy to help."
    ),
}

# Code blocks only unmistakable cases; anything subtler is Nano's call. The patterns are
# deliberately narrow: "ignore the previous watering instructions on the tag" or "can foxglove
# kill a child?" are gardening questions and must reach the model.
_INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(of\s+)?(the\s+|your\s+|these\s+)?"
    r"(previous|prior|above|earlier|system)\s+(instructions?|rules|prompts?|messages)\b"
    r"|\b(ignore|disregard|forget|override)\s+(all\s+)?your\s+(instructions?|rules|prompts?|"
    r"guidelines)\b"
    r"|\b(reveal|print|show|repeat|leak)\b.{0,30}\b(system|hidden|developer)\s+"
    r"(prompt|instructions?|message)\b"
    # Any request about the assistant's own system prompt (both models let "translate your
    # system prompt into French" through in the first eval run).
    r"|\b(your|leaf'?s)\s+(system|hidden|developer)\s+(prompt|instructions?)\b"
    r"|\byou\s+are\s+now\s+(a|an|in)\b"
    r"|\b(jailbreak|developer\s+mode|DAN\s+mode)\b",
    re.IGNORECASE | re.DOTALL,
)
# Toxicity questions ("will this slug bait hurt my baby?") are safety questions, so harm to
# people is left to the model; code stops only weapon-making.
_HARMFUL = re.compile(
    r"\b(make|build|mix)\b.{0,40}\b(bomb|explosive)s?\b"
    r"|\bammonium\s+nitrate\b.{0,40}\b(bomb|explosive|detonat)",
    re.IGNORECASE | re.DOTALL,
)

# Keyword rules for when no model is used. Order breaks ties.
_KEYWORDS: list[tuple[Intent, re.Pattern[str]]] = [
    (
        "doctor",
        re.compile(
            r"\b(spots?|spotted|yellow(ing)?|brown(ing)?|wilt(ing|ed)?|mou?ld|mildew|blight|rot|"
            r"rotting|pests?|bugs?|aphids?|mites?|beetles?|caterpillars?|holes?|curl(ing)?|"
            r"dying|sick|disease[ds]?|fungus|fungal|diagnos\w*|what'?s wrong)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "planner",
        re.compile(
            r"\b(plan|planning|season|schedule|calendar|sow(ing)?|seeds?|transplant(ing)?|"
            r"rotation|next (month|year|spring)|when (should|do|can) i (plant|sow|start))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "care",
        re.compile(
            r"\b(water(ing)?|fertili[sz]\w*|feed(ing)?|prun\w*|harvest\w*|frost|freez\w*|"
            r"heat ?wave|repot\w*|mulch\w*|cover|today|tonight|this week)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "research",
        re.compile(
            r"\b(what is|what are|why do|why does|how does|compare|varieties|variety|cultivars?|"
            r"best (type|kind|variety)|studies|research|evidence)\b",
            re.IGNORECASE,
        ),
    ),
]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")
    return re.sub(r"\s+", " ", text).strip()[:MAX_CHARS]


def code_screen(text: str) -> FrontDoorOut | None:
    """A verdict when code alone can decide the message is unsafe, else None."""
    if _INJECTION.search(text):
        return FrontDoorOut(safe=False, risk="prompt_injection", confidence=1.0)
    if _HARMFUL.search(text):
        return FrontDoorOut(safe=False, risk="harmful", confidence=1.0)
    return None


def keyword_route(text: str) -> FrontDoorOut:
    scores = {intent: len(rx.findall(text)) for intent, rx in _KEYWORDS}
    best = max(scores.values())
    if best == 0:
        return FrontDoorOut(intent="concierge", confidence=0.3)
    intent = next(i for i, _ in _KEYWORDS if scores[i] == best)
    matched = sum(1 for v in scores.values() if v)
    return FrontDoorOut(intent=intent, multi_step=matched >= 3, confidence=0.5)


def _decision(
    out: FrontDoorOut, source: str, settings: Settings, trace: list[ModelCall]
) -> FrontDoorDecision:
    return FrontDoorDecision(
        **out.model_dump(),
        agent=AGENTS[out.intent],
        orchestrator=settings.role_model(ULTRA) if out.multi_step and out.safe else "",
        source=source,
        trace=trace,
    )


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.lower().encode()).hexdigest()[:32]


async def front_door(
    llm: LLM,
    settings: Settings,
    text: str,
    cache: SharedCache | None = None,
    intent: Intent | None = None,
    use_model: bool = True,
    task: str = "front_door",
) -> FrontDoorDecision:
    """Route and screen one message under `task` (a ROUTES key). Never raises for a model
    failure: rules take over."""
    clean = normalize(text)
    trace: list[ModelCall] = []
    blocked = code_screen(clean)
    if blocked is not None:
        return _decision(blocked, "rules", settings, trace)
    if intent is not None:
        return _decision(FrontDoorOut(intent=intent, confidence=1.0), "explicit", settings, trace)
    if not clean:
        return _decision(FrontDoorOut(confidence=0.0), "rules", settings, trace)

    key = _cache_key(clean)
    if cache is not None:
        hit = await cache.get_json(task, key)
        if hit is not None:
            try:
                return _decision(FrontDoorOut.model_validate(hit), "cache", settings, trace)
            except ValueError:
                pass
    if use_model:
        try:
            out = await complete_json(
                llm,
                step=task,
                models=settings.models(task),
                messages=[
                    {"role": "system", "content": FRONT_DOOR_SYSTEM},
                    {"role": "user", "content": f"<message>{sanitize(clean)}</message>"},
                ],
                schema=FrontDoorOut,
                trace=trace,
                thinking=False,
                max_tokens=settings.budget(task),
                temperature=0.2,
            )
        except ModelError as exc:  # includes SpendCapReached: brownout or rules-only
            log.info("front door falls back to rules: %s", exc)
        else:
            if out.risk == "prompt_injection" or out.risk == "harmful":
                out.safe = False
            if cache is not None:
                await cache.put_json(task, key, out.model_dump(), CACHE_TTL_S)
            return _decision(out, "model", settings, trace)
    return _decision(keyword_route(clean), "rules", settings, trace)
