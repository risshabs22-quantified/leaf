"""M5 Season Plan: code rules, month chunks, Super verification, replanning, calendar export,
the page's endpoints, and Leaf Doctor wiring (diagnosis to tasks and replans; check-ins)."""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app import RateLimiter, app
from backend.calendar_file import ics_calendar
from backend.care import PlantState
from backend.care.service import CareContext
from backend.climate.crops import CROPS
from backend.config import Settings
from backend.data.driver import SqliteDriver
from backend.data.migrate import apply
from backend.data.repo import GardenIn, Plant, PlantIn, Repo
from backend.jobs import JobContext, JobMessage, JobRunner
from backend.journal import CheckIn, JournalEntry, MemoryJournal
from backend.schemas import Garden as Prefs
from backend.schemas import PlanStep
from backend.season import (
    apply_issues,
    build_inputs,
    calendar_events,
    check_items,
    diff_weeks,
    frost_day,
    item_id,
    merge_month,
    season_months,
    weather_triggers,
)
from backend.season.service import checkin_loop, gather_inputs, replan, write_plan
from tests.conftest import make_leaf
from tests.test_agent import RESP
from tests.test_climate import fake_forecast

TODAY = date(2026, 3, 2)  # a Monday
ULTRA = Settings(nebius_api_key="x").models("season_plan")[0]
SUPER = Settings(nebius_api_key="x").models("plan_verify")[0]
TMAX = [-2, 1, 7, 14, 20, 25, 28, 27, 22, 15, 7, 0]
TMIN = [-10, -8, -3, 3, 9, 14, 17, 16, 11, 5, -1, -7]
CLIM = {
    "hemisphere": "north",
    "hardiness_zone": {"zone": "5b"},
    "frost": {
        "enough_data": True,
        "last_spring_frost": {"risk_90": "04-20", "risk_50": "05-01", "risk_10": "05-15"},
        "first_autumn_frost": {"risk_10": "09-20", "risk_50": "10-01", "risk_90": "10-12"},
        "season_days": {"median": 150},
    },
    "monthly": [
        {"month": m + 1, "tmax_c": TMAX[m], "tmin_c": TMIN[m], "rain_mm": 60} for m in range(12)
    ],
}


def plant(name, setting="outdoor", pid=None, **kw) -> Plant:
    return Plant(
        id=pid or f"id-{name}",
        garden_id="g",
        common_name=name,
        setting=setting,
        created_at=0,
        updated_at=0,
        **kw,
    )


def garden(**prefs):
    from backend.data.repo import Garden

    return Garden(
        id="g",
        name="Plot",
        lat=44.9,
        lon=-93.2,
        cell="449_-932",
        timezone="UTC",
        prefs=Prefs(**prefs),
        created_at=0,
        last_seen=0,
    )


def fact(field, value, url="https://ext.example.edu/p", quote="a quote from the page"):
    return {"value": value, "quote": quote, "source": {"title": f"Page {field}", "url": url}}


def inputs(prefs=None, states=None, clim=CLIM):
    states = states or [
        PlantState(plant("tomato"), crop=CROPS["tomato"]),
        PlantState(plant("lettuce"), crop=CROPS["lettuce"]),
        PlantState(plant("monstera", "indoor")),
    ]
    return build_inputs(garden(**(prefs or {})), states, clim)


def item(week, ref, kind, action, why="", ids=()):
    return {
        "week": week,
        "plant": ref,
        "kind": kind,
        "action": action,
        "why": why,
        "source_ids": list(ids),
    }


def weeks_of(year, month):
    d = date(year, month, 1)
    d += timedelta(days=(7 - d.weekday()) % 7)
    out = []
    while d.month == month:
        out.append(d.isoformat())
        d += timedelta(weeks=1)
    return out


# ---------- inputs and months ----------


def test_frost_dates_fall_in_the_frost_year_of_the_week():
    assert frost_day("05-01", date(2026, 11, 2), southern=False) == date(2026, 5, 1)
    # South: the frost year runs July to June, so spring (Sep) and autumn (May) straddle it.
    assert frost_day("09-15", date(2026, 10, 5), southern=True) == date(2026, 9, 15)
    assert frost_day("05-01", date(2026, 10, 5), southern=True) == date(2027, 5, 1)
    assert frost_day("09-15", date(2027, 2, 1), southern=True) == date(2026, 9, 15)
    assert frost_day("02-29", date(2027, 3, 1), southern=False) == date(2027, 2, 28)
    assert frost_day(None, date(2026, 1, 1), southern=False) is None


def test_frost_class_comes_from_csu_then_the_more_tender_profile_and_defaults_to_tender():
    inp = inputs(
        states=[
            PlantState(plant("tomato"), crop=CROPS["tomato"]),
            PlantState(
                plant("pea"),
                crop=CROPS["pea"],
                profile={"facts": {"frost_tolerance": [fact("frost_tolerance", "half_hardy")]}},
            ),
            PlantState(
                plant("lettuce"),
                crop=CROPS["lettuce"],
                profile={"facts": {"frost_tolerance": [fact("frost_tolerance", "hardy")]}},
            ),
            PlantState(plant("dahlia")),
        ]
    )
    by = {p.name: p for p in inp.plants.values()}
    assert (
        by["tomato"].frost_class == "very_tender"
        and "colostate" in by["tomato"].frost_source["url"]
    )
    assert by["pea"].frost_class == "half_hardy"  # the profile is more cautious than CSU
    assert by["pea"].frost_source["url"] == "https://ext.example.edu/p"
    assert by["lettuce"].frost_class == "hardy" and "colostate" in by["lettuce"].frost_source["url"]
    assert by["dahlia"].frost_class == "tender" and by["dahlia"].assumed
    assert [p.ref for p in inp.plants.values()] == ["p1", "p2", "p3", "p4"]
    assert inp.sources[0]["url"].startswith("https://www.ncei") or inp.sources[0]["title"]
    assert (
        inp.sources[1]["url"]
        == "https://extension.colostate.edu/resource/vegetable-planting-guide/"
    )


