"""M3: second opinion, confidence in code, plan from passages, claim check, lab handoff."""

from __future__ import annotations

import pytest

from backend.case import Case, Hypothesis, Photo
from backend.claims import code_checks, ensure_passages, judge
from backend.critic import CriticOpinion, add_alternative, combine
from backend.deep import deep_decision
from backend.handoff import NPDN_DIRECTORY, state_code
from backend.schemas import CarePlan, Observation, PlanStep
from tests.conftest import FakeSearcher, make_leaf
from tests.test_agent import RESP, collect, deep_for, finish, search, turn


def case_with(*hyps, quality="good", evidence=2):
    case = Case(notes="tomato")
    for i in range(evidence):
        case.add_evidence("look_closer", f"finding {i}")
    case.differential = [
        Hypothesis(name=n, probability=p, evidence_for=[f"E{i + 1}" for i in range(evidence)])
        for n, p in hyps
    ]
    if quality:
        case.photos = [
            Photo(
                id="P1", full_b64="x", view_b64="x", observation=Observation(image_quality=quality)
            )
        ]
    return case


def opinion(agrees=True, lead=0.8, alt="Septoria", alt_p=0.1):
    return CriticOpinion(
        alternative=alt,
        alternative_probability=alt_p,
        leading_probability=lead,
        agrees=agrees,
        argument="...",
    )


# ---------------------------------------------------------------- confidence


def test_agreement_averages_the_two_opinions():
    c = combine(case_with(("Early blight", 0.8)), opinion(lead=0.7))
    assert c.value == 0.75 and not c.disagreement


def test_disagreement_takes_the_lower_estimate():
    c = combine(
        case_with(("Early blight", 0.85)),
        opinion(agrees=False, lead=0.3, alt="Late blight", alt_p=0.6),
    )
    assert c.disagreement and c.value == 0.3


def test_disagreement_is_the_critics_own_vote():
    # A close alternative alone is not a disagreement: the fitted rule follows the critic's vote.
    c = combine(case_with(("Early blight", 0.8)), opinion(agrees=True, lead=0.5, alt_p=0.45))
    assert not c.disagreement
    c = combine(case_with(("Early blight", 0.8)), opinion(agrees=False, lead=0.6, alt_p=0.2))
    assert c.disagreement


@pytest.mark.parametrize(
    ("quality", "factor"), [("good", 1.0), ("fair", 0.95), ("poor", 0.7), (None, 0.9)]
)
def test_photo_quality_scales_confidence(quality, factor):
    c = combine(case_with(("Early blight", 0.8), quality=quality), opinion(lead=0.8))
    assert c.quality_factor == factor and c.value == pytest.approx(0.8 * factor, abs=1e-3)


@pytest.mark.parametrize(("n", "factor"), [(0, 0.8), (1, 0.9), (2, 1.0), (4, 1.0)])
def test_evidence_coverage_scales_confidence(n, factor):
    c = combine(case_with(("Early blight", 0.8), evidence=n), opinion(lead=0.8))
    assert c.coverage_factor == factor


def test_without_a_second_opinion_leaf_is_less_sure_and_never_near_certain():
    c = combine(case_with(("Early blight", 0.9)), None)
    assert c.critic is None and c.value == pytest.approx(0.81)
    assert combine(case_with(("Early blight", 0.9)), opinion(lead=1.0)).value <= 0.9


def test_the_critics_alternative_is_shown_when_the_investigator_left_it_out():
    case = case_with(("Early blight", 0.8))
    add_alternative(case, opinion(agrees=False, alt="Late blight", alt_p=0.6))
    assert case.differential[-1].name == "Late blight (second opinion)"
    case2 = case_with(("Early blight (Alternaria solani)", 0.8), ("Late blight", 0.1))
    add_alternative(case2, opinion(alt="late blight", alt_p=0.6))
    assert len(case2.differential) == 2  # already there: not duplicated


async def test_the_critic_sees_evidence_but_not_the_investigators_reasoning(settings):
    deep, llm, _ = deep_for(
        settings,
        [turn(search(), finish(("Early blight", 0.83, ["E1"]), summary="SECRET-REASONING"))],
    )
    await collect(deep)
    critic_prompt = next(m for s, m in llm.sent if s == "critic")[-1]["content"]
    assert "Early blight" in critic_prompt and "E1" in critic_prompt
    assert "SECRET-REASONING" not in critic_prompt and "0.83" not in critic_prompt


# ---------------------------------------------------------------- escalation


def verdict(case, critic):
    d = deep_decision(case, combine(case, critic), critic)
    return d.verdict, d.reason


