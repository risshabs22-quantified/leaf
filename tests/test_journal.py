"""M5: the plant journal (SQLite and memory), share views, and check-ins. Offline."""

from __future__ import annotations

import base64
import contextlib
import io
import time

from fastapi.testclient import TestClient
from PIL import Image

from backend.app import RateLimiter, app
from backend.journal import JournalEntry, MemoryJournal, SQLiteJournal, public_view
from tests.conftest import make_leaf
from tests.test_agent import RESP, finish, turn

SPREADING = {"status": "Spreading", "change": "More lower leaves have spots.", "confidence": 0.8}
ADVICE = {
    "advice": "Remove the newly spotted leaves.",
    "steps": [{"action": "Remove spotted leaves", "why": "Slows spread", "source_ids": [1, 42]}],
}


def jpeg(size=(1600, 1200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (60, 120, 50)).save(buf, format="JPEG")
    return buf.getvalue()


@contextlib.contextmanager
def api(settings, responses=None, script=None):
    leaf, llm, _ = make_leaf(
        settings,
        responses or {**RESP, "checkin_compare": SPREADING, "checkin_plan": ADVICE, "chat": "ok"},
        None,
        script,
    )
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        yield client, llm
    app.state.leaf = app.state.deep = app.state.journal = None


# ---------------------------------------------------------------- stores


async def test_sqlite_journal_round_trips_and_expires(tmp_path):
    store = SQLiteJournal(tmp_path / "leaf.db")
    live = JournalEntry(expires=time.time() + 60, report={"plan": {"summary": "s"}})
    dead = JournalEntry(expires=time.time() - 1)
    await store.put(live)
    await store.put(dead)
    got = await store.get(live.id)
    assert got and got.report == {"plan": {"summary": "s"}}
    assert await store.get(dead.id) is None
    assert await store.purge() == 1
    reopened = SQLiteJournal(tmp_path / "leaf.db")  # survives a restart
    assert (await reopened.get(live.id)).id == live.id


async def test_memory_journal_hides_expired_entries():
    store = MemoryJournal()
    e = JournalEntry(expires=time.time() - 1)
    await store.put(e)
    assert await store.get(e.id) is None and await store.purge() == 1


def test_share_view_leaves_out_internal_traces():
    e = JournalEntry(expires=time.time() + 60, report={"trace": [1], "steps": [2], "plan": {}})
    assert set(public_view(e)["report"]) == {"plan"}


# ---------------------------------------------------------------- saving reports


def test_a_quick_report_is_saved_with_a_thumbnail_only(settings):
    with api(settings) as (c, _):
        r = c.post(
            "/api/diagnose",
            data={"notes": "spots"},
            files={"image": ("a.jpg", jpeg(), "image/jpeg")},
        ).json()
        assert r["case_id"]
        view = c.get(f"/api/cases/{r['case_id']}").json()
    thumb = Image.open(io.BytesIO(base64.b64decode(view["photos"][0])))
    assert max(thumb.size) <= 512 and view["mode"] == "quick"
    assert "trace" not in view["report"]


def test_a_deep_report_is_saved_under_its_case_id_only_when_finished(settings):
    script = [
        turn(("ask_user", {"reason": "Rings would separate them.", "questions": ["Rings?"]})),
        turn(finish(("Early blight", 0.8, []))),
    ]
    with api(settings, script=script) as (c, _):
        first = c.post("/api/deep", data={"notes": "tomato"}).json()
        assert c.get(f"/api/cases/{first['case_id']}").status_code == 404  # still waiting
        done = c.post(f"/api/deep/{first['case_id']}/answers", json={"answers": ["yes"]}).json()
        view = c.get(f"/api/cases/{done['case_id']}")
    assert done["case_id"] == first["case_id"] and view.status_code == 200
    assert view.json()["mode"] == "deep"


def test_a_journal_outage_never_fails_the_diagnosis(settings):
    with api(settings) as (c, _):

        async def broken(entry):
            raise OSError("disk full")

        app.state.journal.put = broken
        r = c.post("/api/diagnose", data={"notes": "spots"})
    assert r.status_code == 200 and r.json()["diagnosis"]["candidates"]


def test_unknown_reports_are_404(settings):
    with api(settings) as (c, _):
        assert c.get("/api/cases/nope").status_code == 404
        assert (
            c.post(
                "/api/cases/nope/checkin", files={"image": ("a.jpg", jpeg(), "image/jpeg")}
            ).status_code
            == 404
        )


def test_the_share_page_is_served(settings):
    with api(settings) as (c, _):
        r = c.get("/c/anything")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


# ---------------------------------------------------------------- check-ins


def test_a_spreading_check_in_escalates_and_keeps_only_real_citations(settings):
    with api(settings) as (c, llm):
        r = c.post(
            "/api/diagnose",
            data={"notes": "spots"},
            files={"image": ("a.jpg", jpeg(), "image/jpeg")},
        ).json()
        view = c.post(
            f"/api/cases/{r['case_id']}/checkin",
            data={"notes": "worse"},
            files={"image": ("b.jpg", jpeg(), "image/jpeg")},
        ).json()
    ci = view["checkins"][0]
    assert (
        ci["status"] == "spreading"
        and ci["escalate"]
        and "still spreading" in ci["escalate_reason"]
    )
    assert ci["steps"][0]["source_ids"] == [1]  # 42 is not one of the report's sources
    assert ci["photo"]  # the check-in thumbnail becomes the next comparison's "before"
    compare = next(m for s, m in llm.sent if s == "checkin_compare")
    assert sum(1 for part in compare[1]["content"] if part["type"] == "image_url") == 2


def test_a_stable_check_in_does_not_escalate(settings):
    resp = {
        **RESP,
        "checkin_compare": {"status": "stable", "change": "No change."},
        "checkin_plan": ADVICE,
    }
    with api(settings, responses=resp) as (c, _):
        r = c.post(
            "/api/diagnose",
            data={"notes": "spots"},
            files={"image": ("a.jpg", jpeg(), "image/jpeg")},
        ).json()
        view = c.post(
            f"/api/cases/{r['case_id']}/checkin", files={"image": ("b.jpg", jpeg(), "image/jpeg")}
        ).json()
    assert view["checkins"][0]["status"] == "stable" and not view["checkins"][0]["escalate"]


def test_check_in_uploads_get_the_photo_size_limit_not_the_small_default(settings):
    with api(settings) as (c, _):
        r = c.post("/api/diagnose", data={"notes": "spots"}).json()
        big = c.post(
            f"/api/cases/{r['case_id']}/checkin",
            files={"image": ("b.jpg", b"\\0" * (11 * 1024 * 1024), "image/jpeg")},
        )
        ok = c.post(
            f"/api/cases/{r['case_id']}/checkin",
            files={"image": ("b.jpg", jpeg((3000, 3000)), "image/jpeg")},
        )
    assert big.status_code == 413 and ok.status_code == 200


def test_check_ins_are_capped_per_case(settings, monkeypatch):
    import backend.checkin as ck

    monkeypatch.setattr(ck, "MAX_CHECKINS", 1)
    with api(settings) as (c, _):
        r = c.post("/api/diagnose", data={"notes": "spots"}).json()
        url = f"/api/cases/{r['case_id']}/checkin"
        first = c.post(url, files={"image": ("b.jpg", jpeg(), "image/jpeg")})
        second = c.post(url, files={"image": ("b.jpg", jpeg(), "image/jpeg")})
    assert first.status_code == 200 and second.status_code == 409


def test_check_in_steps_get_the_label_rule_and_lose_unsourced_rates(settings):
    advice = {
        "advice": "Treat it.",
        "steps": [
            {"action": "Spray copper fungicide weekly", "source_ids": [1]},
            {"action": "Mix 4 tablespoons of baking soda per gallon and spray", "source_ids": [1]},
        ],
    }
    resp = {
        **RESP,
        "checkin_compare": {"status": "stable", "change": "Same."},
        "checkin_plan": advice,
    }
    with api(settings, responses=resp) as (c, _):
        r = c.post("/api/diagnose", data={"notes": "spots"}).json()
        view = c.post(
            f"/api/cases/{r['case_id']}/checkin", files={"image": ("b.jpg", jpeg(), "image/jpeg")}
        ).json()
    steps = [s["action"] for s in view["checkins"][0]["steps"]]
    assert steps == ["Spray copper fungicide weekly. Follow the product label."]