def test_months_cover_52_weeks_and_skip_only_dormant_months():
    months = season_months(
        inputs(states=[PlantState(plant("tomato"), crop=CROPS["tomato"])]), TODAY
    )
    assert sum(len(m.weeks) for m in months) == 52
    assert months[0].weeks[0] == "2026-03-02"
    off = [m.label for m in months if not m.active]
    # Dec and Jan are frozen; Feb 2027 is within 12 weeks of the May 1 frost (indoor sowing).
    assert off == ["December 2026", "January 2027"]
    with_indoor = season_months(inputs(), TODAY)  # an indoor plant keeps every month
    assert all(m.active for m in with_indoor)


# ---------- the code rules ----------


def test_frost_rules_remove_early_and_late_plantings_and_caution_the_margin():
    inp = inputs()
    may = weeks_of(2026, 5)  # May 4, 11, 18, 25
    kept, removed = check_items(
        [
            item(may[0], "p1", "transplant", "Plant out the tomatoes"),
            item(may[1], "p1", "transplant", "Plant out the tomatoes"),
            item(may[2], "p1", "transplant", "Plant out the tomatoes"),
        ],
        inp,
        may,
        ULTRA,
    )
    # Very tender: May 1 + 14 days = May 15, so the week of May 11 starts too early as well.
    assert [r["week"] for r in removed] == [may[0], may[1]]
    assert "Too early" in removed[0]["reason"] and "May 15" in removed[0]["reason"]
    assert [k["week"] for k in kept] == [may[2]]
    texts = " ".join(c["text"] for c in kept[0]["cautions"])
    assert "1 year in 10" not in texts  # May 18 is after the 1-in-10 late frost date (May 15)
    octo = weeks_of(2026, 10)
    _, late = check_items([item(octo[0], "p1", "sow", "Sow tomatoes")], inp, octo, ULTRA)
    assert late and "Too late" in late[0]["reason"]


def test_a_planting_under_another_kind_is_still_checked():
    inp = inputs()
    april = weeks_of(2026, 4)
    _, removed = check_items(
        [item(april[0], "p1", "other", "Set out the tomato seedlings in the bed")],
        inp,
        april,
        ULTRA,
    )
    assert removed and "Too early" in removed[0]["reason"]
    kept, _ = check_items(
        [item(april[0], "p1", "sow_indoors", "Sow tomato seeds indoors in trays")],
        inp,
        april,
        ULTRA,
    )
    assert kept  # indoor sowing has no frost rule


def test_hardy_crops_may_go_out_four_weeks_early_with_the_soil_temperature():
    inp = inputs()
    mar, apr = weeks_of(2026, 3), weeks_of(2026, 4)
    _, removed = check_items([item(mar[2], "p2", "sow", "Sow lettuce")], inp, mar, ULTRA)
    assert removed  # Mar 16-22 is before Apr 3
    kept, _ = check_items([item(apr[0], "p2", "sow", "Sow lettuce")], inp, apr, ULTRA)
    texts = " ".join(c["text"] for c in kept[0]["cautions"])
    assert "soil is at least" in texts and "1 year in 10" not in texts  # hardy: no margin note


def test_too_cold_months_and_indoor_plants():
    cold = {
        **CLIM,
        "monthly": [dict(m, tmax_c=3.0) if m["month"] == 11 else m for m in CLIM["monthly"]],
    }
    inp = inputs(clim=cold)
    nov = weeks_of(2026, 11)
    _, removed = check_items([item(nov[0], "p2", "sow", "Sow lettuce")], inp, nov, ULTRA)
    assert removed and "Too cold" in removed[0]["reason"]
    feb = weeks_of(2026, 2)
    kept, _ = check_items([item(feb[0], "p3", "transplant", "Repot the monstera")], inp, feb, ULTRA)
    assert kept  # indoors: no frost or temperature rule


def test_shape_rules_drop_strangers_and_keep_only_supplied_sources():
    inp = inputs()
    jun = weeks_of(2026, 6)
    kept, removed = check_items(
        [
            item(jun[0], "p9", "feed", "Feed the roses"),  # not a plant of this garden
            item("2026-07-06", "p1", "feed", "Feed"),  # not a week of this month
            item(jun[0], "p1", "feed", ""),  # empty
            item(jun[0], "p1", "feed", "Feed the tomatoes", ids=[2, 99, 2, -1]),
            item(jun[1], "garden", "pest_check", "Walk the beds for slugs"),
            item("2026-06-10", "p1", "harvest", "Pick ripe fruit"),  # snapped to its Monday
        ],
        inp,
        jun,
        ULTRA,
    )
    assert removed == []
    assert [k["action"] for k in kept] == [
        "Feed the tomatoes",
        "Walk the beds for slugs",
        "Pick ripe fruit",
    ]
    assert kept[0]["sources"] == [inp.sources[1]]
    assert kept[1]["plant"] == "Whole garden" and kept[1]["sources"] == []
    assert kept[2]["week"] == "2026-06-08"
    assert all(k["by"] == ULTRA and len(k["id"]) == 12 for k in kept)


