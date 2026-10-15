"""M2 climate engine. Golden tests reproduce published numbers (FAO-56 Examples 36 and 38)
or hand-computed synthetic climates; everything runs offline against fake services."""

from __future__ import annotations

import contextlib
import json
import math
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import RateLimiter, app
from backend.cache import SharedCache
from backend.climate import Climate, cell_center
from backend.climate.climatology import (
    Daily,
    annual_extreme_minimums,
    frost_dates,
    hardiness_zone,
    summarise,
)
from backend.climate.crops import CROPS, find_crop
from backend.climate.forecast import Forecast, cost
from backend.climate.nws import active_alerts, parse
from backend.climate.openmeteo import (
    ARCHIVE_URL,
    OpenMeteo,
    WeatherUnavailable,
    climatology_cost,
    units,
)
from backend.climate.risk import frost_level, gdd_day, risks, sowing
from backend.climate.water import (
    Placement,
    effective_rain,
    indoor_advice,
    outdoor_advice,
    p_adjusted,
    step,
    taw_mm,
)
from backend.data.driver import SqliteDriver
from backend.data.kv import MemoryKV
from backend.data.migrate import apply
from backend.data.repo import Repo, utc_day
from backend.jobs import JobContext, JobMessage, JobRunner
from backend.journal import MemoryJournal
from tests.conftest import make_leaf
from tests.test_agent import RESP


def c(f: float) -> float:
    return (f - 32) * 5 / 9


# ---------- golden: USDA hardiness zone ----------


@pytest.mark.parametrize(
    ("mean_min_f", "zone"),
    [
        (-60, "1a"),
        (-55, "1b"),
        (-70, "1a"),  # clamped
        (-17.5, "5a"),  # Minneapolis 2023: 5a (-20 to -15 F)
        (-7, "6a"),  # Denver 2023: 6a (-10 to -5 F)
        (2, "7a"),  # Boston 2023: 7a (0 to 5 F)
        (12, "8a"),  # Atlanta 2023: 8a (10 to 15 F)
        (22, "9a"),  # Seattle 2023: 9a (20 to 25 F)
        (30, "10a"),
        (64.9, "13a"),
        (69, "13b"),
        (80, "13b"),  # clamped
    ],
)
def test_hardiness_zone_follows_usda_10f_zones_and_5f_halves(mean_min_f, zone):
    assert hardiness_zone(c(mean_min_f))["zone"] == zone


def test_zone_range_is_reported_in_both_units():
    z = hardiness_zone(c(-17.5))
    assert z["range_f"] == [-20, -15] and z["range_c"] == [-28.9, -26.1]


def _years(first: int, last: int, tmin_for, tmax: float = 20.0) -> list[Daily]:
    out = []
    d = date(first, 1, 1)
    while d <= date(last, 12, 31):
        out.append(Daily(d, tmin_for(d), tmax, 1.0, 2.0))
        d += timedelta(days=1)
    return out


def test_annual_extreme_minimum_is_the_mean_of_each_years_lowest():
    # Each year's coldest day is -(10 + year index) C; the mean is the zone's input.
    def tmin(d: date) -> float:
        return -(10 + d.year - 1991) if (d.month, d.day) == (1, 15) else 5.0

    days = _years(1991, 2020, tmin)
    mins = annual_extreme_minimums(days)
    assert len(mins) == 30 and mins[0] == -10 and mins[-1] == -39
    summary = summarise(days, lat=45.0)
    assert summary["hardiness_zone"]["mean_extreme_min_c"] == -24.5  # mean of -10..-39
    assert summary["hardiness_zone"]["zone"] == "5b"  # -12.1 F: 5b is -15 to -10 F


# ---------- golden: frost dates ----------


def _frosty(spring_offset, autumn_offset):
    """Frost only on the calendar days 2001-01-01 + offset(year): same month-day every year."""

    def md(offset: int) -> tuple[int, int]:
        d = date(2001, 1, 1) + timedelta(days=offset)
        return d.month, d.day

    def tmin(d: date) -> float:
        k = d.year - 1991
        return -1.0 if (d.month, d.day) in (md(spring_offset(k)), md(autumn_offset(k))) else 5.0

    return tmin


def test_frost_dates_at_10_50_90_percent_and_season_length():
    # Last spring frost 99 + k days after Jan 1 (k = 0..29), first autumn frost 273 + k.
    days = _years(1991, 2020, _frosty(lambda k: 99 + k, lambda k: 273 + k))
    f = frost_dates(days)
    assert f["enough_data"] and f["years"] == 30 and f["years_with_frost"] == 30
    # Spring offsets 99..128: 10th pct 101.9 -> Apr 13, median 113.5 -> Apr 25, 90th 125.1 -> May 6
    assert f["last_spring_frost"] == {"risk_90": "04-13", "risk_50": "04-25", "risk_10": "05-06"}
    # Autumn offsets 273..302: 275.9 -> Oct 4, 287.5 -> Oct 16, 299.1 -> Oct 27
    assert f["first_autumn_frost"] == {"risk_10": "10-04", "risk_50": "10-16", "risk_90": "10-27"}
    assert f["season_days"] == {"short_10": 173, "median": 173, "long_90": 173}


