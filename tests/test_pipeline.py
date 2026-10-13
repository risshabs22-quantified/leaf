import json

import pytest

from backend.config import NEMOTRON_LIGHTNING, NEMOTRON_SUPER, NEMOTRON_ULTRA
from backend.models import extract_json, strip_think
from backend.schemas import ChatRequest
from backend.search import rank_sources
from backend.vision import choose_vision_model
from tests.conftest import DIAG_HIGH, DIAG_LOW, OBS, PLAN, make_leaf


def test_extract_json_handles_fences_and_think_tags():
    raw = '<think>hmm</think>Here you go:\n```json\n{"a": 1}\n```'
    assert extract_json(raw) == {"a": 1}
    assert extract_json('noise {"b": [1, 2]} trailing') == {"b": [1, 2]}
    assert strip_think("reasoning...</think> answer") == "answer"


def test_choose_vision_model_prefers_nvidia_then_preference():
    img = {"architecture": {"modality": "text+image->text"}}
    txt = {"architecture": {"modality": "text->text"}}
    models = [
        {"id": "zai-org/GLM-5.3-Flash", **img},
        {"id": "nvidia/some-vl", **img},
        {"id": "nvidia/Nemotron-3_5-Lightning", **txt},
    ]
    assert choose_vision_model(models) == "nvidia/some-vl"
    assert choose_vision_model(models[:1] + models[2:]) == "zai-org/GLM-5.3-Flash"
    assert choose_vision_model([models[2]]) is None
    assert choose_vision_model([], override="x/y") == "x/y"


def test_rank_sources_prefers_extension_pages():
    out = rank_sources(
        [
            {"url": "https://a.com", "title": "A", "content": "", "score": 0.99},
            {"url": "https://extension.psu.edu/x", "title": "PSU", "content": "", "score": 0.1},
            {"url": "https://a.com", "title": "dup", "content": "", "score": 0.5},
        ]
    )
    assert [s.url for s in out] == ["https://extension.psu.edu/x", "https://a.com"]
    assert [s.id for s in out] == [1, 2]


async def test_diagnose_happy_path_routes_models(settings):
    leaf, llm, searcher = make_leaf(settings, {"vision": OBS, "diagnose": DIAG_HIGH, "plan": PLAN})
    res = await leaf.diagnose(image_b64="abc", notes="spots", region="Ohio")
    assert res.low_confidence is False
    assert res.plan.escalate is False
    assert res.diagnosis.candidates[0].name == "Early blight"
    # Routing: vision model, then Ultra with reasoning, then Lightning for writing.
    assert llm.calls == [
        ("vision", "nvidia/fake-vl"),
        ("diagnose", NEMOTRON_ULTRA),
        ("plan", NEMOTRON_LIGHTNING),
    ]
    assert searcher.queries == ["tomato early blight management Ohio"]
    # Trusted source ranked first; hallucinated source id 99 removed.
    assert res.sources[0].url.startswith("https://extension.umn.edu")
    assert res.plan.steps[0].source_ids == [1]
    assert [t.step for t in res.trace] == ["vision", "diagnose", "plan"]


async def test_widely_spread_causes_always_go_to_a_lab(settings):
    spread = {
        **DIAG_LOW,
        "candidates": [
            {"name": "Early blight", "category": "fungal", "confidence": 0.3},
            {"name": "Nitrogen deficiency", "category": "nutrient", "confidence": 0.25},
        ],
    }
    leaf, _, _ = make_leaf(settings, {"vision": OBS, "diagnose": spread, "plan": PLAN})
    res = await leaf.diagnose(image_b64="abc")
    assert res.low_confidence is True and res.verdict == "lab"
    assert res.plan.escalate is True  # enforced in code even though the model said False
    assert res.diagnosis.follow_up_questions


async def test_a_weak_lead_is_likely_with_follow_up_questions(settings):
    leaf, _, _ = make_leaf(settings, {"vision": OBS, "diagnose": DIAG_LOW, "plan": PLAN})
    res = await leaf.diagnose(image_b64="abc")
    assert res.verdict == "likely" and res.low_confidence and not res.plan.escalate
    assert res.diagnosis.follow_up_questions