def test_organic_and_pet_choices_and_toxic_plants():
    tox = PlantState(
        plant("foxglove"),
        profile={
            "facts": {"toxic_to_pets": [fact("toxic_to_pets", "toxic")]},
            "toxicity": {"toxic_to_pets": "toxic", "toxic_to_children": "unknown"},
        },
    )
    hot = {
        **CLIM,
        "monthly": [dict(m, tmax_c=31.0) if m["month"] == 7 else m for m in CLIM["monthly"]],
    }
    inp = inputs(
        prefs={"organic": True, "pets": True},
        states=[PlantState(plant("tomato"), crop=CROPS["tomato"]), tox],
        clim=hot,
    )
    jul = weeks_of(2026, 7)
    kept, removed = check_items(
        [
            item(jul[0], "p1", "protect", "Spray chlorothalonil on the leaves"),
            item(jul[0], "p1", "protect", "Scatter metaldehyde slug bait"),
            item(jul[1], "p1", "protect", "Spray spinosad in the evening"),
            item(jul[1], "p1", "protect", "Spray neem oil on the leaves"),
            item(jul[2], "p2", "prune", "Cut back the foxglove after flowering"),
        ],
        inp,
        jul,
        ULTRA,
    )
    reasons = [r["reason"] for r in removed]
    assert any("organic" in r for r in reasons) and len(removed) == 2
    spinosad = next(k for k in kept if "spinosad" in k["action"])
    assert any("bees" in c["text"] for c in spinosad["cautions"])
    neem = next(k for k in kept if "neem" in k["action"])
    assert any("burn leaves" in c["text"] for c in neem["cautions"])  # July highs of 31 C
    prune = next(k for k in kept if k["kind"] == "prune")
    assert "toxic to pets" in prune["cautions"][0]["text"]
    assert prune["cautions"][0]["source"]["url"] == "https://ext.example.edu/p"


def test_verifier_removes_only_flagged_ids_and_diff_is_computed_in_code():
    inp = inputs()
    jun = weeks_of(2026, 6)
    kept, _ = check_items(
        [
            item(jun[0], "p1", "feed", "Feed the tomatoes"),
            item(jun[0], "p2", "harvest", "Pick lettuce"),
        ],
        inp,
        jun,
        ULTRA,
    )
    out, removed = apply_issues(
        kept,
        [{"id": kept[1]["id"], "problem": "Bolted by June."}, {"id": "nope"}],
        "verifier (super)",
    )
    assert [k["action"] for k in out] == ["Feed the tomatoes"]
    assert removed[0]["reason"] == "Bolted by June." and removed[0]["by"] == "verifier (super)"
    changes = diff_weeks({jun[0]: kept}, {jun[0]: out, jun[1]: kept[1:]})
    assert changes == [
        {"week": jun[0], "added": [], "removed": ["Pick lettuce (lettuce)"]},
        {"week": jun[1], "added": ["Pick lettuce (lettuce)"], "removed": []},
    ]


def test_weather_triggers_need_a_planting_that_the_weather_threatens():
    inp = inputs()
    may = weeks_of(2026, 5)
    kept, _ = check_items(
        [
            item(may[2], "p1", "transplant", "Plant out the tomatoes"),
            item(may[2], "p2", "sow", "Sow lettuce"),
        ],
        inp,
        may,
        ULTRA,
    )
    weeks = {w: [] for w in may} | {may[2]: kept}
    today = date.fromisoformat(may[2])
    days = [
        {
            "date": (today + timedelta(days=i)).isoformat(),
            "temperature_2m_min": 1.0 if i == 2 else 9.0,
            "temperature_2m_max": 20.0,
        }
        for i in range(7)
    ]
    trig = weather_triggers(inp, weeks, days, today)
    assert len(trig) == 1 and trig[0]["kind"] == "frost"
    assert trig[0]["weeks"] == [may[2], may[3]] and trig[0]["key"] == f"frost:{may[2]}"
    assert "1 °C" in trig[0]["text"] and trig[0]["source"]["url"]
    lettuce_only = {may[2]: [k for k in kept if k["ref"] == "p2"]}
    assert weather_triggers(inp, lettuce_only, days, today) == []  # hardy: a light frost is fine
    hot = [dict(d, temperature_2m_min=15.0, temperature_2m_max=34.0) for d in days]
    assert [t["kind"] for t in weather_triggers(inp, lettuce_only, hot, today)] == ["heat"]


def test_calendar_is_all_day_and_cannot_be_injected():
    class W:
        week = "2026-05-18"
        items = [
            {
                "id": "abc",
                "action": "Plant out\r\nATTENDEE:evil",
                "plant": "tomato",
                "why": "warm",
                "cautions": [{"text": "keep a cover", "source": {}}],
                "sources": [],
            }
        ]

    text = ics_calendar(
        calendar_events([W()], "Leaf: Plot"), "Leaf: Plot", "https://leafathon.site", "/season"
    )
    assert "DTSTART;VALUE=DATE:20260518" in text and "DTEND;VALUE=DATE:20260519" in text
    assert "\r\nATTENDEE" not in text and "General practice" in text
    assert text.count("BEGIN:VEVENT") == 1
    with pytest.raises(ValueError):
        ics_calendar([], "x", "https://leafathon.site", "//evil.example")


# ---------- the service and jobs (fake models, real SQLite) ----------


class Q:
    def __init__(self):
        self.sent = []

    async def send(self, jobs, delay_s=0):
        self.sent += list(jobs)