def test_deep_verdicts_come_most_specific_first():
    agree = opinion(lead=0.8)
    tie = case_with(("Early blight", 0.5), ("Septoria", 0.42))
    v, why = verdict(tie, agree)
    assert v == "likely" and "close behind" in why  # a near-tie is Likely, not a lab
    dis = opinion(agrees=False, lead=0.3, alt="Septoria", alt_p=0.6)
    case = case_with(("Early blight", 0.85))
    v, why = verdict(case, dis)
    assert v == "lab" and "Septoria" in why
    poor = case_with(("Early blight", 0.85), quality="poor")
    assert verdict(poor, agree)[0] == "lab" and "unclear" in verdict(poor, agree)[1]
    clear = case_with(("Early blight", 0.85))
    assert verdict(clear, agree)[0] == "confident"


def test_a_high_stakes_condition_in_the_top_two_goes_to_a_lab():
    agree = opinion(lead=0.8)
    case = case_with(("Early blight", 0.8), ("Late blight", 0.2))
    v, why = verdict(case, agree)
    assert v == "lab" and "can't rule out late blight" in why and "report" in why


async def test_a_disagreeing_critic_escalates_and_produces_a_lab_handoff(settings):
    disagrees = {
        "alternative": "Late blight",
        "alternative_probability": 0.6,
        "leading_probability": 0.3,
        "agrees": False,
        "key_evidence": ["E1"],
        "argument": "White growth on the lesion is a sign of late blight.",
    }
    deep, _, _ = deep_for(
        settings,
        [turn(search(), finish(("Early blight", 0.85, ["E1"])))],
        responses={**RESP, "critic": disagrees},
    )
    res, events = await collect(deep, region="Columbus, OH", notes="potato leaves")
    assert res.plan.escalate and res.low_confidence and res.confidence.disagreement
    assert "late blight" in res.plan.escalate_reason.lower() and res.verdict == "lab"
    assert any(h.name == "Late blight (second opinion)" for h in res.differential)
    h = res.handoff
    assert h.lab_url == f"{NPDN_DIRECTORY}?state=OH&region=" and h.lab_state == "OH"
    assert "paused or reduced" in h.lab_notice and len(h.sampling_sources) == 3
    assert h.differential and h.history["notes"] == "potato leaves"
    assert any(e["type"] == "critic" for e in events)


async def test_a_confident_agreed_case_has_no_handoff(settings):
    deep, _, _ = deep_for(
        settings,
        [turn(search(), search("septoria spots"), finish(("Early blight", 0.85, ["E1", "E2"])))],
    )
    res, _ = await collect(deep)
    assert not res.plan.escalate and res.handoff is None and res.confidence.value >= 0.6


async def test_an_unavailable_second_opinion_does_not_break_the_case(settings):
    resp = {k: v for k, v in RESP.items() if k != "critic"}
    deep, _, _ = deep_for(
        settings, [turn(search(), finish(("Early blight", 0.8, ["E1"])))], responses=resp
    )
    res, _ = await collect(deep)
    assert res.status == "done" and res.critic is None and res.confidence.critic is None


@pytest.mark.parametrize(
    ("region", "code"),
    [("Columbus, OH", "OH"), ("Ohio", "OH"), ("georgia", "GA"), ("Paris", None), ("", None)],
)
def test_state_codes_for_the_lab_link(region, code):
    assert state_code(region) == code


# ---------------------------------------------------------------- claim check


def read_case(
    passage="Remove infected lower leaves. Apply copper fungicide at 2 tablespoons per gallon.",
):
    case = Case()
    src = case.add_source("https://extension.umn.edu/x", "UMN", "", True)
    src.passages = [passage]
    case.add_source("https://blog.example.com/x", "Blog", "", False)  # found, never read
    return case


def plan_of(*steps):
    return CarePlan(summary="s", steps=[PlanStep(action=a, source_ids=ids) for a, ids in steps])


def test_steps_must_cite_a_source_leaf_actually_read():
    plan = plan_of(
        ("Remove infected lower leaves.", [1]), ("Spray neem weekly.", [2]), ("Pray for sun.", [])
    )
    flags = code_checks(plan, read_case())
    assert [s.action for s in plan.steps] == ["Remove infected lower leaves."]
    assert [f.verdict for f in flags] == ["dropped", "dropped"]


def test_a_rate_not_in_the_cited_passage_is_dropped_and_one_that_is_stays():
    plan = plan_of(
        ("Mix 5 tablespoons per gallon of copper.", [1]),
        ("Apply copper fungicide at 2 tablespoons per gallon.", [1]),
    )
    flags = code_checks(plan, read_case())
    assert len(plan.steps) == 1 and "2 tablespoons" in plan.steps[0].action
    assert flags[0].verdict == "dropped" and "5 tbsp" in flags[0].reason


