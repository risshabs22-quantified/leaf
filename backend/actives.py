"""Active ingredients Leaf recognises in care-plan steps, checked in code.

The care-plan writer is asked to respect the gardener's preferences (organic only, pets or
children in the garden), but a prompt is not a guarantee. This table is: every plan step is
scanned for the products below, labelled (organic or not, risk to pets, risk to bees), and a
step that breaks a preference is removed with the reason shown to the gardener.

The labels are deliberately coarse and conservative. "Organic" means the active ingredient
is generally allowed under the USDA National Organic Program (7 CFR 205.601); individual
products still need an OMRI or WSDA listing. Pet and bee notes summarise common extension
and label guidance. Every step that names a product also tells the gardener to follow the
label, which is the legal and safety authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.schemas import CarePlan, Garden

PET_LOW, PET_CAUTION, PET_HIGH = "low", "caution", "high"


@dataclass(frozen=True)
class Active:
    name: str
    pattern: str  # regex, matched case-insensitively on word boundaries
    organic: bool
    pet: str  # low | caution | high
    bees: bool  # harmful to bees on contact or as residue
    pet_note: str = ""
    oil: bool = False  # an oil: leaf burn in heat, and with sulfur
    sulfur: bool = False


ACTIVES: tuple[Active, ...] = (
    # Organic-allowed (as active ingredients; check the product's listing)
    Active(
        "copper",
        r"copper|bordeaux",
        True,
        PET_CAUTION,
        False,
        "Harmful if pets eat treated leaves or the concentrate; keep them off until it dries.",
    ),
    Active(
        "sulfur",
        r"sulfur|sulphur",
        True,
        PET_CAUTION,
        False,
        "Irritates eyes and skin; keep pets off until it dries.",
        sulfur=True,
    ),
    Active("neem oil", r"neem|azadirachtin", True, PET_LOW, False, oil=True),
    Active(
        "horticultural oil",
        r"horticultural oil|dormant oil|paraffinic oil|mineral oil|summer oil",
        True,
        PET_LOW,
        False,
        oil=True,
    ),
    Active("potassium bicarbonate", r"potassium bicarbonate", True, PET_LOW, False),
    Active("baking soda", r"baking soda|sodium bicarbonate", True, PET_LOW, False),
    Active(
        "Bacillus subtilis",
        r"bacillus (?:subtilis|amyloliquefaciens)|serenade",
        True,
        PET_LOW,
        False,
    ),
    Active("Bt", r"bacillus thuringiensis|\bbt\b|\bbtk\b", True, PET_LOW, False),
    Active(
        "insecticidal soap",
        r"insecticidal soap|potassium salts of fatty acids",
        True,
        PET_LOW,
        False,
    ),
    Active("spinosad", r"spinosad", True, PET_LOW, True),
    Active(
        "pyrethrins",
        r"pyrethrin(?!oid)|pyrethrum",
        True,
        PET_CAUTION,
        True,
        "Cats are sensitive to pyrethrins; keep them away until it dries.",
    ),
    Active("iron phosphate", r"iron phosphate|ferric phosphate|sluggo", True, PET_LOW, False),
    Active("diatomaceous earth", r"diatomaceous earth", True, PET_LOW, False),
    Active("kaolin clay", r"kaolin|surround wp", True, PET_LOW, False),
    Active(
        "hydrogen peroxide",
        r"hydrogen peroxide|peroxyacetic|peracetic",
        True,
        PET_CAUTION,
        False,
        "The concentrate burns skin and eyes; store it out of reach.",
    ),
    Active("Epsom salt", r"epsom salt|magnesium sulfate", True, PET_LOW, False),
    # Not allowed in organic growing
    Active(
        "chlorothalonil",
        r"chlorothalonil|daconil",
        False,
        PET_CAUTION,
        False,
        "Keep pets off treated plants until the spray has dried.",
    ),
    Active("mancozeb", r"mancozeb", False, PET_CAUTION, False),
    Active("captan", r"captan", False, PET_CAUTION, False),
    Active("myclobutanil", r"myclobutanil", False, PET_CAUTION, False),
    Active("propiconazole", r"propiconazole", False, PET_CAUTION, False),
    Active("tebuconazole", r"tebuconazole", False, PET_CAUTION, False),
    Active("azoxystrobin", r"azoxystrobin", False, PET_CAUTION, False),
    Active("thiophanate-methyl", r"thiophanate", False, PET_CAUTION, False),
    Active("streptomycin", r"streptomycin", False, PET_CAUTION, False),
    Active("imidacloprid", r"imidacloprid", False, PET_CAUTION, True),
    Active(
        "neonicotinoid",
        r"neonicotinoid|thiamethoxam|clothianidin|dinotefuran|acetamiprid",
        False,
        PET_CAUTION,
        True,
    ),
    Active(
        "carbaryl",
        r"carbaryl|\bsevin\b",
        False,
        PET_HIGH,
        True,
        "Toxic to pets; keep them out of the treated area until it dries.",
    ),
    Active(
        "malathion",
        r"malathion",
        False,
        PET_HIGH,
        True,
        "Toxic to pets; keep them out of the treated area until it dries.",
    ),
    Active(
        "permethrin",
        r"permethrin",
        False,
        PET_HIGH,
        True,
        "Very toxic to cats, even in small amounts.",
    ),
    Active(
        "pyrethroid",
        r"pyrethroid|bifenthrin|cyfluthrin|cypermethrin|deltamethrin|lambda-cyhalothrin",
        False,
        PET_HIGH,
        True,
        "Cats are especially sensitive; keep pets away until it dries.",
    ),
    Active(
        "metaldehyde",
        r"metaldehyde",
        False,
        PET_HIGH,
        False,
        "Slug bait with metaldehyde is a common cause of serious dog poisoning.",
    ),
    Active("glyphosate", r"glyphosate|roundup", False, PET_CAUTION, False),
)

_COMPILED = [(a, re.compile(rf"(?<![\w-])(?:{a.pattern})", re.IGNORECASE)) for a in ACTIVES]

# Words that mean "apply something to the plant", for the spray-window offer.
SPRAY_WORDS = re.compile(
    r"\b(spray|sprays|spraying|apply|application|fungicide|insecticide|miticide|bactericide|"
    r"drench|dust)\b",
    re.IGNORECASE,
)
OIL_HEAT_LIMIT_C = 29.0  # about 85 F: oils and sulfur can burn leaves above this
HEAT_LIMIT_C = 32.0  # about 90 F
POISON_LINE = (
    "If a pet eats a treated plant or product, call your vet or ASPCA Animal Poison Control."
)


def find_actives(text: str) -> list[Active]:
    seen: list[Active] = []
    for active, rx in _COMPILED:
        if rx.search(text) and active not in seen:
            seen.append(active)
    return seen


def apply_garden(plan: CarePlan, garden: Garden) -> None:
    """Label every step, remove steps that break the gardener's preferences, and add the
    cautions that apply to the plan as a whole. Every field it sets is overwritten, so
    anything the model put there is discarded. Call it once per freshly written plan."""
    kept, removed = [], []
    found_all: list[Active] = []
    for step in plan.steps:
        found = find_actives(f"{step.action} {step.why}")
        step.actives = [a.name for a in found]
        step.organic = None if not found else all(a.organic for a in found)
        risks = [a.pet for a in found]
        step.pet_risk = (
            PET_HIGH
            if PET_HIGH in risks
            else PET_CAUTION
            if PET_CAUTION in risks
            else PET_LOW
            if found
            else ""
        )
        step.bee_risk = any(a.bees for a in found)
        if garden.organic and step.organic is False:
            names = ", ".join(a.name for a in found if not a.organic)
            removed.append(f"{step.action} (uses {names}, which is not allowed in organic growing)")
            continue
        if garden.pets and step.pet_risk == PET_HIGH:
            names = ", ".join(a.name for a in found if a.pet == PET_HIGH)
            removed.append(f"{step.action} ({names} is a serious risk to pets)")
            continue
        notes = [a.pet_note for a in found if a.pet_note and (garden.pets or a.pet == PET_HIGH)]
        step.precautions = list(dict.fromkeys(notes))
        kept.append(step)
        found_all += found
    plan.steps = kept
    plan.removed = list(dict.fromkeys(removed))

    cautions = []
    if any(a.oil for a in found_all) and any(a.sulfur for a in found_all):
        cautions.append(
            "Oil and sulfur together can burn leaves. Use one or the other; labels usually ask "
            "for a gap of 2 weeks or more between them."
        )
    if any(a.bees for a in found_all):
        cautions.append(
            "A product in this plan harms bees. Spray in the evening when bees are not foraging, "
            "and never on open flowers."
        )
    if garden.pets and found_all:
        cautions.append(POISON_LINE)
    if garden.organic and removed and not any(s.actives for s in kept):
        cautions.append(
            "The sources Leaf read only named non-organic products for this problem, so only the "
            "non-spray steps are left. Ask your extension office about an organic option."
        )
    plan.cautions = cautions
    plan.spray = any(SPRAY_WORDS.search(f"{s.action} {s.why}") or s.actives for s in kept)
    plan.heat_limit_c = (
        OIL_HEAT_LIMIT_C if any(a.oil or a.sulfur for a in found_all) else HEAT_LIMIT_C
    )
    plan.bee_safe_hours = any(a.bees for a in found_all)


def garden_prompt(garden: Garden) -> str:
    """The <garden> block for the care-plan writer."""
    wants = []
    if garden.organic:
        wants.append(
            "organic only: suggest only methods allowed in organic growing (for example copper, "
            "sulfur, neem or horticultural oil, potassium bicarbonate, Bacillus subtilis, "
            "insecticidal soap, spinosad, Bt), never synthetic fungicides or insecticides"
        )
    if garden.pets:
        wants.append(
            "pets or children use this garden: prefer the least toxic option and say how long "
            "to keep them away; never suggest metaldehyde slug bait, carbaryl, malathion or "
            "pyrethroids"
        )
    return "; ".join(wants) or "no special preferences"
