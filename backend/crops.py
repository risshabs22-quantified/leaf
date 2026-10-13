"""Recognise the crop from free text (the gardener's notes, the vision model's plant guess).

Canonical names matter because the published disease-risk rules are crop-specific.
"""

from __future__ import annotations

import re

CROP_ALIASES: dict[str, list[str]] = {
    "tomato": ["tomato", "solanum lycopersicum", "lycopersicon"],
    "potato": ["potato", "solanum tuberosum"],
    "pepper": ["pepper", "capsicum", "chili", "chilli"],
    "cucurbit": ["squash", "zucchini", "courgette", "cucumber", "pumpkin", "melon", "cucurbit"],
    "rose": ["rose", "roses", "rosa"],
    "apple": ["apple", "malus"],
    "grape": ["grape", "grapevine", "vitis"],
    "bean": ["bean", "beans", "phaseolus"],
    "maize": ["maize", "corn", "zea mays"],
}
_PATTERNS = [
    (crop, re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE))
    for crop, words in CROP_ALIASES.items()
]


def infer_crop(*texts: str | None) -> str | None:
    """First crop named in the texts, in order (the gardener's own words win)."""
    for text in texts:
        if not text:
            continue
        hits = [(m.start(), crop) for crop, pat in _PATTERNS if (m := pat.search(text))]
        if hits:
            return min(hits)[1]
    return None
