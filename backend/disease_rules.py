"""Published disease-pressure rules, computed from hourly weather. Pure functions, no I/O.

Every threshold here comes from the cited source; nothing is invented. Leaf has no leaf-wetness
sensor, so "wet" means an hour with relative humidity >= 90%, the best of the simple proxies
tested by Sentelhas et al. 2008 (Agric. For. Meteorol. 148:392-400; fraction of hours correct
0.87-0.92 across four sites): https://iris.cnr.it/handle/20.500.14243/14687
Gridded (modelled) humidity tends to undercount wet hours and under-trigger these models
(Gleason et al. 2008, Sci. Agric.): https://www.scielo.br/j/sa/a/7VPNDRtBgJFYYGJRPyfVMSM/
A "day" is the local calendar day; the published sources do not fix a boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

WET_RH = 90.0


@dataclass(frozen=True)
class Hour:
    time: datetime  # local time
    temp_c: float
    rh: float
    precip_mm: float = 0.0


@dataclass(frozen=True)
class Day:
    day: date
    hours: tuple[Hour, ...]

    @property
    def wet_hours(self) -> int:
        return sum(1 for h in self.hours if h.rh >= WET_RH)

    @property
    def tmin(self) -> float:
        return min(h.temp_c for h in self.hours)

    @property
    def tmax(self) -> float:
        return max(h.temp_c for h in self.hours)

    @property
    def tmean(self) -> float:
        return sum(h.temp_c for h in self.hours) / len(self.hours)

    @property
    def rain_mm(self) -> float:
        return round(sum(h.precip_mm for h in self.hours), 1)


def group_days(hours: list[Hour]) -> list[Day]:
    """Complete days only (24 hourly values), oldest first."""
    by_day: dict[date, list[Hour]] = {}
    for h in hours:
        by_day.setdefault(h.time.date(), []).append(h)
    return [Day(d, tuple(hs)) for d, hs in sorted(by_day.items()) if len(hs) == 24]


# ----------------------------------------------------------------------- late blight (potato)


def _two_day_criteria(days: list[Day], hours_needed: int) -> list[date]:
    """Second days of qualifying pairs: two consecutive days, each with minimum temperature
    >= 10 C and at least `hours_needed` hours at RH >= 90%. Whether those hours must be
    consecutive is not stated in the source; they are counted, as in the Met Office inputs
    ("number of hours of RH >= 90%")."""

    def ok(d: Day) -> bool:
        return d.tmin >= 10.0 and d.wet_hours >= hours_needed

    return [
        b.day
        for a, b in zip(days, days[1:], strict=False)
        if (b.day - a.day).days == 1 and ok(a) and ok(b)
    ]


def smith_periods(days: list[Day]) -> list[date]:
    """Smith period (potato late blight, Phytophthora infestans): 11 h at RH >= 90%.
    Dancey, Skelsey & Cooke 2017, PAGV Special Report 18, pp. 53-58:
    https://edepot.wur.nl/448972"""
    return _two_day_criteria(days, 11)


def hutton_periods(days: list[Day]) -> list[date]:
    """Hutton criteria (potato late blight; replaced Smith in the UK in 2017): 6 h at
    RH >= 90%. UK-calibrated. Same source as smith_periods, and AHDB:
    https://potatoes.ahdb.org.uk/development-and-implementation-of-a-new-national-warning-system-for-potato-late-blight-in-great-britain-hutton-criteria"""
    return _two_day_criteria(days, 6)


# Wallin severity values as published by the University of Maine (Bulletin #2418, table of
# continuous hours at RH >= 90% vs mean temperature in F over that period). For each band:
# the lowest hours giving severity 1, 2, 3, 4, and the formula offset for longer periods.
# https://extension.umaine.edu/publications/wp-content/uploads/sites/52/2020/03/2418_updated2020.pdf
WALLIN_BANDS_F = [
    (45.0, 54.0, (16, 19, 22, 25), 27, 4),  # table ends at 27 h; longer: (h-1)//3 - 4
    (55.0, 59.0, (13, 16, 19, 22), 24, 3),  # longer than 24 h: (h-1)//3 - 3
    (60.0, 81.0, (10, 13, 16, 19), 21, 2),  # longer than 21 h: (h-1)//3 - 2
]
WALLIN_FIRST_SPRAY = 18  # accumulated values after 50% emergence; blight expected 7-10 days later