class FakeClimate:
    repo = None

    def __init__(self, fc=None):
        self.fc = fc

    async def forecast(self, cell, now=None):
        return self.fc or fake_forecast(datetime.now(UTC).date())

    async def alerts(self, cell):
        from backend.climate.nws import Alerts

        return Alerts(covered=True, alerts=[])

    async def season_gdd(self, cell, bases):
        return {"by_base_c": {}}


async def setup(responses, failing=None, climate=True):
    db = SqliteDriver()
    await apply(db)
    repo = Repo(db)
    g, key = await repo.create_garden(GardenIn(name="Plot", lat=44.9, lon=-93.2, timezone="UTC"))
    if climate:
        await repo.register_cell(g.cell, 44.9, -93.2)
        await repo.put_climatology(g.cell, "ready", CLIM)
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    await repo.add_plant(g.id, PlantIn(common_name="lettuce"))
    leaf, llm, _ = make_leaf(Settings(nebius_api_key="x"), responses, failing)
    ctx = CareContext(repo, FakeClimate(), leaf)
    return repo, g, ctx, llm


MAY = weeks_of(2026, 5)
JUN = weeks_of(2026, 6)
OUTLINE = {
    "milestones": [
        item(MAY[0], "p1", "transplant", "Plant out the tomatoes"),  # too early: code removes
        item(MAY[2], "p1", "transplant", "Plant out the tomatoes", ids=[2]),
        item("2026-04-06", "p2", "sow", "Sow lettuce", ids=[2]),
        item(MAY[2], "p1", "feed", "Feed the tomatoes"),  # not a planting: not in the outline
    ]
}
MONTH_ITEMS = {
    "items": [
        item(MAY[2], "p2", "harvest", "Pick outer lettuce leaves"),
        item(MAY[3], "p1", "transplant", "Plant out the tomatoes again"),  # plantings: outline only
        item(JUN[0], "p1", "pest_check", "Look for hornworms"),
        item(JUN[1], "p1", "pest_check", "Look for hornworms again"),  # one a month
    ]
}
GOOD = {"season_outline": OUTLINE, "season_plan": MONTH_ITEMS, "plan_verify": {"issues": []}}


async def start(repo, g):
    return await repo.create_season_plan(g.id, "2026-03-02", "2027-02-22", {"made_on": "x"}, "t")


async def week_items(repo, plan_id):
    return {w.week: w.items for w in await repo.list_plan_weeks(plan_id)}


async def test_outline_then_months_then_super_then_current():
    repo, g, ctx, llm = await setup(dict(GOOD))
    lettuce = (await repo.list_plants(g.id))[1]
    harvest = item_id(MAY[2], lettuce.id, "harvest", "Pick outer lettuce leaves")
    llm.responses["plan_verify"] = {"issues": [{"id": harvest, "problem": "Too soon to pick."}]}
    plan = await start(repo, g)
    result = await write_plan(ctx, g, plan, TODAY)
    assert result["status"] == "ready"
    steps = [s for s, _ in llm.calls]
    assert steps[0] == "season_outline" and steps.count("season_plan") == 10  # Dec, Jan dormant
    assert {m for s, m in llm.calls if s in ("season_outline", "season_plan")} == {ULTRA}
    assert {m for s, m in llm.calls if s == "plan_verify"} == {SUPER}
    current = await repo.get_season_plan(g.id)
    assert current.id == plan.id and current.checks["verified_by"] == SUPER
    by = {r["by"]: r["reason"] for r in current.checks["removed"]}
    assert "Too early" in by["code"] and by[f"verifier ({SUPER})"] == "Too soon to pick."
    weeks = await week_items(repo, plan.id)
    assert len(weeks) == 52
    assert [i["action"] for i in weeks[MAY[2]]] == ["Plant out the tomatoes"]  # harvest flagged
    assert weeks[MAY[3]] == []  # the month's own planting was dropped: the outline decides
    assert [i["action"] for i in weeks["2026-04-06"]] == ["Sow lettuce"]
    assert [i["action"] for i in weeks[JUN[0]] + weeks[JUN[1]]] == ["Look for hornworms"]
    assert weeks[MAY[2]][0]["sources"][0]["url"].startswith("https://extension.colostate.edu")
    assert "outline" in current.basis


async def test_a_rewrite_keeps_the_current_plan_until_it_is_ready():
    repo, g, ctx, llm = await setup(dict(GOOD))
    first = await start(repo, g)
    await write_plan(ctx, g, first, TODAY)
    second = await start(repo, g)
    assert (await repo.get_season_plan(g.id)).id == first.id  # still shown while writing
    assert (await repo.latest_season_plan(g.id)).id == second.id
    await write_plan(ctx, g, second, TODAY)
    assert (await repo.get_season_plan(g.id)).id == second.id
    assert await repo.get_plan(first.id) is None  # the older plan and its weeks are gone


async def test_unverified_plans_stay_hidden_and_a_retry_writes_nothing_twice():
    repo, g, ctx, llm = await setup(dict(GOOD) | {"plan_verify": "not json"})
    plan = await start(repo, g)
    with pytest.raises(RuntimeError):
        await write_plan(ctx, g, plan, TODAY)
    assert (await repo.latest_season_plan(g.id)).status == "unverified"
    assert await repo.get_season_plan(g.id) is None
    writes = [s for s, _ in llm.calls if s in ("season_outline", "season_plan")]
    llm.responses["plan_verify"] = {"issues": []}
    result = await write_plan(ctx, g, await repo.get_plan(plan.id), TODAY)
    assert result["status"] == "ready"
    assert [s for s, _ in llm.calls if s in ("season_outline", "season_plan")] == writes