async def test_falls_back_to_super_when_ultra_fails(settings):
    leaf, llm, _ = make_leaf(
        settings,
        {"vision": OBS, "diagnose": DIAG_HIGH, "plan": PLAN},
        failing={NEMOTRON_ULTRA},
    )
    res = await leaf.diagnose(image_b64="abc")
    assert ("diagnose", NEMOTRON_SUPER) in llm.calls
    notes = [t.note for t in res.trace if t.step == "diagnose"]
    assert notes[0].startswith("failed") and notes[1] == "fallback"


async def test_malformed_json_is_repaired(settings):
    leaf, llm, _ = make_leaf(
        settings,
        {"vision": OBS, "diagnose": "I think it's blight!", "repair": DIAG_HIGH, "plan": PLAN},
    )
    res = await leaf.diagnose(image_b64="abc")
    assert res.diagnosis.candidates[0].name == "Early blight"
    assert ("repair", NEMOTRON_LIGHTNING) in llm.calls


async def test_text_only_diagnosis_skips_vision(settings):
    leaf, llm, _ = make_leaf(settings, {"diagnose": DIAG_HIGH, "plan": PLAN})
    res = await leaf.diagnose(image_b64=None, notes="yellow lower leaves")
    assert all(step != "vision" for step, _ in llm.calls)
    assert res.observation.plant_guess == "unknown"


async def test_chat_uses_lightning(settings):
    leaf, llm, _ = make_leaf(
        settings,
        {"vision": OBS, "diagnose": DIAG_HIGH, "plan": PLAN, "chat": "Water at the base [1]."},
    )
    res = await leaf.diagnose(image_b64="abc")
    reply = await leaf.chat(ChatRequest(context=res, message="How often should I water?"))
    assert reply.reply.startswith("Water")
    assert llm.calls[-1] == ("chat", NEMOTRON_LIGHTNING)


@pytest.mark.parametrize("payload", [DIAG_HIGH, DIAG_LOW])
def test_fixtures_are_valid_json(payload):
    assert json.loads(json.dumps(payload))


async def test_poor_photo_always_escalates(settings):
    poor = {**OBS, "image_quality": "poor"}
    leaf, _, _ = make_leaf(settings, {"vision": poor, "diagnose": DIAG_HIGH, "plan": PLAN})
    res = await leaf.diagnose(image_b64="abc")
    assert res.low_confidence is True
    assert res.plan.escalate is True


async def test_photo_without_vision_model_uses_notes_only(settings):
    leaf, llm, _ = make_leaf(settings, {"diagnose": DIAG_HIGH, "plan": PLAN})
    leaf.vision_model = None
    res = await leaf.diagnose(image_b64="abc", notes="spots")
    assert all(step != "vision" for step, _ in llm.calls)
    assert "No vision model" in res.observation.quality_notes
    assert res.plan.escalate is True  # poor image quality => escalate


def test_trusted_hosts_match_exactly():
    from backend.search import _trust

    assert _trust("https://extension.umn.edu/x") == 1
    assert _trust("https://www.usda.gov/x") == 1
    assert _trust("https://ask.extension.org/x") == 1
    assert _trust("https://www.rhs.org.uk/x") == 1
    assert _trust("https://best-browser-extension.com/x") == 0
    assert _trust("https://myeducation.blog/x") == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://fakeextension.org/x",  # bare suffix match
        "https://myrhs.org.uk/x",
        "https://extension.evil.com/x",  # "extension." prefix is not a trust signal
        "https://example.edu.evil.com/x",  # trusted name used as a subdomain label
        "https://evil.com/?u=https://a.edu",  # trusted host only in the query string
        "not a url",
    ],
)
def test_lookalike_hosts_are_not_trusted(url):
    from backend.search import _trust

    assert _trust(url) == 0


async def test_tavily_results_are_cached():
    from backend.search import TavilySearcher

    calls = []

    class FakeClient:
        async def search(self, query, **kwargs):
            calls.append(query)
            return {"results": [{"url": "https://a.edu", "title": "A", "content": "", "score": 1}]}

    from datetime import date

    from backend.search import CreditBudget

    budget = CreditBudget(None, max_daily=5, today=lambda: date(2026, 10, 13))
    s = TavilySearcher.__new__(TavilySearcher)
    s._client, s._cache, s.budget = FakeClient(), {}, budget
    first = await s.search("Tomato blight")
    second = await s.search("tomato blight ")
    assert first == second and len(calls) == 1
    assert budget.used == 1  # a cache hit spends no credit
