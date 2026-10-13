"""Canonical condition names: the synonym map the eval scores against, and high-stakes flags."""

from __future__ import annotations

import pytest

from backend.diseases import BY_ID, CONDITIONS, high_stakes, identify


@pytest.mark.parametrize(
    ("name", "cid"),
    [
        ("Early blight (Alternaria solani)", "early_blight"),
        ("Alternaria linariae", "early_blight"),
        ("Late blight (Phytophthora infestans)", "late_blight"),
        ("Septoria leaf spot (Septoria lycopersici)", "septoria"),
        ("Rose rust (Phragmidium mucronatum)", "rust"),
        ("Bean rust (Uromyces appendiculatus)", "rust"),
        ("Black spot (Diplocarpon rosae)", "black_spot"),
        ("Apple scab (Venturia inaequalis)", "apple_scab"),
        ("Bacterial leaf spot (Xanthomonas campestris pv. vesicatoria)", "bacterial_spot"),
        ("Magnesium (Mg) deficiency in corn", "magnesium_deficiency"),
        ("Cucumber mosaic virus (CMV)", "mosaic_virus"),
        ("Powdery mildew (Podosphaera xanthii)", "powdery_mildew"),
        ("Cucurbit downy mildew (Pseudoperonospora cubensis)", "downy_mildew"),
        ("Edema", "edema"),
        ("Oedema from overwatering", "edema"),
        ("Root rot (Pythium)", "overwatering"),
        ("Tomato brown rugose fruit virus (ToBRFV)", "tobrfv"),
        ("Huanglongbing (citrus greening)", "citrus_greening"),
        ("Spotted lanternfly feeding", "spotted_lanternfly"),
    ],
)
def test_names_map_to_their_condition(name, cid):
    assert identify(name) == cid


@pytest.mark.parametrize(
    "name",
    [
        "Bacterial speck (Pseudomonas syringae pv. tomato)",  # not bacterial spot
        "Gray leaf spot (Stemphylium solani)",  # not early blight
        "Target spot (Corynespora cassiicola)",  # not early blight
        "Tomato russet mites",  # a pest, not rust
        "Rust mite damage",
        "Common scab (Streptomyces scabies)",  # potatoes, not apple scab
        "Northern corn leaf blight",
        "",
    ],
)
def test_look_alike_names_are_not_matched(name):
    assert identify(name) is None


def test_first_named_condition_wins():
    assert identify("Early blight or possibly late blight") == "early_blight"
    assert identify("Late blight, not early blight") == "late_blight"


def test_whole_words_only():
    assert identify("Ironically healthy") is None  # "iron" inside a word
    assert identify("Frustrated grower") is None  # "rust" inside a word


@pytest.mark.parametrize(
    "name",
    [
        "Late blight",
        "ToBRFV",
        "Sudden oak death (Phytophthora ramorum)",
        "Boxwood blight",
        "Box tree moth",
    ],
)
def test_high_stakes_conditions_say_why_and_cite(name):
    cond = high_stakes(name)
    assert cond is not None
    assert cond.high_stakes and cond.sources


def test_ordinary_conditions_are_not_high_stakes():
    for name in ("Early blight", "Powdery mildew", "Magnesium deficiency", "Aphids"):
        assert high_stakes(name) is None


def test_table_is_well_formed():
    assert len(BY_ID) == len(CONDITIONS)  # unique ids
    for c in CONDITIONS:
        assert c.kind in {"disease", "abiotic", "pest"}
        assert c.aliases
        assert identify(c.name) == c.id, c.name  # every display name maps to itself