def test_frost_free_years_make_the_rare_levels_unknown_not_wrong():
    # 5 of 30 years have no frost at all: the earliest spring level (10th pct) is "no frost".
    def spring(k: int) -> int | None:
        return None if k < 5 else 99 + k

    def tmin(d: date) -> float:
        k = d.year - 1991
        days = {date(2001, 1, 1) + timedelta(days=o) for o in (spring(k) or -999, 273 + k)}
        frosty = spring(k) is not None and (d.month, d.day) in {(x.month, x.day) for x in days}
        return -1.0 if frosty else 5.0

    f = frost_dates(_years(1991, 2020, tmin))
    assert f["years_with_frost"] == 25
    assert f["last_spring_frost"]["risk_90"] is None
    assert f["last_spring_frost"]["risk_10"] is not None
    assert not any(isinstance(v, float) and math.isnan(v) for v in f["season_days"].values())


def test_a_frost_free_climate_has_no_frost_dates_and_a_full_season():
    f = frost_dates(_years(1991, 2020, lambda d: 15.0))
    assert f["years_with_frost"] == 0
    assert set(f["last_spring_frost"].values()) == {None}
    assert f["season_days"]["median"] == 365


def test_southern_hemisphere_frost_year_runs_july_to_june():
    # Spring frosts in Sep (offset ~70 from Jul 1), autumn frosts in May (offset ~310).
    def tmin(d: date) -> float:
        if (d.month, d.day) in ((9, 10), (5, 20)):
            return -1.0
        return 8.0

    f = frost_dates(_years(1991, 2020, tmin), southern=True)
    assert f["years"] == 29  # Jul-Jun years inside 1991-2020
    assert f["last_spring_frost"]["risk_50"] == "09-10"
    assert f["first_autumn_frost"]["risk_50"] == "05-20"


def test_monthly_normals_and_too_little_data():
    summary = summarise(_years(1991, 2020, lambda d: -2.0 if d.month == 1 else 5.0), lat=50)
    jan = summary["monthly"][0]
    assert jan["frost_days"] == 31 and jan["rain_mm"] == 31.0 and jan["et0_mm"] == 62.0
    with pytest.raises(ValueError):
        summarise(_years(2000, 2010, lambda d: 1.0), lat=50)


# ---------- golden: FAO-56 water balance ----------


@pytest.mark.parametrize(
    ("fc", "wp", "zr", "p", "taw", "raw"),
    [  # FAO-56 Example 36 (published TAW / RAW in mm)
        (0.15, 0.06, 0.4, 0.30, 36, 11),  # onion, loamy sand
        (0.32, 0.15, 0.4, 0.30, 68, 20),  # onion, silt
        (0.35, 0.23, 0.4, 0.30, 48, 14),  # onion, silty clay
        (0.15, 0.06, 0.8, 0.40, 72, 29),  # tomato, loamy sand
        (0.32, 0.15, 0.8, 0.40, 136, 54),  # tomato, silt
        (0.35, 0.23, 0.8, 0.40, 96, 38),  # tomato, silty clay
        (0.15, 0.06, 1.2, 0.55, 108, 59),  # maize, loamy sand
        (0.32, 0.15, 1.2, 0.55, 204, 112),  # maize, silt
        (0.35, 0.23, 1.2, 0.55, 144, 79),  # maize, silty clay
    ],
)
def test_fao56_example_36_taw_and_raw(fc, wp, zr, p, taw, raw):
    t = taw_mm(fc, wp, zr)
    assert round(t) == taw and round(p * t) == raw


def test_fao56_example_38_days_1_and_10():
    # Day 1: start depletion 23 mm (= RAW), irrigation 40 mm, Kc 1.21, ETo 4.5
    d1 = step(23, 40, 1.21 * 4.5, 130 * 0.30)
    assert round(d1.deep_percolation) == 17 and round(d1.depletion_end) == 5
    # Day 10: start depletion 27 mm, irrigation 27 mm, ETo 5.2 -> DP 0, end 6 mm
    d10 = step(27, 27, 1.21 * 5.2, 130 * 0.35)
    assert round(d10.deep_percolation) == 0 and round(d10.depletion_end) == 6


def test_p_adjustment_and_light_rain_rules():
    assert p_adjusted(0.40, 5.75) == pytest.approx(0.37)
    assert p_adjusted(0.20, 12) == 0.1 and p_adjusted(0.75, 0) == 0.8
    assert effective_rain(0.9, 5.0) == 0.0  # below 0.2 x ET0: evaporates
    assert effective_rain(1.0, 5.0) == 1.0


