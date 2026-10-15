"""Spray windows (scored in code from the forecast) and calendar reminders (.ics)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from backend.app import RateLimiter, app
from backend.calendar_file import Reminder, ics
from backend.conditions import Place
from backend.journal import MemoryJournal
from backend.spray import plan_spray
from tests.conftest import make_leaf
from tests.test_agent import RESP

START = datetime(2026, 10, 15)  # local midnight
NOW = datetime(2026, 10, 15, 5, 0)


def forecast(overrides=None, *, temp=18.0, wind=8.0, offset=-4 * 3600, hours=144, start=START):
    """Six days of hourly forecast, mild and dry unless overridden per hour index."""
    cols = {
        k: []
        for k in (
            "time",
            "temperature_2m",
            "relative_humidity_2m",
            "precipitation",
            "precipitation_probability",
            "wind_speed_10m",
            "wind_gusts_10m",
        )
    }
    for i in range(hours):
        row = {
            "temperature_2m": temp,
            "relative_humidity_2m": 60,
            "precipitation": 0.0,
            "precipitation_probability": 0,
            "wind_speed_10m": wind,
            "wind_gusts_10m": wind + 4,
        }
        row.update((overrides or {}).get(i, {}))
        cols["time"].append((start + timedelta(hours=i)).isoformat(timespec="minutes"))
        for k, v in row.items():
            cols[k].append(v)
    return {"hourly": cols, "utc_offset_seconds": offset, "timezone": "America/New_York"}


def rain(*idx):
    return {i: {"precipitation": 2.0, "precipitation_probability": 90} for i in idx}


def test_a_calm_dry_week_gives_one_window_a_day_and_picks_the_first():
    out = plan_spray(forecast(), NOW)
    assert len(out.windows) == 5 and len(out.days) == 5
    best = [w for w in out.windows if w.best]
    assert len(best) == 1 and best[0].start.startswith("2026-10-15")
    assert (
        best[0].start_utc.endswith("Z") and best[0].start_utc[11:13] == "10"
    )  # 6:00 local = 10:00 UTC
    assert best[0].end <= "2026-10-15T20:00"


def test_rain_closes_the_hours_before_it_and_wet_leaves_after_it():
    # Rain at 14:00-15:00 on day one.
    out = plan_spray(forecast(rain(14, 15)), NOW)
    day1 = {c["hour"]: c for c in out.days[0].hours}
    assert day1[9]["score"] == -1 and "rain within" in day1[9]["why"]  # under 6 dry hours after
    assert day1[14]["score"] == -1 and day1[16]["score"] == -1  # rain, then wet leaves
    assert day1[17]["score"] == -1 or day1[17]["score"] >= 0  # no crash at the edge
    assert day1[7]["score"] > 0  # 7:00: 7 dry hours after


def test_wind_and_heat_block_hours_and_oil_lowers_the_heat_limit():
    hot = forecast({i: {"temperature_2m": 30.0} for i in range(144)})
    assert plan_spray(hot, NOW, heat_limit_c=32).windows  # 30 C is fine for most products
    oil = plan_spray(hot, NOW, heat_limit_c=29)
    assert not oil.windows and "No hour" in oil.message
    assert all(c["why"] in ("too hot", "dark", "past") for d in oil.days for c in d.hours)
    windy = plan_spray(forecast(wind=20.0), NOW)
    assert not windy.windows


def test_bee_harming_products_only_get_early_or_evening_windows():
    out = plan_spray(forecast(), NOW, bees=True)
    for w in out.windows:
        hour = int(w.start[11:13])
        assert hour < 8 or hour >= 18
    assert "outside bee foraging hours" in out.windows[0].notes


def test_past_hours_today_are_not_offered():
    out = plan_spray(forecast(), datetime(2026, 10, 15, 13, 30))
    today = out.windows[0]
    assert today.start >= "2026-10-15T13:00"
    assert {c["why"] for c in out.days[0].hours if c["hour"] < 13} == {"past"}


def test_a_shorter_dry_spell_scores_lower_and_says_when_rain_comes():
    out = plan_spray(forecast(rain(*range(26, 30))), NOW)  # rain at 2:00-5:00 the next morning
    first = out.windows[0]
    assert first.dry_hours_after < 24 and any("rain about" in n for n in first.notes)
    assert first.score < 100


# ---------------------------------------------------------------- calendar files


def test_ics_has_utc_times_an_alarm_escaped_text_and_a_leaf_link():
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    text = ics(
        Reminder(
            title="Check in: tomato; early blight, leaf 2",
            start=start,
            end=start + timedelta(minutes=15),
            details="Line one\nLine two",
            path="/c/abc",
        ),
        "https://leaf.example.dev/",
        now=start,
    )
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20261020T090000Z" in text and "TRIGGER:-PT60M" in text
    assert "SUMMARY:Check in: tomato\\; early blight\\, leaf 2" in text
    assert "URL:https://leaf.example.dev/c/abc" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))


def test_reminders_refuse_links_to_other_sites_and_times_without_a_zone():
    import pydantic
    import pytest

    start = datetime(2026, 10, 20, 9, tzinfo=UTC)
    for bad in ("https://evil.example/x", "//evil.example", "c/abc", "/c/x\r\nATTACH:https://e.x"):
        with pytest.raises(pydantic.ValidationError):
            Reminder(title="t", start=start, end=start, path=bad)
    with pytest.raises(pydantic.ValidationError):
        Reminder(title="t", start=datetime(2026, 10, 20, 9), end=start)


# ---------------------------------------------------------------- endpoints


class FakeWeather:
    def __init__(self):
        self.asked = []

    async def geocode(self, region):
        return None if region == "Nowhere" else Place("Columbus, Ohio, US", 39.96, -83.0)

    async def forecast(self, lat, lon):
        self.asked.append((lat, lon))
        today = datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        return forecast(offset=0, start=today)


@contextlib.contextmanager
def api(settings):
    leaf, _, _ = make_leaf(settings, RESP)
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    app.state.weather = FakeWeather()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        yield client
    app.state.leaf = app.state.deep = app.state.journal = app.state.weather = None


def test_spray_window_endpoint_geocodes_and_scores(settings):
    with api(settings) as c:
        r = c.post("/api/spray-window", json={"region": "Columbus, OH", "heat_limit_c": 29})
        missing = c.post("/api/spray-window", json={"region": "Nowhere"})
        empty = c.post("/api/spray-window", json={})
        coords = c.post("/api/spray-window", json={"lat": 40.1, "lon": -83.1, "bees": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["place"] == "Columbus, Ohio, US" and body["heat_limit_c"] == 29
    assert body["windows"] and body["attribution"].startswith("Weather data by Open-Meteo")
    assert missing.status_code == 404 and empty.status_code == 400
    assert coords.status_code == 200 and coords.json()["bee_safe_hours"]


def test_calendar_endpoint_returns_an_ics_file(settings):
    with api(settings) as c:
        r = c.post(
            "/api/calendar.ics",
            json={
                "title": "Spray copper",
                "start": "2026-10-20T13:00:00Z",
                "end": "2026-10-20T14:00:00Z",
                "path": "/c/abc",
            },
        )
        bad = c.post(
            "/api/calendar.ics",
            json={
                "title": "x",
                "start": "2026-10-20T13:00:00Z",
                "end": "2026-10-20T14:00:00Z",
                "path": "https://evil.example",
            },
        )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"]
    assert "DTSTART:20261020T130000Z" in r.text and "/c/abc" in r.text
    assert bad.status_code == 422


def test_a_long_good_day_suggests_its_best_three_hours_not_the_whole_day():
    # Too cool to be ideal before 9:00, ideal all afternoon: the earliest ideal 3 hours win.
    temps = {i: {"temperature_2m": 8.0 + 2 * max(0, min(i % 24, 12) - 6)} for i in range(144)}
    w = plan_spray(forecast(temps), NOW).windows[0]
    assert w.start == "2026-10-15T09:00" and w.end == "2026-10-15T12:00"


def test_utc_times_follow_the_clock_change():
    # New York leaves daylight time on Sunday 1 November 2026: 6:00 is 10:00 UTC before
    # and 11:00 UTC after, although the forecast reports one offset (-4 h) for all of it.
    start = datetime(2026, 10, 30)
    out = plan_spray(forecast(start=start), datetime(2026, 10, 30, 5, 0))
    by_day = {w.start[:10]: w for w in out.windows}
    assert by_day["2026-10-30"].start_utc == by_day["2026-10-30"].start[:11] + "10:00Z"
    assert by_day["2026-11-02"].start_utc == "2026-11-02T11:00Z"


def test_the_hour_already_under_way_is_not_offered():
    out = plan_spray(forecast(), datetime(2026, 10, 15, 14, 50))
    assert out.windows[0].start == "2026-10-15T15:00"
