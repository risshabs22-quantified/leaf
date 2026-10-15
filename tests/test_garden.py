"""Organic and pet-safe plans: labelled and enforced in code (backend/actives.py), whatever
the plan writer did, and the preferences reach the plan writer in every mode."""

from __future__ import annotations

import contextlib

from fastapi.testclient import TestClient

from backend.actives import (
    HEAT_LIMIT_C,
    OIL_HEAT_LIMIT_C,
    POISON_LINE,
    apply_garden,
    find_actives,
    garden_prompt,
)
from backend.app import RateLimiter, app
from backend.journal import MemoryJournal
from backend.schemas import CarePlan, Garden, PlanStep
from tests.conftest import PLAN, make_leaf
from tests.test_agent import RESP


def plan(*actions: str) -> CarePlan:
    return CarePlan(summary="s", steps=[PlanStep(action=a) for a in actions])


def test_products_are_recognised_by_name_brand_and_alias():
    names = lambda t: [a.name for a in find_actives(t)]  # noqa: E731
    assert names("Spray Daconil every 7 days") == ["chlorothalonil"]
    assert names("Use a copper fungicide or Bordeaux mixture") == ["copper"]
    assert names("Apply neem oil, then sulphur dust") == ["sulfur", "neem oil"]
    assert names("A pyrethroid such as bifenthrin") == ["pyrethroid"]
    assert names("Pyrethrins from chrysanthemums") == ["pyrethrins"]
    assert names("Remove leaves but keep the plant watered") == []  # "but" is not Bt
    assert names("Apply Bt (Bacillus thuringiensis) to caterpillars") == ["Bt"]


def test_organic_only_removes_synthetic_steps_and_says_why():
    p = plan("Remove spotted leaves", "Spray chlorothalonil weekly", "Spray copper fungicide")
    apply_garden(p, Garden(organic=True))
    assert [s.action for s in p.steps] == ["Remove spotted leaves", "Spray copper fungicide"]
    assert "chlorothalonil" in p.removed[0] and "organic" in p.removed[0]
    assert p.steps[1].organic is True and p.steps[0].organic is None


def test_without_preferences_nothing_is_removed_but_everything_is_labelled():
    p = plan("Spray chlorothalonil weekly", "Scatter metaldehyde slug pellets")
    apply_garden(p, Garden())
    assert len(p.steps) == 2 and not p.removed
    assert p.steps[0].organic is False and p.steps[0].pet_risk == "caution"
    assert p.steps[1].pet_risk == "high"
    assert "dog" in p.steps[1].precautions[0]  # a high risk is always explained


def test_pet_safe_removes_serious_risks_and_adds_how_long_to_keep_pets_away():
    p = plan(
        "Scatter metaldehyde slug pellets", "Spray copper fungicide", "Hand-pick slugs at dusk"
    )
    apply_garden(p, Garden(pets=True))
    assert [s.action for s in p.steps] == ["Spray copper fungicide", "Hand-pick slugs at dusk"]
    assert "pets" in p.removed[0]
    assert "until it dries" in p.steps[0].precautions[0]
    assert POISON_LINE in p.cautions


def test_bees_oil_with_sulfur_and_the_heat_limit():
    p = plan("Spray neem oil in the evening", "Dust with sulfur", "Spray spinosad on caterpillars")
    apply_garden(p, Garden())
    assert any("burn leaves" in c for c in p.cautions)
    assert any("bees" in c for c in p.cautions)
    assert p.bee_safe_hours and p.steps[2].bee_risk
    assert p.spray and p.heat_limit_c == OIL_HEAT_LIMIT_C


def test_a_plan_with_nothing_to_spray_offers_no_spray_window():
    p = plan("Water at the base in the morning", "Mulch around the plant")
    apply_garden(p, Garden())
    assert not p.spray and p.heat_limit_c == HEAT_LIMIT_C and not p.cautions