def fake_forecast(
    today: date,
    et0: float = 5.0,
    rain=lambda i: 0.0,
    rain_chance=lambda i: 0,
    temp=lambda h: 15.0,
    soil=12.0,
    gust=10.0,
    utc_offset_s: int = 0,
) -> Forecast:
    """7 past and 7 future days around `today` (index 0 = 7 days ago)."""
    days = [today - timedelta(days=7) + timedelta(days=i) for i in range(14)]
    hours = [
        datetime.combine(days[0], datetime.min.time()) + timedelta(hours=h) for h in range(14 * 24)
    ]
    return Forecast(
        cell="407_-740",
        lat=40.7,
        lon=-74.0,
        timezone="UTC",
        utc_offset_s=utc_offset_s,
        fetched_at=0,
        daily={
            "time": [d.isoformat() for d in days],
            "temperature_2m_max": [20.0] * 14,
            "temperature_2m_min": [8.0] * 14,
            "precipitation_sum": [rain(i) for i in range(14)],
            "precipitation_probability_max": [rain_chance(i) for i in range(14)],
            "et0_fao_evapotranspiration": [et0] * 14,
            "wind_gusts_10m_max": [gust] * 14,
            "sunrise": [f"{d.isoformat()}T06:30" for d in days],
            "sunset": [f"{d.isoformat()}T18:30" for d in days],
        },
        hourly={
            "time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
            "temperature_2m": [temp(h) for h in hours],
            "relative_humidity_2m": [70] * len(hours),
            "precipitation": [0.0] * len(hours),
            "precipitation_probability": [0] * len(hours),
            "wind_speed_10m": [10.0] * len(hours),
            "wind_gusts_10m": [gust] * len(hours),
            "soil_temperature_6cm": [soil] * len(hours),
            "soil_moisture_3_to_9cm": [0.3] * len(hours),
            "soil_moisture_9_to_27cm": [0.3] * len(hours),
            "et0_fao_evapotranspiration": [et0 / 24] * len(hours),
            "sunshine_duration": [1800] * len(hours),
        },
    )


TODAY = date(2026, 7, 15)
NOON = datetime(2026, 7, 15, 12, tzinfo=UTC)
TOMATO = CROPS["tomato"]


def test_outdoor_tomato_in_loam_after_a_dry_week_needs_water():
    # Loam midpoints 0.25/0.12, tomato Zr 0.7 m (FAO's smaller value for scheduling):
    # TAW 91 mm; ETc 1.15 x 5 = 5.75 mm/day; p = 0.40 + 0.04 (5 - 5.75) = 0.37 -> RAW 33.7.
    # Start (heuristic) 0.5 RAW = 16.8 mm, plus 7 dry days of 5.75 mm = 57.1 mm.
    a = outdoor_advice(TOMATO, "fruiting", Placement(), fake_forecast(TODAY), [], NOON)
    assert a.model == "fao56" and a.action == "water"
    assert a.numbers["taw"] == 91.0 and a.numbers["raw"] == pytest.approx(33.7, abs=0.05)
    assert a.amount_mm == pytest.approx(57.1, abs=0.05)
    assert any("heuristic" in x for x in a.assumptions)
    assert {s["url"] for s in a.sources} >= {"https://www.fao.org/4/x0490e/x0490e0e.htm"}


def test_a_logged_watering_refills_and_the_next_date_is_forecast():
    waterings = [{"date": (TODAY - timedelta(days=2)).isoformat()}]  # amount unknown: refill
    a = outdoor_advice(TOMATO, "mid", Placement(), fake_forecast(TODAY), waterings, NOON)
    # Refilled two days ago: 2 x 5.75 = 11.5 mm; RAW 33.7 is reached 3 days from today.
    assert a.action == "skip" and a.numbers["depletion"] == 11.5
    assert a.next_check == (TODAY + timedelta(days=3)).isoformat()


def test_likely_rain_today_skips_a_due_watering():
    fc = fake_forecast(TODAY, rain=lambda i: 80.0 if i == 7 else 0.0, rain_chance=lambda i: 90)
    a = outdoor_advice(TOMATO, "mid", Placement(), fc, [], NOON)
    assert a.action == "skip" and "rain" in a.reason


def test_unlikely_rain_does_not_count():
    fc = fake_forecast(TODAY, rain=lambda i: 80.0 if i == 7 else 0.0, rain_chance=lambda i: 30)
    assert outdoor_advice(TOMATO, "mid", Placement(), fc, [], NOON).action == "water"