def wallin_value(hours: int, mean_temp_f: float) -> int:
    """Severity value of one continuous wet period. The bulletin's table jumps from "<15" to
    "16" (and "<12" to "13", "<9" to "10"); those gap hours are treated as 0. Outside
    45-81 F the value is 0 (bulletin: "0 at 40 F or 85 F"). Rounding of the long-period
    formula is not specified; it is floored."""
    for lo, hi, mins, table_max, offset in WALLIN_BANDS_F:
        # Bands are whole degrees in the bulletin; the upper edge of each band runs to just
        # below the next band so no temperature falls between them.
        if lo <= mean_temp_f < hi + 1:
            if hours > table_max:
                return max(0, (hours - 1) // 3 - offset)
            return sum(1 for m in mins if hours >= m)
    return 0


def wallin_periods(hours: list[Hour]) -> list[tuple[datetime, int, int]]:
    """Continuous runs of RH >= 90%: (end time, length in hours, severity value)."""
    runs, current = [], []
    for h in hours + [Hour(datetime.max, 0.0, 0.0)]:  # sentinel closes the last run
        if h.rh >= WET_RH:
            current.append(h)
            continue
        if current:
            mean_f = sum(x.temp_c for x in current) / len(current) * 9 / 5 + 32
            runs.append((current[-1].time, len(current), wallin_value(len(current), mean_f)))
            current = []
    return runs


# ----------------------------------------------------------------------- TOMCAST (tomato)

# Daily disease severity values (DSV) from wet hours and the mean temperature during the wet
# hours (Madden et al. 1978 as adapted for TOMCAST, Pitblado 1992), reproduced by UC IPM:
# https://ipm.ucanr.edu/DISEASE/DATABASE/dsvtable.html
# Bands in C; the source's F labels imply the cut points used here. Lowest wet hours giving
# DSV 1, 2, 3, 4 (None: not reachable in that band).
TOMCAST_BANDS_C = [
    (13.0, 17.5, (7, 16, 21, None)),
    (17.5, 20.5, (4, 9, 16, 23)),
    (20.5, 25.5, (3, 6, 13, 21)),
    (25.5, 29.4, (4, 9, 16, 23)),
]
# Spray-timing advisories, not diagnoses: UConn uses 15 accumulated DSV as standard
# (https://ipm.cahnr.uconn.edu/?p=1983); Rutgers uses 15-25 by programme
# (https://plant-pest-advisory.rutgers.edu/ipm-tomato-report-2013-word-doc-test/).
TOMCAST_ADVISORY = 15


def tomcast_dsv(wet_hours: int, wet_mean_temp_c: float | None) -> int:
    if not wet_hours or wet_mean_temp_c is None:
        return 0
    for lo, hi, mins in TOMCAST_BANDS_C:
        if lo <= wet_mean_temp_c < hi or (hi == 29.4 and wet_mean_temp_c == hi):
            return sum(1 for m in mins if m is not None and wet_hours >= m)
    return 0  # outside 13-29.4 C the table has no row


def tomcast_day(day: Day) -> int:
    wet = [h for h in day.hours if h.rh >= WET_RH]
    mean = sum(h.temp_c for h in wet) / len(wet) if wet else None
    return tomcast_dsv(len(wet), mean)


# ----------------------------------------------------------------------- growing degree days


def gdd(day: Day, base_c: float = 10.0, cap_c: float = 30.0) -> float:
    """Single average with a horizontal cutoff: ((min(Tmax, cap) + Tmin) / 2) - base, floored
    at 0. USBR AgriMet: https://www.usbr.gov/pn/agrimet/gdd.html"""
    return max(0.0, (min(day.tmax, cap_c) + day.tmin) / 2 - base_c)