@pytest.mark.parametrize(
    ("step", "kept"),
    [
        ("Use 0.5 ml per litre", True),
        ("Use 5 ml per litre", False),  # 5 ml is not "0.5 ml"
        ("Use two tablespoons per gallon", True),
        ("Use 2 tbsp per gallon", True),
        ("Use a 1% solution", False),
        ("Use a 0.1 percent solution", True),
        ("Use three capfuls", False),
        ("Dilute 1:5", False),
        ("Dilute 1:10", True),
        ("Use 3 tablespoonfuls", False),
    ],
)
def test_amounts_are_compared_as_values_not_substrings(step, kept):
    passage = "Use 0.5 ml per litre, a 0.1% solution, or 2 tablespoons per gallon; bleach 1:10."
    plan = plan_of((step, [1]))
    code_checks(plan, read_case(passage))
    assert bool(plan.steps) is kept


@pytest.mark.parametrize(
    "text",
    [
        "Apply fungicides weekly",
        "Keep spraying",
        "Use insecticidal soap",
        "Dust with sulfur",
        "Try baking soda",
        "Use hydrogen peroxide",
    ],
)
def test_chemical_wording_variants_get_the_label_reminder(text):
    plan = plan_of((text, [1]))
    code_checks(plan, read_case(f"{text}."))
    assert plan.steps[0].action.endswith("Follow the product label.")


def test_chemical_steps_always_say_follow_the_label():
    plan = plan_of(("Apply copper fungicide at 2 tablespoons per gallon", [1]))
    flags = code_checks(plan, read_case())
    assert plan.steps[0].action.endswith("Follow the product label.")
    assert flags[-1].verdict == "label_added"


async def test_the_judge_drops_unsupported_steps_and_flags_partial_ones(settings):
    verdicts = {
        "verdicts": [
            {"step": 1, "verdict": "supported"},
            {"step": 2, "verdict": "partial", "note": "The source says lower leaves only."},
            {"step": 3, "verdict": "unsupported", "note": "Not in the source."},
        ]
    }
    leaf, _, _ = make_leaf(settings, {"claim_check": verdicts})
    plan = plan_of(
        ("Remove infected lower leaves.", [1]),
        ("Remove all leaves.", [1]),
        ("Burn the garden.", [1]),
    )
    flags = await judge(leaf.llm, settings, plan, read_case(), [])
    assert [s.action for s in plan.steps] == ["Remove infected lower leaves.", "Remove all leaves."]
    assert plan.steps[1].flag == "The source says lower leaves only."
    assert [f.verdict for f in flags] == ["partial", "dropped"]


async def test_an_unavailable_judge_flags_steps_and_drops_chemical_ones(settings):
    leaf, _, _ = make_leaf(settings, {})
    plan = plan_of(
        ("Remove infected lower leaves.", [1]),
        ("Apply copper fungicide at 2 tablespoons per gallon.", [1]),
    )
    flags = await judge(leaf.llm, settings, plan, read_case(), [])
    assert [s.action for s in plan.steps] == ["Remove infected lower leaves."]
    assert plan.steps[0].flag.startswith("Not checked")
    assert [f.verdict for f in flags] == ["partial", "dropped"]


async def test_a_step_the_judge_skips_is_not_kept(settings):
    leaf, _, _ = make_leaf(
        settings, {"claim_check": {"verdicts": [{"step": 1, "verdict": "supported"}]}}
    )
    plan = plan_of(("Remove infected lower leaves.", [1]), ("Remove all leaves.", [1]))
    await judge(leaf.llm, settings, plan, read_case(), [])
    assert [s.action for s in plan.steps] == ["Remove infected lower leaves."]


def test_a_summary_with_an_unsourced_amount_is_replaced():
    from backend.claims import check_summary

    plan = CarePlan(summary="Spray 5 ml per litre weekly.", steps=[])
    flags = check_summary(plan, "Use 0.5 ml per litre.")
    assert "5 ml" not in plan.summary and flags[0].verdict == "dropped"


def test_blend_never_merges_a_broad_name_into_specific_diseases():
    from backend.critic import blend

    first = [
        Hypothesis(name="Early blight", probability=0.5),
        Hypothesis(name="Late blight", probability=0.35),
    ]
    out = blend(first, [Hypothesis(name="Blight", probability=0.6)], 0.5)
    assert {h.name for h in out} == {"Early blight", "Late blight", "Blight"}


def test_a_disagreeing_second_opinion_escalates_even_without_naming_an_alternative():
    silent = CriticOpinion(
        alternative="", alternative_probability=0.0, leading_probability=0.4, agrees=False
    )
    case = case_with(("Early blight", 0.8))
    conf = combine(case, silent)
    d = deep_decision(case, conf, silent)
    assert conf.disagreement and d.verdict == "lab" and "disagree" in d.reason
    assert CriticOpinion().agrees is False  # a reply without a vote counts as disagreement