def test_a_shallow_raised_bed_limits_the_root_zone():
    a = outdoor_advice(
        TOMATO,
        "mid",
        Placement(kind="raised_bed", bed_depth_m=0.3),
        fake_forecast(TODAY),
        [],
        NOON,
    )
    assert a.numbers["taw"] == 39.0  # 130 mm/m x 0.3 m


def test_pots_are_balanced_in_litres():
    a = outdoor_advice(
        TOMATO, "mid", Placement(kind="pot", pot_litres=20), fake_forecast(TODAY), [], NOON
    )
    assert a.numbers["unit"] == "L" and a.numbers["taw"] == 5.0  # 25% of 20 L (heuristic)
    assert a.action == "water" and a.amount_l and a.amount_mm is None
    assert any("UC ANR" in x for x in a.assumptions)


def test_unknown_plants_and_stages_say_what_was_assumed():
    a = outdoor_advice(None, None, Placement(), fake_forecast(TODAY), [], NOON)
    text = " ".join(a.assumptions)
    assert "No FAO-56 crop coefficients" in text and "mid-season" in text


def test_indoor_model_never_uses_weather_and_learns_from_feedback():
    p = Placement(pot_litres=3, light="medium", need="medium")
    first = indoor_advice(p, TODAY, False, None, [])
    assert first.model == "indoor" and first.action == "check"
    base = indoor_advice(p, TODAY, False, TODAY, []).numbers["interval_days"]
    assert base == 7
    wetter = indoor_advice(p, TODAY, False, TODAY, ["too_wet", "too_wet"]).numbers
    drier = indoor_advice(p, TODAY, False, TODAY, ["too_dry"]).numbers
    assert wetter["interval_days"] > base > drier["interval_days"]
    winter = indoor_advice(p, date(2026, 1, 10), False, date(2026, 1, 10), [])
    assert winter.numbers["winter"] and winter.numbers["interval_days"] > base
    due = indoor_advice(p, TODAY, False, TODAY - timedelta(days=8), [])
    assert due.action == "water" and due.amount_l == 0.6  # a fifth of 3 L
    assert all(s["title"].startswith("Leaf heuristic") for s in due.sources)


def test_placement_comes_from_plant_details_and_ignores_junk():
    assert Placement.from_container({"pot_l": 10}).kind == "pot"
    assert Placement.from_container({"soil": "clay", "colour": "red"}).soil == "clay"
    assert Placement.from_container({"pot_litres": -5}) == Placement()


# ---------- risks, degree days, sowing ----------


def test_frost_levels_follow_nws_and_ncei_thresholds():
    assert frost_level(c(36)) == "frost_possible" and frost_level(c(37)) is None
    assert frost_level(0.0) == "freeze" and frost_level(c(28)) == "hard_freeze"


def test_risks_find_tonights_frost_tomorrows_heat_and_gusts():
    def temp(h: datetime) -> float:
        if (
            h.date() == TODAY
            and h.hour >= 22
            or h.date() == TODAY + timedelta(days=1)
            and h.hour < 6
        ):
            return -1.0
        return 15.0

    fc = fake_forecast(TODAY, temp=temp, gust=80.0)
    fc.daily["temperature_2m_max"][8] = 34.0  # tomorrow
    r = risks(fc, NOON)
    assert r["frost"][0]["night_of"] == TODAY.isoformat() and r["frost"][0]["level"] == "freeze"
    assert r["heat"] == [{"date": (TODAY + timedelta(days=1)).isoformat(), "high_c": 34.0}]
    assert r["wind"] and r["wind"][0]["gust_kmh"] == 80.0
    assert {s["url"] for s in r["sources"]} >= {"https://www.weather.gov/lwx/warningsdefined"}


def test_gdd_single_average_with_cutoff():
    assert gdd_day(10, 30, 10) == 10.0
    assert gdd_day(20, 40, 10) == 15.0  # Tmax capped at 30
    assert gdd_day(0, 8, 10) == 0.0  # Tmax below base
    assert gdd_day(7, 32, 10) == 10.0  # USBR: Tmin raised to 10, Tmax capped at 30


def test_sowing_readiness_by_soil_temperature():
    s = {row["crop"]: row["status"] for row in sowing(12.0)}
    # Tomato: min 10 C, optimum 15.6-29.4 C -> possible; lettuce: min 1.7 C, optimum from 4.4 C
    assert s["tomato"] == "possible" and s["lettuce"] == "ideal" and s["pepper"] == "too_cold"
    assert sowing(None) == []


def test_crop_names_match_species_and_common_names():
    assert find_crop("Solanum lycopersicum").key == "tomato"
    assert find_crop("", "cherry tomato").key == "tomato"
    assert find_crop("Courgette").key == "squash"
    assert find_crop("blueberries").key == "berry_bush"
    assert find_crop("mint") is None


