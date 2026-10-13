"""Canonical plant problems: names that count as the same condition, and ones that must not.

Used to score diagnoses fairly (a model that says "Alternaria solani" named early blight) and to
recognise high-stakes conditions in the final ranking. Aliases are regular expressions matched
on whole words, case-insensitively; `not_aliases` mark look-alike names that are a different
condition (target spot is not early blight; a rust mite is a pest, not rust). When a name
mentions several conditions, the one named first wins ("early blight or late blight" is early
blight). Names follow US extension-service usage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Condition:
    id: str
    name: str
    kind: str  # "disease", "abiotic" or "pest"
    aliases: tuple[str, ...]
    not_aliases: tuple[str, ...] = ()
    high_stakes: str = ""  # why a lab should confirm it even when Leaf is fairly sure
    sources: tuple[str, ...] = field(default=())


# fmt: off
CONDITIONS: tuple[Condition, ...] = (
    # ---------------------------------------------------------------- fungal and oomycete
    Condition("late_blight", "Late blight", "disease",
              (r"late blight", r"phytophthora infestans"),
              high_stakes="A community disease: one spot releases thousands of airborne spores "
              "within days, so neighbouring tomato and potato plantings are at risk. Extension "
              "services ask growers to report it.",
              sources=("https://extension.umn.edu/disease-management/late-blight",)),
    Condition("early_blight", "Early blight", "disease",
              (r"early blight", r"alternaria (solani|linariae|tomatophila)"),
              not_aliases=(r"target spot", r"corynespora", r"gr[ae]y leaf spot", r"stemphylium"),
              sources=("https://extension.umn.edu/disease-management/early-blight-tomato-and-potato",)),
    Condition("septoria", "Septoria leaf spot", "disease", (r"septoria",)),
    Condition("powdery_mildew", "Powdery mildew", "disease",
              (r"powdery mildew", r"podosphaera", r"golovinomyces", r"erysiphe", r"sphaerotheca",
               r"leveillula", r"oidium")),
    Condition("downy_mildew", "Downy mildew", "disease",
              (r"downy mildew", r"plasmopara", r"pseudoperonospora", r"peronospora", r"bremia")),
    Condition("rust", "Rust", "disease",
              (r"rust", r"uromyces", r"phragmidium", r"puccinia", r"gymnosporangium"),
              not_aliases=(r"rust mites?", r"russet mites?", r"rusty")),
    Condition("black_spot", "Black spot", "disease",
              (r"black spot", r"diplocarpon", r"marssonina rosae")),
    Condition("apple_scab", "Apple scab", "disease",
              (r"apple scab", r"venturia inaequalis", r"scab"),
              not_aliases=(r"common scab", r"streptomyces", r"powdery scab")),
    Condition("cercospora", "Cercospora leaf spot", "disease",
              (r"cercospora", r"frog-?eye leaf spot")),
    Condition("anthracnose", "Anthracnose", "disease",
              (r"anthracnose", r"colletotrichum", r"sphaceloma")),
    Condition("gray_mold", "Gray mold", "disease", (r"gr[ae]y mou?ld", r"botrytis")),
    Condition("boxwood_blight", "Boxwood blight", "disease",
              (r"boxwood blight", r"calonectria pseudonaviculata", r"cylindrocladium buxicola"),
              high_stakes="Spreads on moved plants; extension advises a lab diagnosis to stop it.",
              sources=("https://homegarden.cahnr.uconn.edu/factsheets/boxwood-blight/",)),
    Condition("sudden_oak_death", "Sudden oak death", "disease",
              (r"sudden oak death", r"phytophthora ramorum", r"ramorum"),
              high_stakes="Under federal quarantine with over 100 host plants and no treatment; "
              "report it and do not move affected plants.",
              sources=("https://www.aphis.usda.gov/plant-pests-diseases/pramorum",)),
    # ---------------------------------------------------------------- bacterial and viral
    Condition("bacterial_spot", "Bacterial spot", "disease",
              (r"bacterial (leaf )?spot", r"xanthomonas (euvesicatoria|vesicatoria|perforans|"
               r"gardneri|campestris pv\.? vesicatoria)"),
              not_aliases=(r"bacterial speck",),
              sources=("https://extension.umn.edu/disease-management/bacterial-spot-tomato-and-pepper",)),
    Condition("fire_blight", "Fire blight", "disease", (r"fire ?blight", r"erwinia amylovora")),
    Condition("ralstonia_r3b2", "Bacterial wilt (Ralstonia race 3 biovar 2)", "disease",
              (r"race 3 biovar 2", r"r3bv2"),
              high_stakes="A US select agent: APHIS asks for an immediate report.",
              sources=("https://www.aphis.usda.gov/plant-pests-diseases/ralstonia",)),
    Condition("citrus_greening", "Citrus greening (huanglongbing)", "disease",
              (r"citrus greening", r"huanglongbing", r"hlb", r"candidatus liberibacter"),
              high_stakes="No cure and under federal quarantine; do not move citrus plants or "
              "fruit out of quarantined areas.",
              sources=("https://www.aphis.usda.gov/plant-pests-diseases/citrus-diseases/"
                       "citrus-greening-and-asian-citrus-psyllid",)),
    Condition("tobrfv", "Tomato brown rugose fruit virus", "disease",
              (r"brown rugose", r"tobrfv"),
              high_stakes="Regulated by APHIS for imported seed and transplants; it breaks "
              "tomato resistance genes and spreads on hands and tools.",
              sources=("https://www.aphis.usda.gov/plant-pests-diseases/tbrfv/faqs",)),
    Condition("mosaic_virus", "Mosaic virus", "disease",
              (r"mosaic", r"tobamovirus", r"tmv", r"tomv", r"cmv")),
    # ---------------------------------------------------------------- abiotic
    Condition("magnesium_deficiency", "Magnesium deficiency", "abiotic",
              (r"magnesium", r"mg deficiency")),
    Condition("nitrogen_deficiency", "Nitrogen deficiency", "abiotic",
              (r"nitrogen deficiency", r"nitrogen", r"n deficiency"),
              not_aliases=(r"nitrogen excess", r"too much nitrogen", r"excess nitrogen")),
    Condition("manganese_deficiency", "Manganese deficiency", "abiotic",
              (r"manganese", r"mn deficiency")),
    Condition("iron_chlorosis", "Iron chlorosis", "abiotic",
              (r"iron (deficiency|chlorosis)", r"iron", r"fe deficiency")),
    Condition("potassium_deficiency", "Potassium deficiency", "abiotic",
              (r"potassium", r"k deficiency"), not_aliases=(r"potassium bicarbonate",)),
    Condition("sunscald", "Sunscald", "abiotic",
              (r"sun ?scald", r"sunburn", r"sun scorch", r"solar injury")),
    Condition("herbicide_injury", "Herbicide injury", "abiotic",
              (r"herbicide", r"2,?4-?d", r"dicamba", r"glyphosate", r"growth regulator injury",
               r"phenoxy")),
    Condition("overwatering", "Overwatering or root rot", "abiotic",
              (r"over-?watering", r"over-?watered", r"root rot", r"waterlogg\w*",
               r"poor drainage")),
    Condition("edema", "Edema (oedema)", "abiotic", (r"o?edema",)),
    Condition("cold_injury", "Frost or cold injury", "abiotic",
              (r"frost", r"freeze", r"cold (injury|damage)", r"chilling")),
    Condition("blossom_end_rot", "Blossom end rot", "abiotic", (r"blossom[- ]end rot",)),
    Condition("drought_scorch", "Drought stress or leaf scorch", "abiotic",
              (r"drought", r"leaf scorch", r"water stress", r"under-?watering")),
    # ---------------------------------------------------------------- pests
    Condition("spider_mites", "Spider mites", "pest",
              (r"spider mites?", r"tetranychus", r"mite stippling")),
    Condition("aphids", "Aphids", "pest", (r"aphids?",)),
    Condition("scale_insects", "Scale insects", "pest",
              (r"scale insects?", r"soft scales?", r"armou?red scales?", r"coccus",
               r"(brown|white|black|cottony|wax|oyster ?shell|euonymus|magnolia) scale")),
    Condition("leaf_miner", "Leaf miners", "pest",
              (r"leaf ?miners?", r"liriomyza", r"leaf ?mining")),
    Condition("spotted_lanternfly", "Spotted lanternfly", "pest",
              (r"spotted lanternfl(y|ies)", r"lycorma"),
              high_stakes="An invasive pest many states ask residents to report.",
              sources=("https://www.aphis.usda.gov/plant-pests-diseases/slf",)),
    Condition("box_tree_moth", "Box tree moth", "pest", (r"box tree moth", r"cydalima"),
              high_stakes="Under federal quarantine; APHIS asks owners to report damage.",
              sources=("https://www.aphis.usda.gov/plant-pests-diseases/box-tree-moth",)),
    Condition("chewing_insects", "Chewing insects", "pest",
              (r"flea beetles?", r"japanese beetles?", r"chewing (insects?|damage|pest)",
               r"caterpillars?", r"beetle feeding")),
)
# fmt: on
BY_ID = {c.id: c for c in CONDITIONS}


def _compile(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    return [re.compile(rf"(?<![a-z]){p}(?![a-z])", re.IGNORECASE) for p in patterns]


_ALIASES = [(c, _compile(c.aliases), _compile(c.not_aliases)) for c in CONDITIONS]


def identify(name: str) -> str | None:
    """The canonical condition a diagnosis names, or None if it names none we know."""
    best: tuple[int, str] | None = None
    for cond, aliases, nots in _ALIASES:
        blocked = [m.span() for p in nots for m in p.finditer(name)]
        for p in aliases:
            for m in p.finditer(name):
                if any(s <= m.start() < e for s, e in blocked):
                    continue  # this mention is part of a look-alike name
                if best is None or m.start() < best[0]:
                    best = (m.start(), cond.id)
                break
    return best[1] if best else None


def high_stakes(name: str) -> Condition | None:
    cid = identify(name)
    cond = BY_ID.get(cid) if cid else None
    return cond if cond and cond.high_stakes else None