async def test_a_timed_out_write_resumes_with_the_months_still_missing():
    repo, g, ctx, llm = await setup(dict(GOOD))
    plan = await start(repo, g)
    inputs = await gather_inputs(ctx, g)
    months = season_months(inputs, TODAY)
    # As if the first attempt saved its outline and the first three months, then timed out.
    await repo.set_plan_basis(plan, {"outline": [], "outline_by": ULTRA})
    await repo.put_plan_weeks(plan, {w: ([], []) for m in months[:3] for w in m.weeks})
    await write_plan(ctx, g, await repo.get_plan(plan.id), TODAY)
    steps = [s for s, _ in llm.calls]
    assert "season_outline" not in steps and steps.count("season_plan") == 7


async def test_a_plan_no_model_can_write_fails_plainly():
    repo, g, ctx, _ = await setup({}, failing={SUPER, ULTRA})
    plan = await start(repo, g)
    result = await write_plan(ctx, g, plan, TODAY)
    latest = await repo.latest_season_plan(g.id)
    assert result["status"] == "failed" and latest.status == "failed" and "Try again" in latest.note


async def test_no_place_or_no_climate_means_no_plan():
    from backend.season.service import PlanUnavailable

    repo, g, ctx, _ = await setup({}, climate=False)
    with pytest.raises(PlanUnavailable, match="climate data"):
        await gather_inputs(ctx, g)
    g.cell = None
    with pytest.raises(PlanUnavailable, match="place"):
        await gather_inputs(ctx, g)


async def ready_plan(responses):
    repo, g, ctx, llm = await setup(dict(GOOD) | responses)
    plan = await start(repo, g)
    await write_plan(ctx, g, plan, TODAY)
    return repo, g, ctx, llm


async def test_replan_rewrites_only_its_weeks_and_says_what_changed():
    moved = {
        "summary": "Moved planting out past the frost.",
        "weeks": [
            {"week": MAY[2], "items": [item("", "p2", "harvest", "Pick outer lettuce leaves")]},
            {
                "week": MAY[3],
                "items": [item("", "p1", "transplant", "Plant out the tomatoes", ids=[2])],
            },
            {"week": JUN[0], "items": [item("", "p1", "feed", "Not a target week")]},
        ],
    }
    repo, g, ctx, llm = await ready_plan({"replan": moved})
    before = await repo.get_season_plan(g.id)
    trigger = {
        "kind": "frost",
        "key": f"frost:{MAY[2]}",
        "weeks": [MAY[2], MAY[3]],
        "text": "Forecast: a low of 1 °C on Wed May 20.",
        "source": {},
    }
    out = await replan(ctx, g, trigger)
    assert out["changed"] and [m for s, m in llm.calls if s == "replan"] == [ULTRA]
    assert out["changes"] == [
        {"week": MAY[2], "added": [], "removed": ["Plant out the tomatoes (tomato)"]},
        {"week": MAY[3], "added": ["Plant out the tomatoes (tomato)"], "removed": []},
    ]
    sent = llm.sent[[s for s, _ in llm.calls].index("replan")][1][1]["content"]
    assert '"plant": "p1", "kind": "transplant"' in sent  # current items carry refs, not names
    after = await repo.get_season_plan(g.id)
    assert after.revision == before.revision + 1
    weeks = {w.week: w for w in await repo.list_plan_weeks(after.id)}
    assert (
        weeks[MAY[3]].changed.startswith("Forecast: a low of 1 °C") and weeks[MAY[3]].revision == 1
    )
    assert weeks[JUN[0]].changed == ""
    ev = [e for e in await repo.list_events(g.id) if e.kind == "replan"]
    assert ev and ev[0].payload["summary"] == "Moved planting out past the frost."
    assert not await repo.replan_weeks(before, {MAY[2]: []}, changed="x", checks={})  # stale


async def test_refs_follow_plants_after_one_is_archived():
    repo, g, ctx, llm = await ready_plan({"replan": {"summary": "", "weeks": []}})
    tomato, lettuce = await repo.list_plants(g.id)
    await repo.add_plant(g.id, PlantIn(common_name="pepper"))
    await repo.archive_plant(g.id, tomato.id)
    inputs = await gather_inputs(ctx, g)
    assert inputs.by_id(lettuce.id).ref == "p1"  # lettuce moved up a place
    weeks = await week_items(repo, (await repo.get_season_plan(g.id)).id)
    sow = weeks["2026-04-06"][0]
    assert sow["plant_id"] == lettuce.id and inputs.ref_of(sow) == "p1"
    trigger = {"kind": "frost", "key": "k", "weeks": [MAY[2], "2026-04-06"], "text": "t"}
    await replan(ctx, g, trigger)
    sent = llm.sent[[s for s, _ in llm.calls].index("replan")][1][1]["content"]
    assert '"plant": "p1", "kind": "sow"' in sent and "Plant out the tomatoes" not in sent


async def test_replan_job_notifies_and_runs_once_per_event():
    repo, g, ctx, llm = await ready_plan(
        {
            "replan": {
                "summary": "Moved.",
                "weeks": [
                    {
                        "week": MAY[3],
                        "items": [item("", "p1", "transplant", "Plant out the tomatoes")],
                    }
                ],
            }
        }
    )
    runner = JobRunner(
        JobContext(
            repo,
            Q(),
            Settings(nebius_api_key="x"),
            extra={"climate": ctx.climate, "leaf": ctx.leaf},
        )
    )
    trigger = {
        "kind": "frost",
        "key": "frost:x",
        "weeks": [MAY[2], MAY[3]],
        "text": "Frost.",
        "source": {},
    }
    msg = JobMessage(
        kind="replan", key=f"replan:{g.id}:frost:x", payload={"garden_id": g.id, "trigger": trigger}
    )
    status, body = await runner.run(msg)
    assert status == 200 and body["result"]["changed"] and body["result"]["notify"] == "no_channels"
    status, body = await runner.run(msg)
    assert body["status"] == "already_done"


