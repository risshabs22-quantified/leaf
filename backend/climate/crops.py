"""Crop coefficients for the climate engine, each with its source (backend/climate/sources.py).

Water: FAO-56 Table 12 (Kc ini, mid, end) and Table 22 (effective rooting depth Zr, depletion
fraction p at ETc of about 5 mm/day). Where FAO prints a Kc range, the midpoint is used and
the range is kept; for Zr the water balance uses the smaller value, as FAO-56's Table 22 note
advises for irrigation scheduling. Kc ini printed once per crop group applies to the
whole group (FAO's note).
Germination: minimum and optimum soil temperature, OSU Extension (Harrington); cabbage's
optimum and broccoli from UC ANR because the OSU table has a typo and no broccoli.
Degree days: crop base temperatures from MRCC; others use the general 10 C (50 F) base.
The knowledge graph (M3) adds species from cited sources; these are the starting set.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


@dataclass(frozen=True)
class Crop:
    key: str
    names: tuple[str, ...]
    kc: tuple[float, float, float] | None = None  # ini, mid, end
    kc_end_range: tuple[float, float] | None = None
    zr: tuple[float, float] | None = None  # metres, FAO-56 Table 22
    p: float | None = None
    germ_min_c: float | None = None
    germ_opt_c: tuple[float, float] | None = None
    gdd_base_c: float | None = None
    sources: tuple[str, ...] = field(default=())


def _germ(min_f: float, lo_f: float, hi_f: float) -> dict:
    return {"germ_min_c": f_to_c(min_f), "germ_opt_c": (f_to_c(lo_f), f_to_c(hi_f))}


WATER = ("fao56_t12", "fao56_ch8")
GERM = ("osu_germination",)

CROPS: dict[str, Crop] = {
    c.key: c
    for c in [
        Crop(
            "tomato",
            ("tomato", "solanum lycopersicum"),
            (0.6, 1.15, 0.80),
            (0.70, 0.90),
            (0.7, 1.5),
            0.40,
            **_germ(50, 60, 85),
            gdd_base_c=10.0,
            sources=(*WATER, *GERM, "mrcc_gdd"),
        ),
        Crop(
            "pepper",
            ("pepper", "sweet pepper", "bell pepper", "chili", "chilli", "capsicum"),
            (0.6, 1.05, 0.90),
            None,
            (0.5, 1.0),
            0.30,
            **_germ(60, 65, 95),
            sources=(*WATER, *GERM),
        ),
        Crop(
            "lettuce",
            ("lettuce", "lactuca sativa"),
            (0.7, 1.00, 0.95),
            None,
            (0.3, 0.5),
            0.30,
            **_germ(35, 40, 80),
            sources=(*WATER, *GERM),
        ),
        Crop(
            "cabbage",
            ("cabbage", "brassica oleracea capitata"),
            (0.7, 1.05, 0.95),
            None,
            (0.5, 0.8),
            0.45,
            germ_min_c=f_to_c(40),
            germ_opt_c=(f_to_c(60), f_to_c(85)),
            sources=(*WATER, *GERM, "ucanr_germination"),
        ),
        Crop(
            "carrot",
            ("carrot", "daucus carota"),
            (0.7, 1.05, 0.95),
            None,
            (0.5, 1.0),
            0.35,
            **_germ(40, 45, 85),
            sources=(*WATER, *GERM),
        ),
        Crop(
            "onion",
            ("onion", "allium cepa", "shallot"),
            (0.7, 1.05, 0.75),
            None,
            (0.3, 0.6),
            0.30,
            **_germ(35, 50, 95),
            gdd_base_c=f_to_c(35),
            sources=(*WATER, *GERM, "mrcc_gdd"),
        ),
        Crop(
            "potato",
            ("potato", "solanum tuberosum"),
            (0.5, 1.15, 0.75),
            None,
            (0.4, 0.6),
            0.35,
            sources=WATER,
        ),
        Crop(
            "bean",
            ("bean", "green bean", "snap bean", "runner bean", "phaseolus"),
            (0.5, 1.05, 0.90),
            None,
            (0.5, 0.7),
            0.45,
            **_germ(60, 60, 85),
            gdd_base_c=10.0,
            sources=(*WATER, *GERM, "mrcc_gdd"),
        ),
        Crop(
            "cucumber",
            ("cucumber", "cucumis sativus"),
            (0.6, 1.00, 0.75),
            None,
            (0.7, 1.2),
            0.50,
            **_germ(60, 60, 95),
            sources=(*WATER, *GERM),
        ),
        Crop(
            "squash",
            ("squash", "zucchini", "courgette", "marrow", "cucurbita pepo"),
            (0.5, 0.95, 0.75),
            None,
            (0.6, 1.0),
            0.50,
            **_germ(60, 70, 95),
            sources=(*WATER, *GERM),
        ),
        Crop(
            "sweet_corn",
            ("sweet corn", "corn", "maize", "zea mays"),
            (0.3, 1.15, 1.05),
            None,
            (0.8, 1.2),
            0.50,
            **_germ(50, 60, 95),
            gdd_base_c=10.0,
            sources=(*WATER, *GERM, "mrcc_gdd"),
        ),
        Crop(
            "strawberry",
            ("strawberry", "fragaria"),
            (0.40, 0.85, 0.75),
            None,
            (0.2, 0.3),
            0.20,
            sources=WATER,
        ),
        Crop(
            "spinach",
            ("spinach", "spinacia oleracea"),
            (0.7, 1.00, 0.95),
            None,
            (0.3, 0.5),
            0.20,
            **_germ(35, 45, 75),
            sources=(*WATER, *GERM),
        ),
        Crop(
            "broccoli",
            ("broccoli",),
            (0.7, 1.05, 0.95),
            None,
            (0.4, 0.6),
            0.45,
            germ_min_c=f_to_c(40),
            germ_opt_c=(f_to_c(60), f_to_c(85)),
            sources=(*WATER, "ucanr_germination"),
        ),
        Crop(
            "garlic",
            ("garlic", "allium sativum"),
            (0.7, 1.00, 0.70),
            None,
            (0.3, 0.5),
            0.30,
            sources=WATER,
        ),
        Crop(
            "pea",
            ("pea", "peas", "pisum sativum"),
            (0.5, 1.15, 1.10),
            None,
            (0.6, 1.0),
            0.35,
            **_germ(40, 40, 75),
            gdd_base_c=f_to_c(40),
            sources=(*WATER, *GERM, "mrcc_gdd"),
        ),
        Crop(
            "lawn_cool",
            ("lawn", "grass", "turf", "cool season grass"),
            (0.90, 0.95, 0.95),
            None,
            (0.5, 1.0),
            0.40,
            sources=WATER,
        ),
        Crop(
            "lawn_warm",
            ("warm season grass", "bermuda grass", "zoysia", "st augustine"),
            (0.80, 0.85, 0.85),
            None,
            (0.5, 1.0),
            0.50,
            sources=WATER,
        ),
        Crop(
            "grape",
            ("grape", "grapevine", "vitis"),
            (0.30, 0.85, 0.45),
            None,
            (1.0, 2.0),
            0.35,
            sources=WATER,
        ),
        Crop(
            "berry_bush",
            ("blueberry", "raspberry", "blackberry", "currant", "gooseberry", "berry bush"),
            (0.30, 1.05, 0.50),
            None,
            (0.6, 1.2),
            0.50,
            sources=WATER,
        ),
        # FAO-56: apples, cherries, pears, no ground cover, killing frost (values before leaf drop).
        Crop(
            "pome_stone_fruit",
            ("apple", "pear", "cherry", "malus", "pyrus", "prunus avium"),
            (0.45, 0.95, 0.70),
            None,
            (1.0, 2.0),
            0.50,
            sources=WATER,
        ),
        # FAO-56: citrus, 50% canopy, no ground cover.
        Crop(
            "citrus",
            ("citrus", "lemon", "lime", "orange", "mandarin", "grapefruit"),
            (0.65, 0.60, 0.65),
            None,
            (1.1, 1.5),
            0.50,
            sources=WATER,
        ),
        # Germination only (no FAO-56 values fetched for these yet).
        Crop("beet", ("beet", "beetroot", "beta vulgaris"), **_germ(40, 50, 85), sources=GERM),
        Crop("radish", ("radish", "raphanus"), **_germ(40, 45, 90), sources=GERM),
        Crop(
            "pumpkin",
            ("pumpkin", "winter squash", "cucurbita maxima"),
            **_germ(60, 70, 90),
            sources=GERM,
        ),
        Crop(
            "melon",
            ("melon", "muskmelon", "cantaloupe", "cucumis melo"),
            **_germ(60, 75, 95),
            sources=GERM,
        ),
    ]
}

# Frost class per crop, from CSU Extension's Vegetable Planting Guide (csu_planting, read
# 2026-10-15): "Hardy ... As early as 2-4 weeks before the date of the average last spring
# frost"; "Semi-Hardy ... 0-2 weeks before"; "Tender ... From seed: around the date of the
# average last spring frost"; "Very Tender ... Typically 2+ weeks after". Summer squash is
# tender, winter squash and pumpkins very tender; melon is the guide's cantaloupe and
# watermelon. Crops the guide does not list (strawberry, garlic, fruit, lawns) have none.
FROST_CLASS: dict[str, str] = {
    "tomato": "very_tender",
    "pepper": "very_tender",
    "pumpkin": "very_tender",
    "melon": "very_tender",
    "bean": "tender",
    "cucumber": "tender",
    "sweet_corn": "tender",
    "squash": "tender",
    "carrot": "half_hardy",
    "potato": "half_hardy",
    "lettuce": "hardy",
    "cabbage": "hardy",
    "onion": "hardy",
    "spinach": "hardy",
    "broccoli": "hardy",
    "pea": "hardy",
}

GENERAL_GDD_BASE_C = 10.0  # 50 F, the general base (NCEI / MRCC)


def find_crop_exact(name: str) -> Crop | None:
    """The crop whose alias is exactly this name (or its plural): "pea" yes, "sweet pea" no."""
    text = " ".join((name or "").lower().split())
    for crop in CROPS.values():
        for alias in crop.names:
            plural = alias[:-1] + "ies" if alias.endswith("y") else alias + "s"
            if text in (alias, plural, alias + "es"):
                return crop
    return None


def find_crop(*names: str) -> Crop | None:
    """The crop for a plant's species or common name ("Solanum lycopersicum", "cherry tomato").

    English puts the head noun last, so the match that ends latest wins ("cherry tomato" is a
    tomato, "apple tree" an apple); then the longest ("winter squash" is a pumpkin)."""
    text = " " + " ".join(n.lower().strip() for n in names if n) + " "
    if not text.strip():
        return None
    best: tuple[int, int, Crop] | None = None
    for crop in CROPS.values():
        for alias in crop.names:
            plurals = (alias + "s", alias + "es", alias[:-1] + "ies" if alias.endswith("y") else "")
            for form in filter(None, (alias, *plurals)):
                at = text.rfind(f" {form} ")
                if at < 0:
                    continue
                score = (at + len(form), len(alias))
                if best is None or score > best[:2]:
                    best = (*score, crop)
    return best[2] if best else None
