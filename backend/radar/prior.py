"""Regional prior: what extension services report nearby nudges a diagnosis, a little.

- A candidate whose name matches a current report in the garden's state (same pest or disease,
  and the report's plants include the plant being diagnosed) gains up to 5 points: 2 per
  distinct source, at most 5 (PRIOR_CAP). Nothing else changes: a prior never adds a candidate.
- The verdict is decided twice, from the scores without and with the prior, and the more
  cautious answer stands. So a prior can send a case to a lab (a serious condition looks likelier
  here) but can never be the reason for Confident.
- The reports are shown with the result (what, where, when, the page's own sentence, the link).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backend.knowledge.profile import _words
from backend.radar import regions
from backend.radar.collect import WINDOW_DAYS, singular
from backend.schemas import Candidate, RegionalNote, RegionalReport

PRIOR_CAP = 0.05
PER_SOURCE = 0.02
# One-word names too general to say which problem is meant ("blight" is early or late blight).
GENERIC = {
    "blight", "rot", "spot", "mildew", "wilt", "mold", "mould", "rust", "scale", "canker",
    "virus", "fungus", "bacteria", "insect", "pest", "disease", "worm", "beetle", "bug",
    "mite", "fly", "borer", "gall", "scab", "leaf", "damage", "infestation", "outbreak",
}  # fmt: skip


@dataclass
class RegionalContext:
    region: str
    reports: list[Any]  # RadarReport rows


# Set by the endpoint that knows where the garden is, read by the diagnosis pipeline.
REGIONAL: ContextVar[RegionalContext | None] = ContextVar("leaf_regional", default=None)


# Words that say nothing about which plant is meant.
PLANT_STOP = {
    "the", "of", "and", "or", "in", "on", "my", "a", "an", "to", "is", "are", "with", "from",
    "leaf", "leave", "plant", "garden", "tree", "unknown", "spot", "spotted", "brown", "yellow",
    "all", "new", "old", "some", "have", "has", "this", "that", "it", "its", "what", "wrong",
}  # fmt: skip


def _words_of(text: str) -> set[str]:
    return {singular(w) for w in _words(text).split()}


def _plant_words(text: str) -> set[str]:
    return {w for w in _words_of(text) if w not in PLANT_STOP and len(w) >= 3}


def matches(candidate: str, report: Any, plant: str) -> bool:
    """The report is about this candidate, on this kind of plant. `plant` is everything Leaf
    knows about it: the photo's plant guess and the gardener's own words."""
    key = report.pest_key.split()
    if not key or (len(key) == 1 and key[0] in GENERIC):
        return False
    if not set(key) <= _words_of(candidate):
        return False
    if report.hosts:  # the plant must be one the report is about; unknown means no match
        hosts = set().union(*(_plant_words(h) for h in report.hosts))
        if not hosts & _plant_words(plant):
            return False
    return True


def apply_prior(
    candidates: list[Candidate], plant: str, ctx: RegionalContext
) -> RegionalNote | None:
    """Add the capped prior to matching candidates (in place) and describe what was used."""
    used: dict[str, Any] = {}
    boosted: list[dict[str, Any]] = []
    for c in candidates:
        found = [r for r in ctx.reports if matches(c.name, r, plant)]
        if not found:
            continue
        sources = {r.source_url for r in found}
        points = round(min(PRIOR_CAP, PER_SOURCE * len(sources)), 3)
        c.prior_points = points
        c.confidence = min(1.0, round(c.confidence + points, 3))
        boosted.append({"name": c.name, "points": points})
        for r in found:
            used.setdefault(r.source_url + r.pest_key, r)
    if not boosted:
        return None
    return RegionalNote(
        region=ctx.region,
        region_name=regions.name(ctx.region),
        window_days=WINDOW_DAYS,
        reports=[
            RegionalReport(
                pest=r.pest,
                hosts=r.hosts,
                place=r.place,
                reported_on=r.reported_on,
                quote=r.quote,
                source_title=r.source_title,
                source_url=r.source_url,
            )
            for r in sorted(used.values(), key=lambda r: r.reported_on, reverse=True)[:6]
        ],
        boosted=boosted,
    )


async def load_context(repo: Any, region: str, today: date | None = None) -> RegionalContext | None:
    """The state's current reports (None when there are none or the state is not covered)."""
    if not regions.covered(region):
        return None
    since = ((today or datetime.now(UTC).date()) - timedelta(days=WINDOW_DAYS)).isoformat()
    reports = await repo.list_radar_reports(region, since, 100)
    return RegionalContext(region, reports) if reports else None


async def region_of_cell(repo: Any, cell: str | None) -> str | None:
    """A cell's state, if it holds a garden and has been looked up and is covered."""
    row = await repo.get_cell(cell) if cell else None
    region = (row or {}).get("region")
    return region if region and regions.covered(region) else None


async def activate(
    app: Any, *, cell: str | None = None, lat: float | None = None, lon: float | None = None
) -> RegionalContext | None:
    """Make the state's reports available to the diagnosis that follows in this request. Known
    from a garden's cell, or from coordinates (looked up once per cell, kept for 30 days). Any
    failure means no prior, never a failed diagnosis."""
    import asyncio

    from backend.data.repo import grid_cell

    try:
        repo = app.state.data.repo
        region = await region_of_cell(repo, cell)
        if region is None and lat is not None and lon is not None:
            point_cell = grid_cell(lat, lon)
            region = await region_of_cell(repo, point_cell)
            cache = getattr(app.state, "cache", None)
            if region is None and cache is not None:
                cached = await cache.get_json("radar_region", point_cell)
                if cached is None:
                    try:
                        found = await asyncio.wait_for(regions.region_for_point(lat, lon), 3)
                        await cache.put_json("radar_region", point_cell, {"r": found}, 30 * 86400)
                    except Exception:  # noqa: BLE001 - not answering: do not ask again for a while
                        found = regions.NOT_COVERED
                        await cache.put_json("radar_region", point_cell, {"r": found}, 600)
                    cached = {"r": found}
                region = cached["r"] if regions.covered(cached["r"]) else None
        ctx = await load_context(repo, region) if region else None
    except Exception:  # noqa: BLE001 - a prior is an extra; nothing here may fail a diagnosis
        REGIONAL.set(None)  # and never a stale one from earlier in this context
        return None
    REGIONAL.set(ctx)
    return ctx
