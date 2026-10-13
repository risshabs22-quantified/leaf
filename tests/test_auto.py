"""Three verdicts (confident / likely / lab) and Auto mode (Quick first, Deep only when unsure)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.app import DailyCap, RateLimiter, app
from backend.deep import DeepLeaf
from backend.store import MemoryCaseStore
from backend.verdict import DEEP, HIGH_STAKES_ANYWHERE, HIGH_STAKES_FLOOR, QUICK, decide
from tests.conftest import DIAG_HIGH, DIAG_LOW, make_leaf
from tests.test_agent import RESP, finish, photo_bytes, photos, search, turn

# ---------------------------------------------------------------- verdict rules


@pytest.mark.parametrize(
    ("ranking", "quality", "verdict", "why"),
    [
        ([("Early blight", 0.85), ("Septoria", 0.1)], "good", "confident", "well ahead"),
        ([("Early blight", 0.7), ("Septoria", 0.1)], "good", "likely", "most likely cause"),
        ([("Early blight", 0.7), ("Septoria", 0.6)], "good", "likely", "close behind"),
        ([("Early blight", 0.35), ("Septoria", 0.3)], "good", "lab", "spread out"),
        ([("Early blight", 0.85), ("Septoria", 0.1)], "poor", "lab", "unclear"),
        ([("Late blight", 0.85)], "good", "lab", "thinks this is late blight"),
        ([("Early blight", 0.7), ("Late blight", 0.2)], "good", "lab", "can't rule out"),
        ([], "good", "lab", "could not reach"),
    ],
)
def test_quick_verdicts(ranking, quality, verdict, why):
    d = decide(ranking, quality, QUICK)
    assert d.verdict == verdict and why in d.reason


def test_a_high_stakes_condition_needs_a_real_chance_to_force_a_lab():
    weak = HIGH_STAKES_FLOOR - 0.05
    d = decide([("Early blight", 0.85), ("Late blight", weak)], "good", QUICK)
    assert d.verdict != "lab"
    # below the top two, it takes 20% to force a lab
    d = decide([("Early blight", 0.5), ("Septoria", 0.3), ("Late blight", 0.19)], "good", QUICK)
    assert d.verdict != "lab" and "late blight" not in d.reason.lower()


def test_a_high_stakes_condition_at_20_percent_anywhere_goes_to_a_lab():
    ranking = [("Early blight", 0.85), ("Septoria", 0.3), ("Gray mold", 0.25), ("ToBRFV", 0.2)]
    for t in (QUICK, DEEP):
        d = decide(ranking, "good", t, top_score=0.85)
        assert d.verdict == "lab" and d.high_stakes == "Tomato brown rugose fruit virus"
        assert "can't rule out" in d.reason
    assert HIGH_STAKES_ANYWHERE == 0.2


def test_deep_scores_on_its_combined_confidence():
    ranking = [("Early blight", 0.8), ("Septoria", 0.1)]
    assert decide(ranking, "good", DEEP, top_score=0.25).verdict == "lab"
    assert decide(ranking, "good", DEEP, top_score=0.7).verdict == "confident"
    assert decide(ranking, "good", DEEP, top_score=0.7, contested_by="Septoria").verdict == (
        "likely"
    )
    strong = decide(ranking, "good", DEEP, top_score=0.7, disagreement="They disagree.")
    assert strong.verdict == "lab" and strong.reason == "They disagree."


async def test_quick_reports_its_verdict_and_only_a_lab_verdict_escalates(settings):
    leaf, _, _ = make_leaf(settings, {**RESP, "diagnose": DIAG_HIGH})
    res = await leaf.diagnose(image_b64="abc")
    assert res.verdict == "confident" and not res.plan.escalate and not res.low_confidence
    blight = {
        **DIAG_HIGH,
        "candidates": [
            {"name": "Early blight", "confidence": 0.8},
            {"name": "Late blight (Phytophthora infestans)", "confidence": 0.18},
        ],
    }
    leaf, _, _ = make_leaf(settings, {**RESP, "diagnose": blight})
    res = await leaf.diagnose(image_b64="abc")
    assert res.verdict == "lab" and res.plan.escalate and "report" in res.plan.escalate_reason


async def test_the_model_cannot_escalate_or_clear_a_case_itself(settings):
    plan = {**RESP["plan"], "escalate": True, "escalate_reason": "model says so"}
    leaf, _, _ = make_leaf(settings, {**RESP, "diagnose": DIAG_HIGH, "plan": plan})
    res = await leaf.diagnose(image_b64="abc")
    assert res.verdict == "confident" and res.plan.escalate is False


async def test_watch_for_and_sample_if_survive_messy_model_output(settings):
    plan = {**RESP["plan"], "watch_for": "Fuzzy growth underneath", "sample_if": None}
    leaf, _, _ = make_leaf(settings, {**RESP, "plan": plan})
    res = await leaf.diagnose(image_b64="abc")
    assert res.plan.watch_for == ["Fuzzy growth underneath"] and res.plan.sample_if == ""


# ---------------------------------------------------------------- auto mode


def auto_for(settings, diagnose, script=(), failing=None):
    leaf, llm, searcher = make_leaf(settings, {**RESP, "diagnose": diagnose}, failing, script)
    return DeepLeaf(leaf, MemoryCaseStore()), llm


async def run_auto(deep, **kw):
    events = []

    async def emit(e):
        events.append(e)

    res = await deep.auto(photos=kw.pop("photos", photos()), emit=emit, **kw)
    return res, events


async def test_auto_stops_after_quick_when_quick_is_confident(settings):
    deep, llm = auto_for(settings, DIAG_HIGH)
    asked = []
    res, events = await run_auto(deep, may_investigate=lambda: asked.append(1) or "")
    assert res.mode == "quick" and res.verdict == "confident"
    assert res.auto.investigated is False and res.auto.reason.startswith("Quick was confident")
    assert not asked  # the Deep cap is not spent
    assert not any(step == "investigate" for step, _ in llm.calls)
    assert any(e["type"] == "auto" and e["investigate"] is False for e in events)


async def test_auto_investigates_when_quick_is_unsure_and_starts_from_its_work(settings):
    script = [turn(search()), turn(finish(("Early blight", 0.8, ["E1"])))]
    deep, llm = auto_for(settings, DIAG_LOW, script)
    res, events = await run_auto(deep, notes="tomato")
    assert res.mode == "deep" and res.auto.investigated is True
    assert "Quick was unsure" in res.auto.reason
    steps = [step for step, _ in llm.calls]
    assert steps.count("vision") == 1  # Quick's look at photo 1 is reused
    assert steps.count("diagnose") == 1  # Quick's differential is the seed: no second one
    brief = next(m for s, m in llm.sent if s == "investigate")[1]["content"]
    assert "<quick_diagnosis>" in brief and "Are the spots ringed" not in brief
    assert "Leaf's Quick diagnosis, which was unsure" in brief
    assert [h.name for h in res.first_differential] == ["Early blight", "Nitrogen deficiency"]
    kinds = [e["type"] for e in events]
    assert kinds.index("auto") < kinds.index("case") < kinds.index("done")
    assert res.cost_usd >= sum(t.cost_usd or 0 for t in res.trace if "quick" in t.note)


async def test_the_auto_decision_survives_answering_questions(settings):
    ask = ("ask_user", {"reason": "Rings would separate them.", "questions": ["Rings?"]})
    script = [turn(ask), turn(finish(("Early blight", 0.8, [])))]
    deep, _ = auto_for(settings, DIAG_LOW, script)
    first, _ = await run_auto(deep)
    assert first.status == "needs_answers" and first.auto.investigated
    done = await deep.resume(await deep.store.get(first.case_id), ["yes"])
    assert done.status == "done" and done.auto.reason == first.auto.reason


async def test_auto_reads_quick_sources_instead_of_searching_again(settings):
    deep, llm = auto_for(settings, DIAG_LOW, [turn(finish(("Early blight", 0.8, [])))])
    res, _ = await run_auto(deep)
    brief = next(m for s, m in llm.sent if s == "investigate")[1]["content"]
    assert "<sources_found>" in brief and res.sources


async def test_auto_only_looks_at_the_photos_quick_did_not_see(settings):
    deep, llm = auto_for(settings, DIAG_LOW, [turn(finish(("Early blight", 0.8, [])))])
    res, _ = await run_auto(deep, photos=photos(3))
    assert [step for step, _ in llm.calls].count("vision") == 3  # Quick's 1, then 2 more
    assert all(p.observation for p in res.photos)


async def test_auto_stays_quick_when_the_deep_cap_is_used(settings):
    deep, llm = auto_for(settings, DIAG_LOW)
    res, _ = await run_auto(deep, may_investigate=lambda: "Deep mode has reached today's limit.")
    assert res.mode == "quick" and res.auto.investigated is False
    assert "today's limit" in res.notice and "today's limit" in res.auto.reason
    assert not any(step == "investigate" for step, _ in llm.calls)


async def test_a_failed_investigation_returns_quicks_answer_without_redoing_it(settings):
    deep, llm = auto_for(settings, DIAG_LOW)

    async def boom(case, emit, deadline=None):
        raise KeyError("a bug")

    deep.investigator.run = boom
    res, _ = await run_auto(deep)
    assert res.status == "fallback" and res.mode == "quick" and res.auto.investigated
    assert res.diagnosis.candidates[0].name == "Early blight"
    assert [step for step, _ in llm.calls].count("diagnose") == 1  # Quick is not run twice


# ---------------------------------------------------------------- API


@contextmanager
def api(settings, diagnose, script=()):
    leaf, llm, _ = make_leaf(settings, {**RESP, "diagnose": diagnose}, None, list(script))
    app.state.leaf, app.state.deep = leaf, None
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        yield client, llm
    app.state.leaf = app.state.deep = None


def test_api_auto_spends_the_deep_cap_only_when_it_investigates(settings):
    with api(settings, DIAG_HIGH) as (c, _):
        left = app.state.deep_cap.left
        r = c.post("/api/auto", files=[("images", ("a.jpg", photo_bytes(), "image/jpeg"))])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["auto"]["investigated"] is False and body["verdict"] == "confident"
        assert app.state.deep_cap.left == left and body["case_id"] and body["owner_token"]
    with api(settings, DIAG_LOW, [turn(finish(("Early blight", 0.8, [])))]) as (c, _):
        left = app.state.deep_cap.left
        body = c.post("/api/auto", data={"notes": "tomato spots"}).json()
        assert body["auto"]["investigated"] is True and app.state.deep_cap.left == left - 1


def test_api_auto_past_the_cap_returns_quick_with_the_reason(settings):
    with api(settings, DIAG_LOW) as (c, llm):
        app.state.deep_cap = DailyCap(0)
        body = c.post("/api/auto", data={"notes": "tomato spots"}).json()
    assert body["mode"] == "quick" and "today's limit of 0" in body["auto"]["reason"]
    assert not any(step == "investigate" for step, _ in llm.calls)


def test_api_auto_stream_reports_the_decision_before_the_result(settings):
    with api(settings, DIAG_LOW, [turn(finish(("Early blight", 0.8, [])))]) as (c, _):
        with c.stream("POST", "/api/auto/stream", data={"notes": "tomato"}) as r:
            text = "".join(r.iter_text())
    assert text.index('"type": "auto"') < text.index('"type": "result"')