async def test_season_job_skips_superseded_writes():
    repo, g, ctx, llm = await setup(dict(GOOD))
    runner = JobRunner(
        JobContext(
            repo,
            Q(),
            Settings(nebius_api_key="x"),
            extra={"climate": ctx.climate, "leaf": ctx.leaf},
        )
    )
    p1 = await start(repo, g)
    await start(repo, g)  # a rewrite before the first job ran
    _, body = await runner.run(
        JobMessage(kind="season_plan", key="a", payload={"garden_id": g.id, "plan_id": p1.id})
    )
    assert body["result"] == {"skipped": "superseded"}


def test_southern_gardens_plant_out_in_spring_september_to_november():
    south = {
        **CLIM,
        "hemisphere": "south",
        "frost": {
            "enough_data": True,
            "last_spring_frost": {"risk_90": "09-01", "risk_50": "09-15", "risk_10": "10-01"},
            "first_autumn_frost": {"risk_10": "04-20", "risk_50": "05-01", "risk_90": "05-15"},
        },
        "monthly": [dict(m, tmax_c=TMAX[(m["month"] + 5) % 12]) for m in CLIM["monthly"]],
    }
    inp = inputs(clim=south)
    sep, octo, may = weeks_of(2026, 9), weeks_of(2026, 11), weeks_of(2027, 5)
    _, early = check_items(
        [item(sep[2], "p1", "transplant", "Plant out the tomatoes")], inp, sep, ULTRA
    )
    assert early and "Sep 29" in early[0]["reason"]  # Sep 15 + 2 weeks (very tender)
    kept, _ = check_items(
        [item(octo[0], "p1", "transplant", "Plant out the tomatoes")], inp, octo, ULTRA
    )
    assert kept
    _, late = check_items(
        [item(may[0], "p1", "transplant", "Plant out the tomatoes")], inp, may, ULTRA
    )
    assert late and "Too late" in late[0]["reason"]  # after the May 1 autumn frost
    months = season_months(inp, date(2026, 9, 7))
    assert [m.label for m in months if not m.active] == []  # monstera indoors keeps all months


# ---------- endpoints and Leaf Doctor wiring ----------


@contextlib.contextmanager
def api(settings, responses=None):
    leaf, llm, _ = make_leaf(settings, {**RESP, **(responses or {})})
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        app.state.climate = FakeClimate()
        app.state.queue = Q()
        yield client
    app.state.leaf = app.state.deep = app.state.journal = None
    app.state.climate = None


def new_garden(client, place=True):
    body = {"name": "Plot", "timezone": "UTC"} | ({"lat": 44.9, "lon": -93.2} if place else {})
    made = client.post("/api/gardens", json=body).json()
    return made["garden"], {"X-Leaf-Key": made["key"]}


def with_climate(client, g):
    repo = app.state.data.repo
    client.portal.call(repo.register_cell, g["cell"], 44.9, -93.2)
    client.portal.call(repo.put_climatology, g["cell"], "ready", CLIM)
    return repo


def test_season_endpoints_explain_what_is_missing_and_queue_the_writer(settings):
    with api(settings) as client:
        g, h = new_garden(client, place=False)
        gid = g["id"]
        v = client.get(f"/api/gardens/{gid}/season", headers=h).json()
        assert v["plan"] is None and v["pending"] is None
        assert client.post(f"/api/gardens/{gid}/season").status_code == 404  # no key
        r = client.post(f"/api/gardens/{gid}/season", headers=h)
        assert r.status_code == 409 and "place" in r.json()["detail"]
        g, h = new_garden(client)
        gid = g["id"]
        with_climate(client, g)
        r = client.post(f"/api/gardens/{gid}/season", headers=h)
        assert r.status_code == 409 and "plants" in r.json()["detail"]
        client.post(f"/api/gardens/{gid}/plants", headers=h, json={"common_name": "tomato"})
        r = client.post(f"/api/gardens/{gid}/season", headers=h)
        assert r.status_code == 202 and r.json()["pending"]["status"] == "writing"
        assert r.json()["plan"] is None and r.json()["weeks"] == []  # nothing unverified shown
        seasons = lambda: [j for j in app.state.queue.sent if j.kind == "season_plan"]  # noqa: E731
        assert len(seasons()) == 1
        again = client.post(f"/api/gardens/{gid}/season", headers=h)  # still writing: no new job
        assert again.status_code == 202 and len(seasons()) == 1
        assert client.get(f"/api/gardens/{gid}/season.ics", headers=h).status_code == 404
        assert client.get("/season").status_code == 200


def test_rewrites_are_limited_to_three_a_day(settings):
    with api(settings) as client:
        g, h = new_garden(client)
        repo = with_climate(client, g)
        client.post(f"/api/gardens/{g['id']}/plants", headers=h, json={"common_name": "tomato"})
        for _ in range(3):
            assert client.post(f"/api/gardens/{g['id']}/season", headers=h).status_code == 202
            plan = client.portal.call(repo.latest_season_plan, g["id"])
            client.portal.call(repo.set_season_status, plan, "failed", "")
        r = client.post(f"/api/gardens/{g['id']}/season", headers=h)
        assert r.status_code == 429


