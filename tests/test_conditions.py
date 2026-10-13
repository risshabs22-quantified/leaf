"""M2: published disease rules, the Open-Meteo client, and the conditions/read/alerts tools.

Offline: weather comes from an httpx MockTransport, web text from FakeSearcher.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend import disease_rules as rules
from backend.case import Case
from backend.conditions import Place, WeatherClient, parse_hours, summarise
from backend.tools import TOOLS as T
from backend.tools import ToolContext, ToolError, _safe_url
from tests.conftest import FakeSearcher, make_leaf
from tests.test_agent import RESP, collect, deep_for, finish, turn

DAY0 = datetime(2026, 10, 1)


def day_hours(
    day: int, wet_hours: int, wet_temp: float = 20.0, dry_temp: float = 20.0, rain: float = 0.0
) -> list[rules.Hour]:
    """24 hours; the first `wet_hours` at RH 95%, the rest at 60%."""
    out = []
    for h in range(24):
        wet = h < wet_hours
        out.append(
            rules.Hour(
                DAY0 + timedelta(days=day, hours=h),
                wet_temp if wet else dry_temp,
                95.0 if wet else 60.0,
                rain / 24,
            )
        )
    return out


# ---------------------------------------------------------------- rules


def test_wet_hours_count_rh_at_or_above_90():
    hours = day_hours(0, 7) + [rules.Hour(DAY0, 20, 90.0)] * 0
    day = rules.group_days(hours)[0]
    assert day.wet_hours == 7


def test_incomplete_days_are_not_scored():
    assert rules.group_days(day_hours(0, 5)[:23]) == []


@pytest.mark.parametrize(
    ("wet", "tmin", "hutton", "smith"),
    [
        (12, 11.0, True, True),
        (7, 11.0, True, False),
        (5, 11.0, False, False),
        (12, 9.5, False, False),
    ],
)
def test_hutton_and_smith_need_two_warm_humid_days(wet, tmin, hutton, smith):
    days = rules.group_days(
        day_hours(0, wet, wet_temp=tmin, dry_temp=tmin + 5)
        + day_hours(1, wet, wet_temp=tmin, dry_temp=tmin + 5)
    )
    assert bool(rules.hutton_periods(days)) is hutton
    assert bool(rules.smith_periods(days)) is smith


def test_one_qualifying_day_is_not_a_period():
    days = rules.group_days(day_hours(0, 12, 12) + day_hours(1, 2, 12))
    assert rules.hutton_periods(days) == []


@pytest.mark.parametrize(
    ("hours", "temp_f", "value"),
    [
        (18, 65, 3),
        (18, 56, 2),
        (18, 50, 1),  # the worked examples in UMaine bulletin 2418
        (9, 65, 0),
        (10, 65, 1),
        (15, 50, 0),
        (16, 50, 1),
        (22, 65, 5),
        (20, 40, 0),
        (20, 85, 0),
    ],
)
def test_wallin_values_match_the_maine_table(hours, temp_f, value):
    assert rules.wallin_value(hours, temp_f) == value


def test_three_separate_six_hour_periods_score_zero():
    hours = []
    for _ in range(3):
        hours += [rules.Hour(DAY0 + timedelta(hours=len(hours)), 18.3, 95.0) for _ in range(6)]
        hours += [rules.Hour(DAY0 + timedelta(hours=len(hours)), 18.3, 60.0) for _ in range(4)]
    assert [v for _, _, v in rules.wallin_periods(hours)] == [0, 0, 0]


@pytest.mark.parametrize(
    ("wet", "temp", "dsv"),
    [
        (10, 22, 2),
        (7, 13, 1),
        (7, 12.9, 0),
        (23, 19, 4),
        (21, 15, 3),
        (2, 22, 0),
        (3, 22, 1),
        (16, 29.4, 3),
        (16, 29.5, 0),
    ],
)
def test_tomcast_values_match_the_published_table(wet, temp, dsv):
    assert rules.tomcast_dsv(wet, temp) == dsv


def test_tomcast_uses_the_temperature_during_the_wet_hours():
    # Wet hours at 22 C, dry hours at 35 C: the daily mean would be off the table.
    day = rules.group_days(day_hours(0, 10, wet_temp=22, dry_temp=35))[0]
    assert rules.tomcast_day(day) == 2


def test_gdd_caps_the_maximum_and_floors_at_zero():
    hot = rules.group_days(
        [rules.Hour(DAY0 + timedelta(hours=h), 15 if h < 12 else 35, 50) for h in range(24)]
    )[0]
    cold = rules.group_days(
        [rules.Hour(DAY0 + timedelta(hours=h), 5 if h < 12 else 12, 50) for h in range(24)]
    )[0]
    assert rules.gdd(hot) == 12.5 and rules.gdd(cold) == 0.0


# ---------------------------------------------------------------- summary and crop gating

PLACE = Place("Columbus, Ohio, US", 40.0, -83.0, "Ohio", "US")


def fortnight(wet=12, temp=18.0, rain=5.0):
    return sum((day_hours(d, wet, temp, temp + 4, rain) for d in range(14)), [])


def test_tomato_gets_tomcast_only():
    s = summarise(fortnight(), "tomato", PLACE)
    assert [i["name"] for i in s["indices"]] == ["TOMCAST disease severity values"]
    assert s["indices"][0]["last_7_days"] == 7 * rules.tomcast_dsv(12, 18.0)
    assert "ipm.ucanr.edu" in s["indices"][0]["sources"][0]


def test_potato_gets_the_late_blight_rules():
    s = summarise(fortnight(), "potato", PLACE)
    assert {i["name"] for i in s["indices"]} == {
        "Hutton criteria periods",
        "Smith periods",
        "Wallin late blight severity values",
    }
    hutton = next(i for i in s["indices"] if i["name"].startswith("Hutton"))
    assert hutton["count"] == 13  # 14 qualifying days give 13 overlapping pairs


@pytest.mark.parametrize("crop", ["pepper", None])
def test_crops_without_a_published_rule_get_the_general_summary_only(crop):
    s = summarise(fortnight(rain=24.0), crop, PLACE)
    assert s["indices"] == [] and "No published crop-specific disease index" in s["note"]
    assert s["rain_mm_total"] == 336.0 and s["rainy_days"] == 14 and s["wet_hours_total"] == 168
    assert (
        s["attribution"] == "Weather data by Open-Meteo.com" and "modelled humidity" in s["caveat"]
    )


def test_forecast_hours_and_today_are_never_counted():
    now = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
    times = [
        (datetime(2026, 10, 1) + timedelta(hours=h)).isoformat(timespec="minutes")
        for h in range(15 * 24)
    ]  # Oct 1 to Oct 15 23:00 local
    data = {
        "utc_offset_seconds": -4 * 3600,
        "hourly": {
            "time": times,
            "temperature_2m": [20.0] * len(times),
            "relative_humidity_2m": [50] * len(times),
            "precipitation": [0.0] * len(times),
        },
    }
    hours = parse_hours(data, now)
    assert max(h.time for h in hours).date().isoformat() == "2026-10-14"  # local today is Oct 15


def test_missing_values_are_dropped_not_guessed():
    data = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": ["2026-10-01T00:00", "2026-10-01T01:00"],
            "temperature_2m": [None, 20.0],
            "relative_humidity_2m": [95, None],
            "precipitation": [0.0, 0.0],
        },
    }
    assert parse_hours(data, datetime(2026, 10, 5, tzinfo=UTC)) == []


# ---------------------------------------------------------------- weather client (mocked HTTP)


def open_meteo(geo_results, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": geo_results})
        times = [
            (datetime(2026, 9, 28) + timedelta(hours=h)).isoformat(timespec="minutes")
            for h in range(15 * 24)
        ]
        return httpx.Response(
            200,
            json={
                "utc_offset_seconds": 0,
                "timezone": "America/New_York",
                "hourly": {
                    "time": times,
                    "temperature_2m": [18.0] * len(times),
                    "relative_humidity_2m": [95] * len(times),
                    "precipitation": [0.1] * len(times),
                },
            },
        )

    return handler


def client_with(geo, calls):
    http = httpx.AsyncClient(transport=httpx.MockTransport(open_meteo(geo, calls)))
    return WeatherClient(http, now=lambda: datetime(2026, 10, 12, 12, tzinfo=UTC))


COLUMBUS = {
    "name": "Columbus",
    "admin1": "Ohio",
    "country_code": "US",
    "latitude": 39.96,
    "longitude": -82.99,
}
VILLAGE = {
    "name": "Ohio",
    "admin1": "Illinois",
    "country_code": "US",
    "latitude": 41.56,
    "longitude": -89.46,
}


async def test_a_bare_state_name_is_located_at_its_capital_not_a_namesake_village():
    calls = []
    place = await client_with([VILLAGE, COLUMBUS], calls).geocode("Ohio")
    assert place.label == "Ohio (weather at Columbus)" and place.approximate
    assert "name=Columbus" in str(calls[0])


async def test_city_with_state_prefers_the_matching_state():
    calls = []
    other = {**COLUMBUS, "admin1": "Georgia", "latitude": 32.46}
    place = await client_with([other, COLUMBUS], calls).geocode("Columbus, OH")
    assert place.admin1 == "Ohio" and place.lat == 39.96


async def test_weather_is_cached_and_location_is_rounded():
    calls = []
    wx = client_with([COLUMBUS], calls)
    a, _ = await wx.history(39.9612, -82.9988)
    b, _ = await wx.history(39.98, -83.01)  # same 1-decimal cell
    assert a == b and len(calls) == 1
    assert "latitude=40.0" in str(calls[0]) and "longitude=-83.0" in str(calls[0])
    assert all(h.time.date().isoformat() < "2026-10-12" for h in a)


async def test_a_weather_outage_is_a_clean_tool_error(settings):
    def down(request):
        return httpx.Response(503)

    wx = WeatherClient(httpx.AsyncClient(transport=httpx.MockTransport(down)))
    case = Case(region="Columbus, OH")
    ctx = tool_ctx(settings, case, weather=wx)
    with pytest.raises(ToolError, match="weather service"):
        await T["get_conditions"].handler(T["get_conditions"].args(reason="Check rain."), ctx)


# ---------------------------------------------------------------- tools


async def _noop(_):
    return None


def tool_ctx(settings, case, weather=None, searcher=None):
    leaf, _, _ = make_leaf(settings, RESP)
    return ToolContext(leaf.llm, searcher or FakeSearcher(), settings, case, None, _noop, weather)


async def test_get_conditions_without_any_location_says_so(settings):
    ctx = tool_ctx(settings, Case())
    with pytest.raises(ToolError, match="No location"):
        await T["get_conditions"].handler(T["get_conditions"].args(reason="Check rain."), ctx)


async def test_get_conditions_uses_the_crop_and_sends_compact_data(settings):
    calls = []
    case = Case(region="Columbus, OH", crop="tomato")
    ctx = tool_ctx(settings, case, weather=client_with([COLUMBUS], calls))
    out = await T["get_conditions"].handler(T["get_conditions"].args(reason="Wet weather?"), ctx)
    assert out.data["indices"][0]["name"].startswith("TOMCAST")
    assert len(out.data["daily_wet_hours"]) == 14 and "daily" not in out.data
    assert "TOMCAST" in out.summary


async def test_browser_location_is_used_before_the_typed_region(settings):
    calls = []
    case = Case(region="Paris", lat=40.0, lon=-83.0)
    ctx = tool_ctx(settings, case, weather=client_with([], calls))
    out = await T["get_conditions"].handler(T["get_conditions"].args(reason="Rain?"), ctx)
    assert out.data["place"] == "your location (approximate)"
    assert not any("geocoding" in str(u) for u in calls)


async def test_read_source_only_opens_urls_from_this_cases_search(settings):
    case = Case()
    ctx = tool_ctx(settings, case)
    with pytest.raises(ToolError, match="search_sources first"):
        await T["read_source"].handler(
            T["read_source"].args(reason="Read it.", source_id="S1", query="rings"), ctx
        )
    case.add_source("https://extension.umn.edu/x", "UMN", "", True)
    out = await T["read_source"].handler(
        T["read_source"].args(reason="Read it.", source_id="s1", query="rings"), ctx
    )
    assert out.source_ids == ["S1"] and case.sources[0].passages and case.tavily_credits == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/x",
        "ftp://a.edu/x",
    ],
)
def test_unsafe_urls_are_never_opened(url):
    assert _safe_url(url) is False


def test_normal_web_pages_are_allowed():
    assert _safe_url("https://extension.umn.edu/x") and _safe_url("https://8.8.8.8/x")


async def test_check_alerts_searches_recent_news_and_registers_sources(settings):
    searcher = FakeSearcher()
    case = Case(crop="tomato")
    ctx = tool_ctx(settings, case, searcher=searcher)
    out = await T["check_alerts"].handler(
        T["check_alerts"].args(reason="Outbreaks?", region="Ohio"), ctx
    )
    query, kwargs = searcher.web_calls[0]
    assert query == "tomato disease outbreak Ohio"
    assert kwargs["topic"] == "news" and kwargs["time_range"] == "month"
    assert out.source_ids and case.sources


async def test_no_outbreak_news_is_a_finding_not_an_error(settings):
    searcher = FakeSearcher()

    async def empty(query, **kw):
        return [], 1

    searcher.search_web = empty
    ctx = tool_ctx(settings, Case(), searcher=searcher)
    out = await T["check_alerts"].handler(
        T["check_alerts"].args(reason="Outbreaks?", region="Ohio"), ctx
    )
    assert out.data["results"] == [] and "does not rule anything out" in out.data["note"]


# ---------------------------------------------------------------- end to end, scripted


async def test_a_deep_case_reads_a_source_and_the_plan_uses_its_passages(settings):
    script = [
        turn(("search_sources", {"reason": "Find guidance.", "query": "tomato early blight"})),
        turn(
            (
                "read_source",
                {"reason": "Check the ID features.", "source_id": "S1", "query": "rings and halos"},
            )
        ),
        turn(finish(("Early blight", 0.8, ["E1", "E2"]))),
    ]
    deep, llm, searcher = deep_for(settings, script)
    res, _ = await collect(deep)
    assert [s.tool for s in res.steps] == ["search_sources", "read_source", "finish"]
    assert searcher.extracted == ["https://extension.umn.edu/early-blight"]  # trusted first = S1
    plan_prompt = next(m for s, m in llm.sent if s == "plan")[-1]["content"]
    assert "Passage about rings and halos" in plan_prompt
