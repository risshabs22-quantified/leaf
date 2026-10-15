"""Leaf Radar endpoints: what the weekly runs found, and what they did."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from backend.data.driver import DataError
from backend.platform_api import Key, _ip, _open
from backend.radar import regions
from backend.radar.collect import WINDOW_DAYS
from backend.radar.prior import region_of_cell

router = APIRouter(tags=["Radar"])

HOW = (
    "Once a week, for each US state that has a garden, Leaf searches that state's university "
    "extension sites and reads the pages. It keeps a report only when the page has its own "
    f"recent date (within {WINDOW_DAYS} days), says a specific pest or disease was seen, and "
    "contains the quoted sentence word for word. Nothing is seeded or written from memory."
)


def _run_view(run: Any) -> dict[str, Any]:
    return run.model_dump() | {"checked_at": run.created_at}


async def region_view(request: Request, region: str, public: bool = False) -> dict[str, Any]:
    """`public` hides run details when there is nothing to show: a run on its own would say
    that someone in that state has a garden."""
    repo = request.app.state.data.repo
    since = (datetime.now(UTC).date() - timedelta(days=WINDOW_DAYS)).isoformat()
    try:
        reports = await repo.list_radar_reports(region, since, 100)
        runs = await repo.radar_runs(region, 4)
    except DataError as exc:
        raise HTTPException(503, "Leaf's storage is unavailable. Please try again.") from exc
    if public and not reports:
        runs = []
    return {
        "region": region,
        "name": regions.name(region),
        "covered": regions.covered(region),
        "verified_sources": region not in regions.UNVERIFIED,
        "window_days": WINDOW_DAYS,
        "reports": [r.model_dump() for r in reports],
        "last_run": _run_view(runs[0]) if runs else None,
        "runs": [_run_view(r) for r in runs],
        "how": HOW,
    }


@router.get("/api/radar/regions")
async def radar_regions(request: Request) -> dict[str, Any]:
    """The states Radar has run for, with what each run found (no garden data)."""
    request.app.state.radar_limiter.check(_ip(request))
    runs = await request.app.state.data.repo.radar_runs(None, 60)
    # Only states with something to show: a run alone would say that a state has a garden.
    return {
        "regions": [
            {"region": r.region, "name": regions.name(r.region), **_run_view(r)}
            for r in runs
            if r.reports
        ],
        "covered": sorted(c for c in regions.NAMES if regions.covered(c)),
        "unverified_sources": regions.UNVERIFIED,
        "how": HOW,
    }


@router.get("/api/radar")
async def radar_for_region(
    request: Request, region: Annotated[str, Query(pattern=r"^[A-Za-z]{2}$")]
) -> dict[str, Any]:
    """One state's current reports, each with the page's own sentence and link."""
    request.app.state.radar_limiter.check(_ip(request))
    code = region.upper()
    if code not in regions.NAMES:
        raise HTTPException(404, "Radar covers US states only.")
    return await region_view(request, code, public=True)


@router.get("/api/gardens/{garden_id}/radar")
async def radar_for_garden(request: Request, garden_id: str, key: Key = None) -> dict[str, Any]:
    """Radar for the garden's own state (the garden key opens it)."""
    garden = await _open(request, garden_id, key)
    region = await region_of_cell(request.app.state.data.repo, garden.cell)
    if region is None:
        return {
            "region": None,
            "name": "",
            "covered": False,
            "reports": [],
            "last_run": None,
            "runs": [],
            "how": HOW,
            "note": "Radar covers US gardens with a place set. This one has none, or is outside "
            "the states Radar covers.",
        }
    return await region_view(request, region)
