"""US states, the extension sites Leaf reads for each, and a point's state.

The hosts were each fetched in October 2026 and confirmed to be that state's university
extension or plant-health service. Three could not be read by the checker (a bot challenge,
a certificate error, empty pages) and are marked UNVERIFIED: a wrong host only means an empty
result, never a wrong report, because every report is checked against the page itself.
Washington D.C. has no extension host and is not covered.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

log = logging.getLogger("leaf.radar")

NWS_POINTS = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
NWS_UA = "Leaf garden care (https://leafathon.site)"
# Hosts shared with the rest of the university: only the extension part of each is read.
PATHS: dict[str, dict[str, str]] = {
    "AK": {"www.uaf.edu": "/ces"},
    "DE": {"www.udel.edu": "/academics/colleges/canr"},
    "ID": {"www.uidaho.edu": "/extension"},
    "MA": {"www.umass.edu": "/agriculture-food-environment/extension-outreach"},
    "ND": {"www.ndsu.edu": "/agriculture/extension"},
    "RI": {"web.uri.edu": "/coopext"},
    "SC": {"www.clemson.edu": "/extension"},
    "VT": {"www.uvm.edu": "/extension"},
    "WY": {"www.uwyo.edu": "/uwe"},
}
NOT_COVERED = "-"  # stored for a cell outside the US, or in a state Leaf has no hosts for

# code: (name, hosts, verified)
_STATES: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "AL": ("Alabama", ("aces.edu",), True),
    "AK": ("Alaska", ("www.uaf.edu",), True),
    "AZ": ("Arizona", ("extension.arizona.edu",), True),
    "AR": ("Arkansas", ("www.uaex.uada.edu",), True),
    "CA": ("California", ("ucanr.edu",), True),
    "CO": ("Colorado", ("extension.colostate.edu", "planttalk.colostate.edu"), True),
    "CT": ("Connecticut", ("homegarden.cahnr.uconn.edu", "extension.uconn.edu"), True),
    "DE": ("Delaware", ("www.udel.edu",), True),
    "DC": ("District of Columbia", (), True),
    "FL": ("Florida", ("ifas.ufl.edu",), True),
    "GA": ("Georgia", ("extension.uga.edu",), True),
    "HI": ("Hawaii", ("ctahr.hawaii.edu",), True),
    "ID": ("Idaho", ("www.uidaho.edu",), True),
    "IL": ("Illinois", ("extension.illinois.edu",), True),
    "IN": ("Indiana", ("extension.purdue.edu", "ag.purdue.edu"), True),
    "IA": ("Iowa", ("extension.iastate.edu",), True),
    "KS": ("Kansas", ("extension.k-state.edu",), True),
    "KY": ("Kentucky", ("mgcafe.uky.edu",), True),
    "LA": ("Louisiana", ("www.lsuagcenter.com",), False),
    "ME": ("Maine", ("extension.umaine.edu",), True),
    "MD": ("Maryland", ("extension.umd.edu",), True),
    "MA": ("Massachusetts", ("www.umass.edu", "extension.umass.edu"), True),
    "MI": ("Michigan", ("canr.msu.edu",), False),
    "MN": ("Minnesota", ("extension.umn.edu",), True),
    "MS": ("Mississippi", ("extension.msstate.edu",), True),
    "MO": ("Missouri", ("extension.missouri.edu", "ipm.missouri.edu"), True),
    "MT": ("Montana", ("extension.montana.edu",), True),
    "NE": ("Nebraska", ("extension.unl.edu", "hles.unl.edu"), True),
    "NV": ("Nevada", ("extension.unr.edu",), True),
    "NH": ("New Hampshire", ("extension.unh.edu",), True),
    "NJ": ("New Jersey", ("extension.rutgers.edu",), True),
    "NM": ("New Mexico", ("extension.nmsu.edu", "aces.nmsu.edu"), True),
    "NY": ("New York", ("cals.cornell.edu", "plantclinic.cornell.edu"), True),
    "NC": ("North Carolina", ("ces.ncsu.edu",), True),
    "ND": ("North Dakota", ("www.ndsu.edu",), True),
    "OH": ("Ohio", ("extension.osu.edu", "bygl.osu.edu"), True),
    "OK": ("Oklahoma", ("extension.okstate.edu",), False),
    "OR": ("Oregon", ("extension.oregonstate.edu",), True),
    "PA": ("Pennsylvania", ("extension.psu.edu",), True),
    "RI": ("Rhode Island", ("web.uri.edu",), True),
    "SC": ("South Carolina", ("www.clemson.edu", "hgic.clemson.edu"), True),
    "SD": ("South Dakota", ("extension.sdstate.edu",), True),
    "TN": ("Tennessee", ("utextension.tennessee.edu",), True),
    "TX": ("Texas", ("agrilifeextension.tamu.edu", "aggie-horticulture.tamu.edu"), True),
    "UT": ("Utah", ("extension.usu.edu",), True),
    "VT": ("Vermont", ("www.uvm.edu",), True),
    "VA": ("Virginia", ("ext.vt.edu",), True),
    "WA": ("Washington", ("extension.wsu.edu", "hortsense.cahnrs.wsu.edu"), True),
    "WV": ("West Virginia", ("extension.wvu.edu",), True),
    "WI": ("Wisconsin", ("extension.wisc.edu", "pddc.wisc.edu"), True),
    "WY": ("Wyoming", ("www.uwyo.edu",), True),
}
NAMES: dict[str, str] = {code: v[0] for code, v in _STATES.items()}
UNVERIFIED = sorted(code for code, v in _STATES.items() if not v[2])


def covered(region: str) -> bool:
    return bool(_STATES.get(region, ("", (), True))[1])


def name(region: str) -> str:
    return NAMES.get(region, region)


def include_domains(region: str) -> list[str]:
    """The host patterns Tavily is limited to: each host, and its subdomains unless it is a
    bare www host (shared with the rest of the university)."""
    out: list[str] = []
    for host in _STATES.get(region, ("", (), True))[1]:
        path = PATHS.get(region, {}).get(host)
        if path:
            out.append(f"{host}{path}/*")
        else:
            out.append(host)
            if not host.startswith("www."):
                out.append(f"*.{host}")
    return out


def host_ok(url: str, region: str) -> bool:
    """The page is on one of the state's extension hosts (and under its path, for a host shared
    with the rest of the university). Tavily's filter and redirects are not taken on trust."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https"):
        return False
    for h in _STATES.get(region, ("", (), True))[1]:
        path = PATHS.get(region, {}).get(h)
        if path:
            if host == h and (parsed.path == path or parsed.path.startswith(path + "/")):
                return True
        elif host == h or host.endswith("." + h):
            return True
    return False