def test_values_the_model_wrote_into_code_owned_fields_are_discarded():
    p = CarePlan(
        summary="s",
        steps=[PlanStep(action="Spray imidacloprid", organic=True, pet_risk="low", bee_risk=False)],
        removed=["invented"],
        cautions=["invented"],
        spray=False,
    )
    apply_garden(p, Garden())
    s = p.steps[0]
    assert s.organic is False and s.pet_risk == "caution" and s.bee_risk
    assert p.removed == [] and "invented" not in p.cautions and p.spray


def test_organic_with_no_organic_option_left_says_so():
    p = plan("Spray mancozeb", "Remove infected leaves")
    apply_garden(p, Garden(organic=True))
    assert [s.action for s in p.steps] == ["Remove infected leaves"]
    assert any("extension office" in c for c in p.cautions)


def test_garden_prompt_names_the_preferences():
    assert "organic only" in garden_prompt(Garden(organic=True))
    assert "metaldehyde" in garden_prompt(Garden(pets=True))
    assert garden_prompt(Garden()) == "no special preferences"


# ---------------------------------------------------------------- through the API


@contextlib.contextmanager
def api(settings, plan_response):
    leaf, llm, _ = make_leaf(settings, {**RESP, "plan": plan_response})
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        yield client, llm
    app.state.leaf = app.state.deep = app.state.journal = None


SYNTHETIC = {
    **PLAN,
    "steps": [
        {"action": "Remove spotted lower leaves", "why": "Slows spread", "source_ids": [1]},
        {
            "action": "Spray chlorothalonil every 7 days",
            "why": "Protects leaves",
            "source_ids": [1],
        },
    ],
}


def test_quick_sends_the_preferences_to_the_plan_writer_and_enforces_them(settings):
    with api(settings, SYNTHETIC) as (c, llm):
        r = c.post("/api/diagnose", data={"notes": "spots", "organic": "true", "pets": "true"})
        body = r.json()
        saved = c.get(f"/api/cases/{body['case_id']}").json()["report"]
    prompt = next(m for step, m in llm.sent if step == "plan")[1]["content"]
    assert "<garden>organic only" in prompt and "pets or children" in prompt
    assert body["garden"] == {"organic": True, "pets": True}
    assert [s["action"] for s in body["plan"]["steps"]] == ["Remove spotted lower leaves"]
    assert "chlorothalonil" in body["plan"]["removed"][0]
    assert saved["plan"]["removed"] == body["plan"]["removed"]  # the share link shows it too


def test_quick_without_preferences_keeps_the_step_and_labels_it(settings):
    with api(settings, SYNTHETIC) as (c, llm):
        body = c.post("/api/diagnose", data={"notes": "spots"}).json()
    prompt = next(m for step, m in llm.sent if step == "plan")[1]["content"]
    assert "<garden>no special preferences</garden>" in prompt
    steps = body["plan"]["steps"]
    assert len(steps) == 2 and steps[1]["organic"] is False and body["plan"]["spray"]


def test_check_in_advice_keeps_to_the_same_preferences(settings):
    from tests.test_journal import SPREADING, jpeg, owner
    from tests.test_journal import api as journal_api

    advice = {
        "advice": "It is spreading.",
        "steps": [
            {"action": "Spray mancozeb now", "why": "Stops spread", "source_ids": [1]},
            {"action": "Remove newly spotted leaves", "why": "Slows spread", "source_ids": [1]},
        ],
    }
    responses = {**RESP, "checkin_compare": SPREADING, "checkin_plan": advice, "chat": "ok"}
    with journal_api(settings, responses) as (c, _):
        first = c.post(
            "/api/diagnose",
            data={"notes": "spots", "organic": "true"},
            files={"image": ("a.jpg", jpeg(), "image/jpeg")},
        ).json()
        r = c.post(
            f"/api/cases/{first['case_id']}/checkin",
            files={"image": ("b.jpg", jpeg(), "image/jpeg")},
            headers=owner(first),
        )
    assert r.status_code == 200, r.text
    steps = [s["action"] for s in r.json()["checkins"][0]["steps"]]
    assert steps == ["Remove newly spotted leaves"]