def test_a_mild_disagreement_lowers_confidence_but_does_not_force_a_lab():
    # Fitted rule: the critic escalates on its own only when it gives the leader under 50%.
    mild = CriticOpinion(
        alternative="Septoria", alternative_probability=0.3, leading_probability=0.65, agrees=False
    )
    case = case_with(("Early blight", 0.8))
    conf = combine(case, mild)
    assert conf.disagreement and conf.value == 0.65
    d = deep_decision(case, conf, mild)
    assert d.verdict == "likely" and "leans toward Septoria" in d.reason


def test_deep_sends_widely_spread_causes_to_a_lab():
    from backend.verdict import DEEP

    agree = opinion(lead=0.3)
    case = case_with(("Early blight", 0.35))
    conf = combine(case, agree)
    d = deep_decision(case, conf, agree)
    assert conf.value < DEEP.lab_below and d.verdict == "lab" and "spread out" in d.reason


def test_the_second_opinions_alternative_never_outranks_the_leader():
    case = case_with(("Early blight", 0.5), ("Septoria", 0.2))
    add_alternative(case, opinion(agrees=False, alt="Late blight", alt_p=0.8))
    assert case.differential[0].name == "Early blight"
    assert case.differential[1].name == "Late blight (second opinion)"
    assert case.differential[1].probability <= case.differential[0].probability


async def test_a_plan_with_no_surviving_steps_escalates(settings):
    unsupported = {"verdicts": [{"step": i, "verdict": "unsupported"} for i in range(1, 9)]}
    deep, _, _ = deep_for(
        settings,
        [turn(search(), finish(("Early blight", 0.85, ["E1"])))],
        responses={**RESP, "claim_check": unsupported},
    )
    res, _ = await collect(deep)
    assert not res.plan.steps and res.plan.escalate


async def test_leaf_reads_trusted_sources_itself_when_the_investigator_read_none():
    case = Case()
    case.differential = [Hypothesis(name="Early blight", probability=0.8)]
    case.add_source("https://blog.example.com/x", "Blog", "", False)
    case.add_source("https://extension.umn.edu/x", "UMN", "", True)
    searcher = FakeSearcher()
    await ensure_passages(case, searcher)
    assert searcher.extracted[0] == "https://extension.umn.edu/x"  # trusted first
    assert case.sources[1].passages and case.tavily_credits == 2
    before = list(searcher.extracted)
    await ensure_passages(case, searcher)  # already has passages: no more reads
    assert searcher.extracted == before


def test_the_alternative_is_not_added_twice_under_another_spelling():
    case = case_with(
        ("Septoria leaf spot", 0.7), ("Bacterial spot (Xanthomonas/Pseudomonas)", 0.05)
    )
    add_alternative(
        case, opinion(agrees=False, alt="Bacterial spot (Xanthomonas spp.)", alt_p=0.38)
    )
    assert len(case.differential) == 2


def test_blend_combines_both_views_by_disease_name():
    from backend.critic import blend

    first = [
        Hypothesis(name="Early blight (Alternaria solani)", probability=0.8),
        Hypothesis(name="Septoria leaf spot", probability=0.1),
    ]
    final = [
        Hypothesis(
            name="Septoria leaf spot (Septoria lycopersici)", probability=0.6, evidence_for=["E1"]
        ),
        Hypothesis(name="Early blight", probability=0.3),
    ]
    out = blend(first, final, 0.4)
    assert [h.name for h in out][0].startswith("Early blight")  # 0.6*0.3+0.4*0.8 = 0.50 vs 0.40
    assert out[0].probability == 0.5 and out[1].probability == 0.4
    assert out[1].evidence_for == ["E1"]  # evidence ids come from the investigator


def test_blend_weight_zero_is_the_investigator_alone():
    from backend.critic import blend

    final = [Hypothesis(name="Rust", probability=0.7)]
    assert blend([Hypothesis(name="Scab", probability=0.9)], final, 0.0)[0].name == "Rust"
    assert blend([], final, 0.4)[0].name == "Rust"


async def test_the_final_ranking_blends_the_first_look_with_the_investigation():
    from backend.config import Settings

    blended = Settings(nebius_api_key="t", vision_model="nvidia/fake-vl", deep_blend_first=0.7)
    # first differential (DIAG_HIGH): Early blight 0.82, Septoria 0.1
    deep, _, _ = deep_for(
        blended,
        [turn(search(), finish(("Septoria leaf spot", 0.6, ["E1"]), ("Early blight", 0.3, [])))],
    )
    res, _ = await collect(deep)
    assert res.investigator_differential[0].name == "Septoria leaf spot"
    assert res.differential[0].name == "Early blight"  # 0.3*0.3 + 0.7*0.82 = 0.66 vs 0.25
    assert res.differential[0].probability == pytest.approx(0.664, abs=0.01)