def test_every_crop_value_names_its_source():
    for crop in CROPS.values():
        assert crop.sources, crop.key
        if crop.kc:
            assert "fao56_t12" in crop.sources and crop.zr and crop.p is not None
        if crop.germ_min_c is not None:
            assert {"osu_germination", "ucanr_germination"} & set(crop.sources)


# ---------- Open-Meteo budget, NWS ----------


def test_weighted_units():
    assert units(19, 14) == pytest.approx(1.9) and cost() == pytest.approx(1.9)
    assert units(4, 365 * 5) == pytest.approx(365 * 5 / 14)
    assert 770 < climatology_cost() < 800


async def test_archive_is_fetched_in_spaced_5_year_chunks_and_charged():
    calls, slept = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(dict(req.url.params))
        s, e = (
            date.fromisoformat(req.url.params["start_date"]),
            date.fromisoformat(req.url.params["end_date"]),
        )
        n = (e - s).days + 1
        times = [(s + timedelta(days=i)).isoformat() for i in range(n)]
        return httpx.Response(200, json={"daily": {"time": times, "temperature_2m_min": [1.0] * n}})

    async def sleep(s):
        slept.append(s)

    om = OpenMeteo(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep)
    out = await om.archive_daily(
        40, -74, date(1991, 1, 1), date(2020, 12, 31), ["temperature_2m_min"], "era5_seamless"
    )
    assert len(calls) == 6 and len(slept) == 5
    assert calls[0]["start_date"] == "1991-01-01" and calls[-1]["end_date"] == "2020-12-31"
    assert len(out["time"]) == 10958 and out["time"][-1] == "2020-12-31"
    assert om._spent_local == pytest.approx(10958 / 14, rel=0.01)


async def test_the_daily_allowance_is_enforced_before_calling():
    called = []
    om = OpenMeteo(
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: called.append(1))),
        allowance=10,
    )
    om._spent_local = 9.5
    with pytest.raises(WeatherUnavailable, match="allowance"):
        await om.get(ARCHIVE_URL, {}, 1.0)
    assert called == []


