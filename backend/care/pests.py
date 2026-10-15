"""Pests whose scouting Leaf times by degree days. Each threshold is published by the source
named on it (fetched 2026-10-15); filled from the researcher's table."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pest:
    key: str
    name: str
    host_crops: tuple[str, ...]  # keys of backend/climate/crops.CROPS
    gdd_f: float
    base_f: float
    start: str  # what the degree days count from, e.g. "January 1"
    event: str
    look_for: str
    source_title: str
    source_url: str

    @property
    def base_c(self) -> float:
        return round((self.base_f - 32) * 5 / 9, 1)


WISC_DD = (
    "https://hort.extension.wisc.edu/articles/degree-days-common-fruit-vegetable-insect-pests/"
)
JAN1_ASSUMED = "January 1 (Leaf's assumption: the source gives no start date)"

# Left out on purpose: Colorado potato beetle and codling moth need a biofix (first egg mass,
# first moth catch) Leaf cannot observe; seedcorn maggot and flea beetle have no stated hosts;
# no degree-day figure was found for Mexican bean beetle.
PESTS: tuple[Pest, ...] = (
    Pest(
        "japanese_beetle",
        "Japanese beetle",
        ("grape", "bean"),
        1030,
        50,
        "January 1",
        "adults start emerging",
        "Look for metallic green beetles skeletonising leaves; "
        "hand-pick them into soapy water in the morning.",
        "Iowa State University Extension: Japanese beetle adults now emerging",
        "https://crops.extension.iastate.edu/cropnews/2023/06/japanese-beetle-adults-now-emerging",
    ),
    Pest(
        "squash_vine_borer",
        "squash vine borer",
        ("squash", "pumpkin"),
        900,
        50,
        JAN1_ASSUMED,
        "moths start emerging (egg laying peaks near 1,000)",
        "Look for orange-and-black moths by day and sawdust-like frass at the base of stems.",
        "University of Wisconsin Extension: squash vine borer",
        "https://hort.extension.wisc.edu/articles/squash-vine-borer/",
    ),
    Pest(
        "cabbage_maggot",
        "cabbage maggot",
        ("cabbage", "broccoli", "radish"),
        300,
        43,
        JAN1_ASSUMED + "; the source counts from when the ground thaws",
        "the first generation of flies appears",
        "Check for wilting seedlings and white "
        "maggots on the roots; row covers keep the flies off.",
        "University of Wisconsin Vegetable Entomology: cabbage maggot",
        "https://vegento.russell.wisc.edu/pests/cabbage-maggot/",
    ),
    Pest(
        "onion_maggot",
        "onion maggot",
        ("onion",),
        680,
        40,
        JAN1_ASSUMED,
        "the first generation of flies appears",
        "Look for yellowing, collapsing onion seedlings and maggots in the bulbs.",
        "University of Wisconsin Extension: degree days for common fruit and vegetable pests",
        WISC_DD,
    ),
    Pest(
        "european_corn_borer",
        "European corn borer",
        ("sweet_corn",),
        375,
        50,
        JAN1_ASSUMED,
        "the first moths fly",
        "Look for pinholes and frass in the whorl leaves and tassels.",
        "Cornell University (NYS IPM) sweet corn trap network",
        "https://sweetcorn.nysipm.cornell.edu/2018/05/29/ny-sweet-corn-trap-network-report-5-29-18",
    ),
    Pest(
        "squash_bug",
        "squash bug",
        ("squash", "pumpkin"),
        193,
        58,
        JAN1_ASSUMED,
        "eggs start to appear",
        "Look under leaves for clusters of bronze eggs and grey nymphs; crush the eggs.",
        "University of Wisconsin Vegetable Entomology: squash bug",
        "https://vegento.russell.wisc.edu/pests/squash-bug",
    ),
)