def test_ready_plan_shows_weeks_today_and_exports_a_calendar(settings):
    with api(settings, GOOD) as client:
        g, h = new_garden(client)
        repo = with_climate(client, g)
        client.post(f"/api/gardens/{g['id']}/plants", headers=h, json={"common_name": "tomato"})
        client.post(f"/api/gardens/{g['id']}/plants", headers=h, json={"common_name": "lettuce"})
        garden_row = client.portal.call(repo.get_garden, g["id"])
        ctx = CareContext(repo, app.state.climate, app.state.leaf)
        plan = client.portal.call(start, repo, garden_row)
        client.portal.call(write_plan, ctx, garden_row, plan, TODAY)
        v = client.get(f"/api/gardens/{g['id']}/season", headers=h).json()
        assert v["plan"]["status"] == "ready" and len(v["weeks"]) == 52 and v["pending"] is None
        assert "trace_id" not in v["plan"] and "outline" not in v["plan"]["basis"]
        ics = client.get(f"/api/gardens/{g['id']}/season.ics", headers=h)
        assert ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar")
        assert ics.text.count("BEGIN:VEVENT") == 4  # tomato out, lettuce sown, harvest, scouting


def report_with(plan):
    return {
        "diagnosis": {"candidates": [{"name": "Early blight", "confidence": 0.8}]},
        "verdict": "likely",
        "plan": plan,
    }