async def test_usage_is_recorded_in_d1_so_it_survives_restarts():
    db = SqliteDriver()
    await apply(db)
    repo = Repo(db)
    om = OpenMeteo(
        repo,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    await om.get(ARCHIVE_URL, {}, 2.5)
    assert await OpenMeteo(repo).spent_today() == 2.5


NWS_SAMPLE = {
    "features": [
        {
            "properties": {
                "id": "a1",
                "event": "Freeze Warning",
                "severity": "Moderate",
                "headline": "Freeze Warning until 9 AM",
                "onset": "2026-10-15T02:00:00-05:00",
                "ends": "2026-10-15T09:00:00-05:00",
                "instruction": "Cover plants.",
            }
        },
        {"properties": {"id": "a2", "event": "Special Marine Warning"}},
    ]
}


def test_nws_alerts_keep_only_garden_events():
    alerts = parse(NWS_SAMPLE)
    assert [(a.event, a.kind) for a in alerts] == [("Freeze Warning", "freeze")]
    assert alerts[0].instruction == "Cover plants."


async def test_nws_outside_the_us_is_remembered_and_needs_a_user_agent():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("user-agent"))
        return httpx.Response(400, json={"detail": "out of bounds"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = SharedCache(MemoryKV())
    first = await active_alerts("515_-1", 51.5, -0.1, cache, http)
    again = await active_alerts("515_-1", 51.5, -0.1, cache, http)
    assert not first.covered and not again.covered and len(seen) == 1
    assert "leafathon.site" in seen[0]


# ---------- the climatology job ----------


async def climate_runner(build=None):
    db = SqliteDriver()
    await apply(db)
    repo = Repo(db)

    class Queue:
        sent: list = []

        async def send(self, jobs, delay_s=0):
            self.sent += [(j, delay_s) for j in jobs]

    climate = Climate(repo, None)
    if build is not None:
        climate.build_climatology = build
    from backend.config import Settings

    ctx = JobContext(repo, Queue(), Settings(nebius_api_key="x"), extra={"climate": climate})
    return JobRunner(ctx), repo, ctx.queue


async def test_climatology_job_builds_once_and_stores_it():
    built = []

    async def build(cell):
        built.append(cell)
        return {"model": "era5_seamless", "hardiness_zone": {"zone": "7a"}}

    runner, repo, _ = await climate_runner(build)
    await repo.register_cell("407_-740", 40.7, -74.0)
    status, body = await runner.run(
        JobMessage(kind="climatology", key="climatology:407_-740", payload={"cell": "407_-740"})
    )
    assert status == 200 and body["result"]["zone"] == "7a"
    assert (await repo.get_climatology("407_-740"))["status"] == "ready"
    again = await runner.run(
        JobMessage(kind="climatology", key="climatology:407_-740:x", payload={"cell": "407_-740"})
    )
    assert again[1]["result"] == {"skipped": "already built"} and built == ["407_-740"]


async def test_the_ninth_new_cell_of_the_day_waits_for_tomorrow():
    async def build(cell):
        return {"model": "m", "hardiness_zone": {"zone": "5a"}}

    runner, repo, queue = await climate_runner(build)
    for i in range(8):
        await repo.register_cell(f"{i}_0", 0.1 * i, 0.0)
        await repo.put_climatology(f"{i}_0", "ready", {"x": 1})
    await repo.register_cell("99_0", 9.9, 0.0)
    status, body = await runner.run(
        JobMessage(kind="climatology", key="climatology:99_0", payload={"cell": "99_0"})
    )
    assert status == 200 and body["result"]["why"] == "daily cell limit"
    tomorrow = utc_day(datetime.now(UTC).timestamp() + 86400).replace("-", "")
    assert body["result"]["deferred_until"].startswith(tomorrow)
    msg, delay = queue.sent[-1]
    assert msg.key.startswith("climatology:99_0:" + tomorrow) and 60 <= delay <= 86400 + 8 * 3600


async def test_a_failed_build_is_marked_and_retried():
    async def build(cell):
        raise WeatherUnavailable("Open-Meteo HTTP 429")

    runner, repo, _ = await climate_runner(build)
    await repo.register_cell("1_1", 0.1, 0.1)
    status, _ = await runner.run(
        JobMessage(kind="climatology", key="climatology:1_1", payload={"cell": "1_1"})
    )
    assert status == 500 and (await repo.get_climatology("1_1"))["status"] == "failed"


def test_cell_centres():
    assert cell_center("407_-740") == (40.7, -74.0) and cell_center("-338_1512") == (-33.8, 151.2)


# ---------- endpoints (fake Open-Meteo and NWS) ----------


def weather_transport(today: date):
    fc = fake_forecast(today)

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.weather.gov" in str(req.url):
            return httpx.Response(200, json=NWS_SAMPLE)
        return httpx.Response(
            200,
            json={
                "timezone": "UTC",
                "utc_offset_seconds": 0,
                "hourly": fc.hourly,
                "daily": fc.daily,
            },
        )

    return httpx.MockTransport(handler)


@contextlib.contextmanager
def api(settings):
    leaf, _, _ = make_leaf(settings, RESP)
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        http = httpx.AsyncClient(transport=weather_transport(datetime.now(UTC).date()))
        app.state.climate.http = http
        app.state.climate.om.http = http
        yield client
    app.state.leaf = app.state.deep = app.state.journal = None


GARDEN = {"name": "Plot", "lat": 40.71, "lon": -74.01, "timezone": "UTC"}


def test_garden_climate_view_has_everything_and_starts_no_build(settings):
    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        client.post(f"/api/gardens/{gid}/plants", headers=h, json={"common_name": "tomato"})
        v = client.get(f"/api/gardens/{gid}/climate", headers=h).json()
        assert v["cell"] == "407_-740" and v["climatology"]["status"] in ("queued", "not_requested")
        assert len(v["days"]) == 7 and v["alerts"]["alerts"][0]["kind"] == "freeze"
        assert [s["crop"] for s in v["sowing"]] == ["tomato"]  # only this garden's crops
        assert v["attribution"]["text"] == "Weather data by Open-Meteo.com"
        assert v["soil_temperature_6cm_c"] == 12.0


def test_watering_advice_outdoor_and_indoor_and_the_log(settings):
    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        tomato = client.post(
            f"/api/gardens/{gid}/plants",
            headers=h,
            json={"common_name": "tomato", "stage": "fruiting"},
        ).json()
        fern = client.post(
            f"/api/gardens/{gid}/plants",
            headers=h,
            json={"common_name": "fern", "setting": "indoor", "container": {"pot_litres": 3}},
        ).json()
        out = client.get(f"/api/gardens/{gid}/plants/{tomato['id']}/water", headers=h).json()
        assert out["model"] == "fao56" and out["action"] == "water"
        log = client.post(
            f"/api/gardens/{gid}/plants/{tomato['id']}/log", headers=h, json={"kind": "watered"}
        )
        assert log.status_code == 200
        after = client.get(f"/api/gardens/{gid}/plants/{tomato['id']}/water", headers=h).json()
        assert after["action"] == "skip"  # refilled today
        ind = client.get(f"/api/gardens/{gid}/plants/{fern['id']}/water", headers=h).json()
        assert ind["model"] == "indoor" and ind["action"] == "check"
        bad = client.post(
            f"/api/gardens/{gid}/plants/nope/log", headers=h, json={"kind": "watered"}
        )
        assert bad.status_code == 404


def test_public_climate_is_rate_limited(settings):
    with api(settings) as client:
        app.state.climate_limiter = RateLimiter(per_ip_per_hour=1, daily_cap=100)
        assert client.get("/api/climate?lat=40.7&lon=-74").status_code == 200
        assert client.get("/api/climate?lat=40.7&lon=-74").status_code == 429
        assert client.get("/api/climate?lat=95&lon=0").status_code == 422


def test_weather_outage_is_a_clear_503(settings):
    with api(settings) as client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502)))
        app.state.climate.om.http = http
        r = client.get("/api/climate?lat=40.7&lon=-74")
        assert r.status_code == 503 and "weather" in r.json()["detail"]