def _state_pattern(state: str) -> re.Pattern[str]:
    pat = re.escape(state.lower())
    if state == "Virginia":  # "West Virginia"
        pat = r"(?<!west )" + pat
    elif state == "Washington":  # "Washington D.C.", and "Washington County" (in many states)
        pat += r"(?!,? d\.?c)(?! county)"
    return re.compile(rf"\b{pat}\b")


_PATTERNS = {code: _state_pattern(n) for code, n in NAMES.items() if code != "DC"}


def other_states_named(text: str, region: str) -> set[str]:
    """States other than `region` that the text names (by full name)."""
    low = text.lower()
    return {c for c, rx in _PATTERNS.items() if c != region and rx.search(low)}


def names_state(text: str, region: str) -> bool:
    rx = _PATTERNS.get(region)
    return bool(rx and rx.search(text.lower()))


async def region_for_point(lat: float, lon: float, http: httpx.AsyncClient | None = None) -> str:
    """The US state a point is in (the National Weather Service's own lookup), or NOT_COVERED
    for a point outside the US or in a state Leaf has no hosts for. Raises httpx.HTTPError
    when NWS cannot be reached, so the caller tries again later."""
    client = http or httpx.AsyncClient(timeout=15)
    try:
        r = await client.get(
            NWS_POINTS.format(lat=lat, lon=lon),
            headers={"User-Agent": NWS_UA, "Accept": "application/geo+json"},
        )
    finally:
        if http is None:
            await client.aclose()
    if r.status_code == 404:  # NWS answers 404 for points outside its coverage
        return NOT_COVERED
    r.raise_for_status()
    state = (
        ((r.json().get("properties") or {}).get("relativeLocation") or {})
        .get("properties", {})
        .get("state", "")
    )
    return state if covered(state) else NOT_COVERED