def test_a_diagnosis_joins_the_garden_and_check_ins_close_the_loop(settings):
    with api(settings) as client:
        g, h = new_garden(client)
        p = client.post(
            f"/api/gardens/{g['id']}/plants", headers=h, json={"common_name": "tomato"}
        ).json()
        report = report_with(
            {
                "summary": "Remove spotted leaves and protect new growth.",
                "steps": [
                    {"action": "Remove the spotted lower leaves", "why": "They carry spores."},
                    {"action": "Spray chlorothalonil weekly", "why": "Protects new leaves."},
                ],
                "check_in_days": 5,
            }
        )
        entry = JournalEntry(report=report, expires=time.time() + 3600)
        client.portal.call(app.state.journal.put, entry)
        url = f"/api/gardens/{g['id']}/plants/{p['id']}/diagnosis"
        bad = client.post(url, headers=h, json={"case_id": entry.id, "owner_token": "x" * 16})
        assert bad.status_code == 404
        g2, h2 = new_garden(client)
        assert (
            client.post(
                url, headers=h2, json={"case_id": entry.id, "owner_token": entry.owner_token}
            ).status_code
            == 404
        )
        repo = app.state.data.repo
        client.portal.call(
            repo.update_prefs, g["id"], Prefs(organic=True, pets=False)
        )  # the garden is organic: the synthetic step is removed in code
        r = client.post(
            url, headers=h, json={"case_id": entry.id, "owner_token": entry.owner_token}
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["tasks_added"] == 2 and out["replan"] is False  # no season plan yet
        assert any("organic" in x for x in out["removed"])
        today = client.get(f"/api/gardens/{g['id']}/today", headers=h).json()
        doctor = [t for t in today["tasks"] if t["created_by"] == "doctor"]
        assert [t["title"] for t in doctor] == ["Remove the spotted lower leaves"]
        assert doctor[0]["source"]["url"] == f"/c/{entry.id}"
        again = client.post(
            url, headers=h, json={"case_id": entry.id, "owner_token": entry.owner_token}
        )
        assert again.json()["tasks_added"] == 0  # idempotent
        events = client.portal.call(repo.events_by_ref, "diagnosis", entry.id)
        assert len(events) == 1 and events[0].payload["diagnosis"] == "Early blight"

        # A check-in: the check-in task is done; spreading adds the new steps and another check-in.
        record = CheckIn(
            status="spreading",
            change="More spots",
            steps=[PlanStep(action="Mulch under the plant", why="Stops splash.")],
        )
        ctx = CareContext(repo, app.state.climate, app.state.leaf)
        res = client.portal.call(checkin_loop, ctx, app.state.queue, entry.id, report, record, 1)
        assert res[0]["status"] == "spreading" and res[0]["tasks_added"] == 2
        day = datetime.now(UTC).date()
        tasks = client.portal.call(
            repo.list_tasks, g["id"], day.isoformat(), (day + timedelta(days=10)).isoformat()
        )
        checkins = sorted((t.day, t.status) for t in tasks if t.kind.startswith("checkin:"))
        assert checkins == [
            ((day + timedelta(days=5)).isoformat(), "done"),
            ((day + timedelta(days=7)).isoformat(), "open"),
        ]
        assert any(t.title == "Mulch under the plant" for t in tasks)
        kinds = [e.kind for e in client.portal.call(repo.list_events, g["id"])]
        assert "checkin" in kinds


def test_a_diagnosis_replans_the_season_and_odd_reports_are_refused(settings):
    with api(settings, GOOD) as client:
        g, h = new_garden(client)
        repo = with_climate(client, g)
        p = client.post(
            f"/api/gardens/{g['id']}/plants", headers=h, json={"common_name": "tomato"}
        ).json()
        garden_row = client.portal.call(repo.get_garden, g["id"])
        ctx = CareContext(repo, app.state.climate, app.state.leaf)
        client.portal.call(
            write_plan, ctx, garden_row, client.portal.call(start, repo, garden_row), TODAY
        )
        entry = JournalEntry(
            report=report_with({"summary": "Prune.", "steps": [{"action": "Prune"}]}),
            expires=time.time() + 3600,
        )
        client.portal.call(app.state.journal.put, entry)
        url = f"/api/gardens/{g['id']}/plants/{p['id']}/diagnosis"
        r = client.post(
            url, headers=h, json={"case_id": entry.id, "owner_token": entry.owner_token}
        )
        assert r.json()["replan"] is True
        job = [j for j in app.state.queue.sent if j.kind == "replan"][-1]
        assert job.key.endswith(f"diagnosis:{entry.id[:12]}:{p['id']}")
        assert (
            "Early blight" in job.payload["trigger"]["text"] and "Prune" in job.payload["context"]
        )
        odd = JournalEntry(report=report_with({"steps": "not a list"}), expires=time.time() + 3600)
        client.portal.call(app.state.journal.put, odd)
        r = client.post(url, headers=h, json={"case_id": odd.id, "owner_token": odd.owner_token})
        assert r.status_code == 409


def test_aftercare_that_mentions_planting_stays_but_plantings_come_from_the_outline():
    inp = inputs()
    apr = weeks_of(2026, 4)
    kept, removed = check_items(
        [
            item(apr[2], "p1", "other", "Harden off the tomatoes before planting out"),
            item(apr[2], "p1", "other", "Set out the tomato seedlings in the bed"),
            item(apr[3], "p1", "transplant", "Plant out the tomatoes"),
        ],
        inp,
        apr,
        ULTRA,
    )
    assert [k["action"] for k in kept] == ["Harden off the tomatoes before planting out"]
    assert len(removed) == 2  # a relabelled planting is still caught by its wording
    outline = [dict(kept[0], kind="sow_indoors", action="Sow tomato seeds indoors", id="o1")]
    merged = merge_month(kept, outline, apr)
    assert [m["action"] for m in merged] == ["Sow tomato seeds indoors", kept[0]["action"]]


async def test_a_superseded_write_stops_before_spending_on_months():
    repo, g, ctx, llm = await setup(dict(GOOD))
    plan = await start(repo, g)

    real = repo.set_plan_basis

    async def then_supersede(p, basis):
        await real(p, basis)
        await start(repo, g)  # a newer write arrives right after the outline

    repo.set_plan_basis = then_supersede
    result = await write_plan(ctx, g, plan, TODAY)
    assert result["status"] == "superseded"
    assert [s for s, _ in llm.calls] == ["season_outline"]


async def test_verification_resumes_from_saved_verdicts(monkeypatch):
    import backend.season.service as service
    from backend.models import ModelError
    from backend.prompts import PLAN_VERIFY_SYSTEM

    monkeypatch.setattr(service, "VERIFY_CHUNK", 1)
    monkeypatch.setattr(service, "VERIFY_CONCURRENCY", 1)
    repo, g, ctx, llm = await setup(dict(GOOD))
    plan = await start(repo, g)
    real, seen = llm.complete, {"n": 0}

    async def flaky(*, model, messages, **kw):
        if messages[0]["content"] == PLAN_VERIFY_SYSTEM:
            seen["n"] += 1
            if seen["n"] > 2:  # two chunks pass, then the verifier goes down
                raise ModelError("down")
        return await real(model=model, messages=messages, **kw)

    llm.complete = flaky
    with pytest.raises(RuntimeError):
        await write_plan(ctx, g, plan, TODAY)
    assert len((await repo.get_plan(plan.id)).basis["verdicts"]) == 2
    llm.complete = real
    before = [s for s, _ in llm.calls].count("plan_verify")
    await write_plan(ctx, g, await repo.get_plan(plan.id), TODAY)
    items = (await repo.get_season_plan(g.id)).checks["items"]
    assert [s for s, _ in llm.calls].count("plan_verify") - before == items - 2


async def test_a_resumed_write_drops_plants_archived_meanwhile():
    repo, g, ctx, llm = await setup(dict(GOOD) | {"plan_verify": "not json"})
    plan = await start(repo, g)
    with pytest.raises(RuntimeError):
        await write_plan(ctx, g, plan, TODAY)
    tomato = (await repo.list_plants(g.id))[0]
    await repo.archive_plant(g.id, tomato.id)
    llm.responses["plan_verify"] = {"issues": []}
    await write_plan(ctx, g, await repo.get_plan(plan.id), TODAY)
    weeks = await week_items(repo, (await repo.get_season_plan(g.id)).id)
    left = [it for its in weeks.values() for it in its]
    assert left and all(it["plant_id"] != tomato.id for it in left)


async def test_replans_wait_in_brownout():
    from backend.models import SpendCapReached

    repo, g, ctx, llm = await ready_plan({})

    async def capped(**kw):
        raise SpendCapReached("brownout")

    llm.complete = capped
    out = await replan(ctx, g, {"kind": "frost", "key": "k", "weeks": [MAY[2]], "text": "t"})
    assert out["skipped"].startswith("brownout")


def test_the_outline_moves_a_mid_week_earliest_date_to_the_next_week_instead_of_losing_it():
    from backend.season import outline_items

    inp = inputs()
    may = weeks_of(2026, 5)  # the earliest tomato date is May 15, a Friday: week of May 11
    kept, removed = outline_items(
        [
            item(may[1], "p1", "transplant", "Plant out the tomatoes"),  # holds May 15: moves
            item(may[0], "p1", "sow", "Sow tomatoes outdoors"),  # a week too early: removed
        ],
        inp,
        may,
        ULTRA,
    )
    assert [(k["week"], k["action"]) for k in kept] == [(may[2], "Plant out the tomatoes")]
    assert [r["week"] for r in removed] == [may[0]] and "Too early" in removed[0]["reason"]
