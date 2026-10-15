"""Soil moisture from a sensor (Home Assistant) replaces the water balance for its plant.

A reading is the gardener's own data. The only rule Leaf adds is the dry mark: below it the
plant is watered, at or above it the plant is left alone, whatever the weather estimate says.
The default is 30 percent, Leaf's own assumption (sensors differ, so a sender may give its
own `dry_below`). A reading counts for 12 hours; after that the weather estimate takes over
again. A reading sent as a test is labelled TEST wherever it is used.
"""

from __future__ import annotations

from backend.climate.water import WaterAdvice
from backend.data.repo import Reading

FRESH_S = 12 * 3600
DEFAULT_DRY_BELOW = 30.0
SOURCE = {
    "title": "Your soil moisture sensor. The dry mark is Leaf's own default (30 %) unless the "
    "sender gives one",
    "url": "",
}
TEST_SOURCE = {
    "title": "A TEST soil moisture reading, not from a sensor. The dry mark is Leaf's own "
    "default (30 %) unless the sender gives one",
    "url": "",
}


def is_fresh(reading: Reading | None, now_ts: float) -> bool:
    return reading is not None and now_ts - reading.created_at <= FRESH_S


def sensor_advice(reading: Reading) -> WaterAdvice:
    """Water or skip from one reading."""
    mark = reading.dry_below if reading.dry_below is not None else DEFAULT_DRY_BELOW
    dry = reading.value < mark
    label = "TEST reading" if reading.label == "test" else "Sensor reading"
    where = "below" if dry else "at or above"
    return WaterAdvice(
        model="sensor",
        action="water" if dry else "skip",
        reason=(
            f"{label}: soil moisture {reading.value:g} %, {where} the dry mark of {mark:g} %. "
            "This replaces Leaf's weather estimate for this plant."
        ),
        numbers={"soil_moisture_pct": reading.value, "dry_below_pct": mark},
        assumptions=[
            "The dry mark is Leaf's own default unless the sender gave one; sensors differ."
        ],
        sources=[TEST_SOURCE if reading.label == "test" else SOURCE],
    )
