"""Where every number in the climate engine comes from.

Each coefficient or threshold names one of these sources. Values that no source supports are
marked HEURISTIC and say so wherever they are shown; they are Leaf's own assumptions.
Sources were fetched and read on 2026-10-15.
"""

from __future__ import annotations

from typing import TypedDict


class Source(TypedDict):
    title: str
    url: str


SOURCES: dict[str, Source] = {
    "fao56_t12": {
        "title": "FAO Irrigation and Drainage Paper 56 (Allen et al., 1998), Table 12: single "
        "crop coefficients",
        "url": "https://www.fao.org/4/x0490e/x0490e0b.htm",
    },
    "fao56_t19": {
        "title": "FAO-56, Table 19: typical soil water characteristics",
        "url": "https://www.fao.org/4/x0490e/x0490e0c.htm",
    },
    "fao56_ch8": {
        "title": "FAO-56, Chapter 8: root zone depletion (Eq. 82-88), Table 22 (rooting depth, "
        "depletion fraction p), light rain below 0.2 ETo ignored, Examples 36 and 38",
        "url": "https://www.fao.org/4/x0490e/x0490e0e.htm",
    },
    "openmeteo_et0": {
        "title": "Open-Meteo: FAO-56 Penman-Monteith reference evapotranspiration (ET0)",
        "url": "https://open-meteo.com/en/docs",
    },
    "usda_phzm": {
        "title": "USDA Plant Hardiness Zone Map (2023): average annual extreme minimum "
        "temperature, 1991-2020; 10 F zones, 5 F half zones a/b",
        "url": "https://planthardiness.ars.usda.gov/pages/how-to-use-the-maps",
    },
    "usda_phzm_method": {
        "title": "USDA PHZM map creation: 30 years, 1991-2020",
        "url": "https://planthardiness.ars.usda.gov/pages/map-creation",
    },
    "ncei_freeze": {
        "title": "NC State Extension: interpreting NCEI freeze/frost probabilities "
        "(32 F freeze, 28 F hard freeze, 10/50/90% levels)",
        "url": "https://content.ces.ncsu.edu/interpreting-freezefrost-probabilities-from-the-"
        "national-centers-for-environmental-information",
    },
    "nws_frost": {
        "title": "NWS Baltimore/Washington: Frost Advisory when lows of 33-36 F are forecast on "
        "clear, calm nights",
        "url": "https://www.weather.gov/lwx/warningsdefined",
    },
    "rutgers_heat": {
        "title": "Rutgers Plant & Pest Advisory: days over 90 F and nights over 70 F reduce "
        "tomato fruit set",
        "url": "https://plant-pest-advisory.rutgers.edu/high-temperatures-and-effect-on-tomato-"
        "pollination-and-fruit-set/",
    },
    "nws_wind": {
        "title": "NWS Indianapolis criteria: Wind Advisory at sustained 30 mph and/or gusts "
        "45 mph (criteria are local)",
        "url": "https://www.weather.gov/ind/criteria",
    },
    "osu_germination": {
        "title": "Oregon State University Extension (after J.F. Harrington): soil temperature "
        "conditions for vegetable seed germination",
        "url": "https://extension.oregonstate.edu/gardening/soil-compost/soil-temperature-"
        "conditions-vegetable-seed-germination",
    },
    "ucanr_germination": {
        "title": "UC Master Gardener Program: seed germination temperature and timing",
        "url": "https://ucanr.edu/program/uc-master-gardener-program/seed-germination-"
        "temperature-and-timing",
    },
    "mrcc_gdd": {
        "title": "Midwestern Regional Climate Center: growing degree day base temperatures",
        "url": "https://mrcc.purdue.edu/resources/growing-degree-day-description",
    },
    "usbr_gdd": {
        "title": "USBR AgriMet: growing degree days, single average with a horizontal cutoff",
        "url": "https://www.usbr.gov/pn/agrimet/gdd.html",
    },
    "nws_api": {
        "title": "National Weather Service API: active alerts for a point",
        "url": "https://www.weather.gov/documentation/services-web-api",
    },
    "ucanr_mix": {
        "title": "UC ANR: soil mixes should hold at least 40% water by volume",
        "url": "https://ucanr.edu/blog/nursery-and-flower-grower/article/soil-mixes-part-3-how-"
        "much-air-and-water",
    },
    "HEURISTIC": {
        "title": "Leaf heuristic: no published source; an assumption shown as such",
        "url": "",
    },
}


def cite(*ids: str) -> list[Source]:
    return [SOURCES[i] for i in ids]
