"""The plant knowledge graph: species profiles built once from cited web sources and shared,
and photo identification checked against sources.

Facts are extracted by Lightning, their quotes are checked against the passage in code, and
Super verifies each one before it is shown. Fields no source supports are marked unsourced;
nothing is filled in from a model's memory.
"""

from __future__ import annotations

import re
import unicodedata

_CULTIVAR = re.compile(r"['‘’\"].*?['‘’\"]")
_HYBRID = re.compile(r"\s[x×]\s|×")


def species_key(name: str, binomial: bool = False) -> str:
    """A stable key for a plant. With `binomial` (the name is known to be scientific: from the
    vision model, a candidate, or a plant's species field) cultivars, varieties and hybrid
    marks are dropped and the key is "genus species". Otherwise the whole normalised name is
    kept, so common names such as "string of pearls" and "string of hearts" never collide."""
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = _CULTIVAR.sub(" ", text)
    text = _HYBRID.sub(" ", text)
    if binomial:
        text = re.sub(r"\b(var|subsp|ssp|f|cv)\.?\s+\S+", " ", text, flags=re.IGNORECASE)
    words = re.findall(r"[A-Za-z][A-Za-z-]*", text)
    if not words:
        return ""
    if binomial and len(words) >= 2:
        return f"{words[0].lower()} {words[1].lower()}"
    return " ".join(w.lower() for w in words)[:80]


def genus(key: str) -> str:
    return key.split(" ")[0] if key else ""
