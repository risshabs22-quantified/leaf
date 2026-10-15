"""The plant-ID eval set and its scorer (the live run is scripts/eval_plant_id.py)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from scripts.eval_plant_id import CASES, load_cases, score_case, summarise

ROOT = Path(__file__).resolve().parent.parent


def test_forty_photos_of_forty_species_each_with_a_licence_row():
    cases = load_cases(CASES)
    assert len(cases) == 40 and len({c["scientific_name"] for c in cases}) == 40
    readme = (ROOT / "evals" / "README.md").read_text()
    for c in cases:
        path = ROOT / "evals" / "images" / c["file"]
        with Image.open(path) as img:
            assert max(img.size) <= 1600
        assert f"`{path.name}`" in readme, path.name  # licence, author and source listed


def test_synonyms_and_genus_level_scoring():
    case = {"scientific_name": "Dracaena trifasciata", "synonyms": ["Sansevieria trifasciata"]}
    assert score_case(case, ["sansevieria trifasciata"])["species_top1"]
    s = score_case(case, ["dracaena reflexa", "dracaena trifasciata"])
    assert not s["species_top1"] and s["species_top3"] and s["genus_top1"]
    assert score_case(case, []) == {
        "species_top1": False,
        "species_top3": False,
        "genus_top1": False,
    }


def test_summary_counts_what_the_check_fixed_and_broke():
    rows = [
        {
            "file": "a",
            "vision": {"species_top1": False, "species_top3": True, "genus_top1": True},
            "checked": {"species_top1": True, "species_top3": True, "genus_top1": True},
            "confident": True,
            "seconds": 8,
            "cost_usd": 0.003,
        },
        {
            "file": "b",
            "vision": {"species_top1": True, "species_top3": True, "genus_top1": True},
            "checked": {"species_top1": False, "species_top3": True, "genus_top1": True},
            "confident": False,
            "seconds": 10,
            "cost_usd": 0.003,
        },
    ]
    s = summarise(rows)
    assert s["check_fixed"] == ["a"] and s["check_broke"] == ["b"]
    assert s["checked"]["species_top1"] == 0.5 and s["confident_right"] == 1