def test_garden_creation_registers_the_cell_for_climatology(settings):
    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        assert made["garden"]["cell"] == "407_-740"
        json.dumps(made)  # serialisable


async def test_a_slow_archive_chunk_is_retried_once_not_the_whole_30_years():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.params["start_date"])
        if len(calls) == 2:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"daily": {"time": [], "temperature_2m_min": []}})

    async def sleep(s):
        return None

    om = OpenMeteo(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep)
    await om.archive_daily(
        40, -74, date(1991, 1, 1), date(2000, 12, 31), ["temperature_2m_min"], "era5_seamless"
    )
    assert calls == ["1991-01-01", "1996-01-01", "1996-01-01"]


def test_leap_years_do_not_shift_frost_dates():
    # A frost every Apr 20, leap years included: every level is exactly Apr 20.
    days = _years(1991, 2020, lambda d: -1.0 if (d.month, d.day) in ((4, 20), (10, 20)) else 5.0)
    f = frost_dates(days)
    assert set(f["last_spring_frost"].values()) == {"04-20"}
    assert set(f["first_autumn_frost"].values()) == {"10-20"}


def test_years_of_missing_data_are_dropped_not_counted_frost_free():
    def tmin(d: date) -> float | None:
        if d.year < 2001:
            return None  # ten years of nulls
        return -1.0 if (d.month, d.day) in ((4, 1), (10, 1)) else 5.0

    f = frost_dates(_years(1991, 2020, tmin))
    assert f == {"years": 20, "enough_data": False}


def test_southern_frost_year_starts_in_july():
    # A frost on Jul 15 belongs to the new frost year's spring (offset 14), not the old autumn.
    days = _years(1991, 2020, lambda d: -1.0 if (d.month, d.day) in ((7, 15), (5, 1)) else 9.0)
    f = frost_dates(days, southern=True)
    assert f["last_spring_frost"]["risk_50"] == "07-15"
    assert f["first_autumn_frost"]["risk_50"] == "05-01"


def test_kc_by_stage_follows_fao_linear_stages():
    from backend.climate.water import kc_for

    assert kc_for(TOMATO, "initial") == 0.6 and kc_for(TOMATO, "mid") == 1.15
    assert kc_for(TOMATO, "development") == 0.875  # halfway 0.6 -> 1.15
    assert kc_for(TOMATO, "late") == 0.975  # halfway 1.15 -> 0.80


def test_logged_litres_on_open_ground_are_converted_or_flagged_never_a_refill():
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    fc = fake_forecast(TODAY)
    no_spread = outdoor_advice(
        TOMATO, "mid", Placement(), fc, [{"date": yesterday, "amount_l": 0.5}], NOON
    )
    assert no_spread.action == "water" and no_spread.amount_mm == pytest.approx(57.1, abs=0.05)
    assert any("litres was not counted" in a for a in no_spread.assumptions)
    spread = outdoor_advice(
        TOMATO, "mid", Placement(canopy_m2=0.5), fc, [{"date": yesterday, "amount_l": 10}], NOON
    )
    assert spread.numbers["depletion"] == pytest.approx(57.1 - 20, abs=0.05)  # 10 L / 0.5 m2


def test_pot_rain_falls_on_the_pot_top_and_mm_logs_convert_to_litres():
    from backend.climate.water import _pot_geometry

    top = _pot_geometry(20)
    pot = Placement(kind="pot", pot_litres=20)
    # Low ET0 keeps the pot away from both limits, so the rain's effect is visible: 10 mm on
    # yesterday lowers the deficit by 10 mm x the pot's top area, not the leaves' area.
    dry = outdoor_advice(TOMATO, "mid", pot, fake_forecast(TODAY, et0=0.5), [], NOON)
    wet_fc = fake_forecast(TODAY, et0=0.5, rain=lambda i: 10.0 if i == 6 else 0.0)
    wet = outdoor_advice(TOMATO, "mid", pot, wet_fc, [], NOON)
    assert 0 < wet.numbers["depletion"] < dry.numbers["depletion"] < 5.0
    assert dry.numbers["depletion"] - wet.numbers["depletion"] == pytest.approx(10 * top, abs=0.06)
    logged = outdoor_advice(
        TOMATO,
        "mid",
        Placement(kind="pot", pot_litres=20),
        fake_forecast(TODAY),
        [{"date": TODAY.isoformat(), "amount_mm": 10}],
        NOON,
    )
    assert logged.history[-1]["depletion"] == pytest.approx(5.0 - 10 * top, abs=0.05)


