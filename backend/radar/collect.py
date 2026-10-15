"""One weekly Radar run for one US state.

1. Search the state's extension sites (Tavily, limited to the hosts in `regions`).
2. Read the pages found that Leaf has not read lately (Tavily, whole pages, 5 per credit).
3. In code: find the page's own date (a page with none, or an old one, is not a current report),
   cut it to the few paragraphs that look like an occurrence, and refuse text that tries to
   instruct an AI reader.
4. Nano 3 screens each remaining page: a current report, and no injection.
5. Lightning writes the reports, each with a sentence copied from the page.
6. Code checks every report against the page (below). Only reports that pass are kept.

Page text is untrusted: it only ever reaches a model inside <page> or <passages>, sanitised, and
the models' words are never shown: only the page's own quote is.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from backend.config import Settings
from backend.knowledge.profile import _plain, _words, quote_in_passage
from backend.models import LLM, ModelError, complete_json, gather_limited
from backend.prompts import RADAR_EXTRACT_SYSTEM, RADAR_SCREEN_SYSTEM, sanitize
from backend.radar import regions
from backend.router import code_screen, normalize
from backend.schemas import ModelCall, RadarExtraction, RadarFinding, RadarScreen
from backend.search import Searcher

log = logging.getLogger("leaf.radar")

WINDOW_DAYS = 45  # a report older than this is not "current"
MAX_PAGES = 10  # pages read per region per week (two Tavily credits)
PAGE_RECHECK_DAYS = 21  # a page read lately is not paid for again
MIN_SNIPPET_CHARS = 120
MAX_REPORTS_PER_PAGE = 4
EXCERPT_PARAGRAPHS = 3
EXCERPT_CHARS = 1400
MIN_QUOTE_WORDS = 6
HEAD_CHARS = 2500  # where a page's own date is looked for
# Two searches a run. The first favours dated news (the month and year are in the words of a
# current post); the second names pests that extension services report when they arrive.
# These only steer the search: no pest is ever recorded without a sentence from a page.
QUERIES = (
    "{state} {month} {year} pest or disease detected confirmed reported gardens trees county",
    "{state} spotted lanternfly emerald ash borer late blight boxwood blight jumping worm "
    "detected found confirmed",
)

# A sighting needs a verb that says something was seen, not just the noun "reports" (a tracker
# page's heading, "Current reports of late blight", is not a sighting).
_OCCURRENCE = re.compile(
    r"\b(confirmed|detected|found|observed|identified|diagnosed|caught|spotted|seen|noticed|"
    r"infested|infected|trapped|(?:have|has|had)\s+(?:all\s+)?reported|"
    r"reported\s+(?:in|on|at|from|across|by)|outbreak\w*|infestation\w*)\b",
    re.IGNORECASE,
)
# A sentence about what might happen is advice, not a report. ("May" is also a month, so it
# counts only as a modal: "may be", "may occur".)
_FORECAST = re.compile(
    r"\b(expect\w*|might|could|potential\w*|risk of|watch for|be on the lookout|scout for|"
    r"look out|anticipat\w*|forecast\w*|if you see|should you|"
    r"may\s+(?:be|become|occur|appear|develop|cause|show|spread|see|find|have|need|continue|"
    r"emerge|start|begin|arrive))\b",
    re.IGNORECASE,
)
# "No late blight has been confirmed" is the opposite of a sighting.
_NEGATION = re.compile(
    r"\b(no|not|never|none|nor|neither|without|absence|absent|free of|cannot|unable)\b|n't\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:19|20)\d\d\b")
_MARKER = re.compile(
    r"\b(posted|published|updated|modified|released|issued|date)\b[^\n]{0,30}$", re.I
)
_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        1,
    )
    for m in names
}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MDY = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.IGNORECASE)
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})\.?,?\s+(20\d\d)\b", re.IGNORECASE)
_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")
_URL_DATE = re.compile(r"/(20\d\d)/(\d{1,2})(?:/(\d{1,2}))?(?:/|$|-)")
_STOP = {"the", "of", "and", "in", "on", "a", "an", "to", "for", "by"}


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def page_date(title: str, text: str, url: str, today: date) -> date | None:
    """The page's own date, found in code. A date beside "posted", "published" or "updated"
    wins; otherwise the dates in the opening text must agree (within the window), because a page
    that shows a 2026 event beside a 2019 article has no date Leaf can trust. A date later than
    tomorrow is an event, not a publication. With none in the text, a date in the URL."""
    head = f"{title}\n{text[:HEAD_CHARS]}"
    found: list[tuple[int, date]] = []
    for rx, y, mo, d in (
        (_MDY, 3, 1, 2),
        (_DMY, 3, 2, 1),
        (_ISO, 1, 2, 3),
        (_SLASH, 3, 1, 2),
    ):
        for m in rx.finditer(head):
            month = _MONTHS[m[mo].lower()] if rx in (_MDY, _DMY) else int(m[mo])
            day = _safe_date(int(m[y]), month, int(m[d]))
            if day:
                found.append((m.start(), day))
    valid = sorted((pos, d) for pos, d in found if d <= today + timedelta(days=1))
    marked = [(pos, d) for pos, d in valid if _MARKER.search(head[max(0, pos - 40) : pos])]
    if marked:
        return marked[0][1]
    if valid:
        days = [d for _, d in valid]
        return valid[0][1] if max(days) - min(days) <= timedelta(days=WINDOW_DAYS) else None
    m = _URL_DATE.search(urlparse(url).path)
    if m:
        d = _safe_date(int(m[1]), int(m[2]), int(m[3] or 1))
        if d and d <= today + timedelta(days=1):
            return d
    return None


def best_excerpt(text: str, state: str) -> str:
    """The few paragraphs of a page most likely to state an occurrence: those that use words
    like detected, confirmed or reported (and name the state), in page order."""
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n+", text)]
    scored = []
    for i, p in enumerate(paragraphs):
        if not 60 <= len(p) <= 1500:
            continue
        score = len(_OCCURRENCE.findall(p)) + (2 if state.lower() in p.lower() else 0)
        if score >= 1:
            scored.append((score, i, p[:600]))
    best = sorted(sorted(scored, reverse=True)[:EXCERPT_PARAGRAPHS], key=lambda t: t[1])
    return "\n".join(p for _, _, p in best)[:EXCERPT_CHARS]


def singular(word: str) -> str:
    """A rough singular ("tomatoes" and "tomato", "aphids" and "aphid", "berries" and "berry")."""
    if len(word) <= 3 or word.endswith("ss") or not word.endswith("s"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "xes", "sses", "zes")):
        return word[:-2]
    return word[:-1]


def pest_key(name: str) -> str:
    """A pest or disease name as normalised words ("Early Blight" and "early blights" agree)."""
    return " ".join(singular(w) for w in _words(name).split() if w not in _STOP)[:80]


@dataclass
class Page:
    id: str
    url: str
    title: str
    excerpt: str
    on: date


@dataclass
class Collection:
    reports: list[dict[str, Any]] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)  # url -> reports, none, undated, ...
    searched: int = 0
    read: int = 0
    relevant: int = 0
    undated: int = 0
    rejected: int = 0
    credits: int = 0
    note: str = ""


def check_findings(
    findings: list[RadarFinding], pages: dict[str, Page], region: str, week: str
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """The code checks. A report is kept only when its page exists, the quote is in that page's
    text word for word, the quote names the pest and says it was seen (not that it might be),
    the page is not about another state, and the status is one Leaf accepts. Returns
    (reports, [(pest, why rejected)])."""
    kept: list[dict[str, Any]] = []
    rejected: list[tuple[str, str]] = []
    per_page: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for f in findings:
        pest = _plain(f.pest, 80)
        page = pages.get(f.passage.strip().upper())

        def no(why: str, pest: str = pest) -> None:
            rejected.append((pest or "?", why))

        quote_words = _words(f.quote).split()
        key = pest_key(pest)
        if page is None:
            no("no such page")
        elif f.status not in ("confirmed", "reported"):
            no("not an occurrence")
        elif not key or not quote_in_passage(f.quote, page.excerpt):
            no("quote not on the page")
        elif len(quote_words) < MIN_QUOTE_WORDS:
            no("quote too short")
        elif not set(key.split()) <= {singular(w) for w in quote_words}:
            no("quote does not name the pest")
        elif not _OCCURRENCE.search(f.quote) or _FORECAST.search(f.quote):
            no("quote is advice or a forecast, not a sighting")
        elif _NEGATION.search(f.quote):
            no("quote says it was not seen")
        elif any(int(y) != page.on.year for y in _YEAR.findall(f.quote)):
            no("quote is about another year")
        elif regions.other_states_named(page.excerpt, region) and not regions.names_state(
            page.excerpt, region
        ):
            no("page is about another state")
        elif (page.url, key) in seen:
            continue
        elif per_page.get(page.url, 0) >= MAX_REPORTS_PER_PAGE:
            no("too many from one page")
        else:
            seen.add((page.url, key))
            per_page[page.url] = per_page.get(page.url, 0) + 1
            on_page = f" {_words(page.excerpt)} "
            place = _plain(f.where, 80)
            if len(_words(place)) < 3 or f" {_words(place)} " not in on_page:
                place = ""  # a place the page does not state is dropped, not guessed
            seen_words = {singular(w) for w in on_page.split()}
            hosts = [
                h
                for h in (_plain(x, 60) for x in f.hosts)
                if h and {singular(w) for w in _words(h).split()} <= seen_words
            ][:6]  # plants the page names; the model's other words are dropped
            status = (
                "confirmed"
                if re.search(r"\b(confirm\w*|diagnos\w*)\b", f.quote, re.I)
                else "reported"
            )
            kept.append(
                {
                    "region": region,
                    "pest": pest,
                    "pest_key": key,
                    "hosts": hosts,
                    "place": place,
                    "status": status,  # from the quote's own words, not the model's say-so
                    "reported_on": page.on.isoformat(),
                    "quote": _plain(f.quote, 500),
                    "source_title": _plain(page.title, 200) or page.url,
                    "source_url": page.url,
                    "week": week,
                }
            )
    return kept, rejected


async def collect_region(
    llm: LLM,
    settings: Settings,
    searcher: Searcher,
    region: str,
    today: date,
    week: str,
    already_read: Callable[[list[str]], Awaitable[set[str]]],
    trace: list[ModelCall],
    on_credits: Callable[[int], Awaitable[None]] | None = None,
) -> Collection:
    """Run steps 1 to 6 for one state. Raises SearchPaused when Tavily credits are used up."""
    out = Collection()
    state = regions.name(region)

    async def spent(credits: int) -> None:
        out.credits += credits
        if on_credits is not None and credits:
            await on_credits(credits)

    # 1. Search the state's extension sites.
    found: dict[str, dict[str, Any]] = {}
    for q in QUERIES:
        results, credits = await searcher.search_web(
            q.format(state=state, month=today.strftime("%B"), year=today.year),
            time_range="year",
            max_results=10,
            include_domains=regions.include_domains(region),
        )
        await spent(credits)
        for r in results:
            url = str(r.get("url") or "")
            if url and url not in found and regions.host_ok(url, region):
                found[url] = r
    out.searched = len(found)
    read_lately = await already_read(list(found))
    best_first = sorted(found, key=lambda u: -float(found[u].get("score") or 0))
    fresh = [u for u in best_first if u not in read_lately][
        :MAX_PAGES
    ]  # the best hits, either query

    # 2. Read the pages (five a credit).
    texts: dict[str, str] = {}
    for i in range(0, len(fresh), 5):
        got, credits = await searcher.extract_pages(fresh[i : i + 5])
        await spent(credits)
        texts.update(got)
    texts = {u: t for u, t in texts.items() if u in found}  # nothing a redirect brought in
    out.read = len(texts)
    snippets = 0
    for url in fresh:
        if url in texts:
            continue
        # Some sites refuse Tavily's reader. The search result's own snippet is a verbatim piece
        # of the page (often with its date), so it is read instead; with none, the page is skipped.
        snippet = str(found[url].get("content") or "")
        if len(snippet) >= MIN_SNIPPET_CHARS:
            texts[url] = snippet
            snippets += 1
        else:
            out.outcomes[url] = "unreadable"
    if snippets:
        out.note = f"{snippets} page(s) could not be opened, so their search excerpts were read."

    # 3. Date, cut and check in code.
    pages: dict[str, Page] = {}
    for url, text in texts.items():
        title = _plain(found[url].get("title") or "", 200)
        on = page_date(title, text, url, today)
        if on is None:
            out.outcomes[url], out.undated = "undated", out.undated + 1
            continue
        if today - on > timedelta(days=WINDOW_DAYS):
            out.outcomes[url] = "old"
            continue
        excerpt = best_excerpt(text, state)
        if not excerpt:
            out.outcomes[url] = "none"
            continue
        if code_screen(normalize(excerpt)) is not None:
            out.outcomes[url] = "unsafe"
            log.warning("radar: page text with instruction-like wording skipped: %s", url[:100])
            continue
        pages[url] = Page(f"P{len(pages) + 1}", url, title, excerpt, on)

    # 4. Nano screens each page.
    async def screen(page: Page) -> RadarScreen:
        return await complete_json(
            llm,
            step="radar_screen",
            models=settings.models("radar_screen"),
            messages=[
                {"role": "system", "content": RADAR_SCREEN_SYSTEM},
                {
                    "role": "user",
                    "content": f"<page><title>{sanitize(page.title, 200)}</title>"
                    f"<excerpts>{sanitize(page.excerpt, EXCERPT_CHARS)}</excerpts></page>",
                },
            ],
            schema=RadarScreen,
            trace=trace,
            thinking=False,
            max_tokens=settings.budget("radar_screen"),
            temperature=0.1,
        )

    items = list(pages.values())
    verdicts = await gather_limited([screen(p) for p in items], limit=4)
    keep: dict[str, Page] = {}
    for page, v in zip(items, verdicts, strict=True):
        if isinstance(v, BaseException):
            if not isinstance(v, ModelError):
                raise v
            out.outcomes[page.url] = "unscreened"  # tried again next week
        elif v.injection:
            out.outcomes[page.url] = "unsafe"
        elif not v.relevant:
            out.outcomes[page.url] = "none"
        else:
            keep[page.id] = page
    out.relevant = len(keep)

    # 5. Lightning writes the reports.
    findings: list[RadarFinding] = []
    if keep:
        listing = [
            {"id": p.id, "source": sanitize(p.title, 200), "text": sanitize(p.excerpt, 1400)}
            for p in keep.values()
        ]
        extracted = await complete_json(
            llm,
            step="radar_extract",
            models=settings.models("radar_extract"),
            messages=[
                {"role": "system", "content": RADAR_EXTRACT_SYSTEM},
                {
                    "role": "user",
                    "content": f"<region>{state}</region>\n"
                    f"<passages>{json.dumps(listing, ensure_ascii=False)}</passages>",
                },
            ],
            schema=RadarExtraction,
            trace=trace,
            thinking=False,
            max_tokens=settings.budget("radar_extract"),
            temperature=0.2,
        )
        findings = extracted.reports

    # 6. Code checks every report against its page.
    out.reports, rejected = check_findings(findings, keep, region, week)
    out.rejected = len(rejected)
    for why in {w for _, w in rejected}:
        log.info(
            "radar %s: %d report(s) rejected: %s",
            region,
            sum(1 for _, w in rejected if w == why),
            why,
        )
    with_reports = {r["source_url"] for r in out.reports}
    for page in keep.values():
        out.outcomes[page.url] = "reports" if page.url in with_reports else "none"
    return out