def test_every_outdoor_advice_labels_its_heuristics():
    a = outdoor_advice(TOMATO, "mid", Placement(), fake_forecast(TODAY), [], NOON)
    assert any("70%" in x for x in a.assumptions)
    assert any(s["title"].startswith("Leaf heuristic") for s in a.sources)


async def test_forecast_is_cached_per_cell_per_hour():
    from backend.climate.forecast import get_forecast

    calls = []
    fc = fake_forecast(TODAY)

    def handler(req):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "timezone": "UTC",
                "utc_offset_seconds": 0,
                "hourly": fc.hourly,
                "daily": fc.daily,
            },
        )

    om = OpenMeteo(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    cache = SharedCache(MemoryKV())
    t = datetime(2026, 7, 15, 9, 5, tzinfo=UTC)
    await get_forecast(om, cache, "407_-740", 40.7, -74.0, t)
    await get_forecast(om, cache, "407_-740", 40.7, -74.0, t + timedelta(minutes=50))
    assert len(calls) == 1
    await get_forecast(om, cache, "407_-740", 40.7, -74.0, t + timedelta(minutes=60))
    assert len(calls) == 2


async def test_builds_wait_for_the_next_hour_when_the_hourly_allowance_is_full():
    async def build(cell):
        raise AssertionError("must not build")

    runner, repo, queue = await climate_runner(build)
    climate = runner.ctx.extra["climate"]
    await repo.add_api_usage("open-meteo:hour", 3500, day=datetime.now(UTC).strftime("%Y-%m-%dT%H"))
    await repo.register_cell("5_5", 0.5, 0.5)
    _, body = await runner.run(
        JobMessage(kind="climatology", key="climatology:5_5", payload={"cell": "5_5"})
    )
    assert body["result"]["why"] == "hour allowance" and climate.om.hourly_allowance == 4000
    assert 60 <= queue.sent[-1][1] <= 2 * 3600


async def test_an_allowance_spent_mid_build_defers_instead_of_failing():
    async def build(cell):
        raise WeatherUnavailable("Leaf's weather allowance for this hour is used up.")

    runner, repo, queue = await climate_runner(build)
    await repo.register_cell("6_6", 0.6, 0.6)
    status, body = await runner.run(
        JobMessage(kind="climatology", key="climatology:6_6", payload={"cell": "6_6"})
    )
    assert status == 200 and body["result"]["why"] == "allowance"
    assert (await repo.get_climatology("6_6"))["status"] == "queued"


async def test_archive_chunks_are_cached_so_a_retry_resumes():
    calls = []

    def handler(req):
        calls.append(req.url.params["start_date"])
        return httpx.Response(
            200, json={"daily": {"time": ["1991-01-01"], "temperature_2m_min": [1.0]}}
        )

    async def sleep(s):
        return None

    cache = SharedCache(MemoryKV())
    om = OpenMeteo(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep, cache=cache
    )
    for _ in range(2):
        await om.archive_daily(
            40, -74, date(1991, 1, 1), date(2000, 12, 31), ["temperature_2m_min"], "era5_seamless"
        )
    assert calls == ["1991-01-01", "1996-01-01"]  # the second build fetched nothing


async def test_a_passing_nws_400_does_not_mark_the_cell_outside_the_us():
    from backend.climate.nws import AlertsUnavailable

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"detail": "Bad request"}))
    )
    with pytest.raises(AlertsUnavailable):
        await active_alerts("407_-740", 40.7, -74.0, SharedCache(MemoryKV()), http)


def test_deferred_builds_are_staggered_per_cell():
    from backend.jobs import _stagger_s

    offsets = {_stagger_s(f"{i}_0") for i in range(40)}
    assert len(offsets) > 10 and max(offsets) < 8 * 3600


def test_garden_view_adds_spray_disease_risk_hours_and_season_degree_days(settings):
    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        client.post(f"/api/gardens/{gid}/plants", headers=h, json={"common_name": "tomato"})
        v = client.get(f"/api/gardens/{gid}/climate", headers=h).json()
        assert "best" in v["spray_window"] or "days" in v["spray_window"]
        assert "tomato" in v["disease_risk"]
        assert v["season_degree_days"]["by_base_c"]["10"] >= 0
        assert len(v["next_48_hours"]) == 48
        assert {"soil_moisture_3_to_9cm", "sunshine_duration", "relative_humidity_2m"} <= set(
            v["next_48_hours"][0]
        )
        assert v["degree_days"][0]["base_c"] == 10.0
