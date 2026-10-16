"""M3: the plant knowledge graph and photo identification, offline with fakes."""

from __future__ import annotations

import contextlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import RateLimiter, app
from backend.config import NEMOTRON_LIGHTNING, NEMOTRON_SUPER, Settings
from backend.data.driver import SqliteDriver
from backend.data.migrate import apply
from backend.data.repo import Repo
from backend.jobs import JobContext, JobMessage, JobRunner
from backend.journal import MemoryJournal
from backend.knowledge import species_key
from backend.knowledge.identify import identify
from backend.knowledge.profile import (
    build_profile,
    check_extracted,
    clean_value,
    profile_view,
    quote_in_passage,
)
from backend.schemas import ExtractedFact
from backend.search import SearchPaused
from tests.conftest import FakeLLM, FakeSearcher, make_leaf
from tests.test_agent import RESP

TOMATO_PAGE = (
    "Tomatoes need full sun, at least 6 to 8 hours a day. Keep soil evenly moist and water "
    "deeply about once a week. The leaves and stems of tomato are toxic to dogs and cats. "
    "Plant after the last frost when nights stay above 10 C."
)
FOXGLOVE_PAGE = "All parts of foxglove are highly poisonous to people and pets."


# ---------- names and code checks ----------


@pytest.mark.parametrize(
    ("name", "binomial", "key"),
    [
        ("Solanum lycopersicum", True, "solanum lycopersicum"),
        ("Solanum lycopersicum 'Roma'", True, "solanum lycopersicum"),
        ("Fragaria × ananassa", True, "fragaria ananassa"),
        ("Brassica oleracea var. capitata", True, "brassica oleracea"),
        ("Cherry Tomato", False, "cherry tomato"),
        ("  basil ", False, "basil"),
        ("!!!", False, ""),
        # Common names keep every word: no two plants share a profile (or its toxicity).
        ("String of pearls", False, "string of pearls"),
        ("String of hearts", False, "string of hearts"),
        ("Lily of the valley", False, "lily of the valley"),
        ("Swiss cheese plant", False, "swiss cheese plant"),
    ],
)
def test_species_keys(name, binomial, key):
    assert species_key(name, binomial=binomial) == key


def test_quotes_must_be_word_for_word_and_long_enough():
    assert quote_in_passage("toxic to dogs and cats", TOMATO_PAGE)
    assert quote_in_passage("Toxic to  DOGS and cats.", TOMATO_PAGE)  # case, spaces, period
    assert not quote_in_passage("safe for dogs and cats", TOMATO_PAGE)
    assert not quote_in_passage("full sun", TOMATO_PAGE)  # too short to prove anything


@pytest.mark.parametrize(
    ("field", "value", "out"),
    [
        ("toxic_to_pets", "Mildly Toxic", "mildly_toxic"),
        ("toxic_to_pets", "safe", None),
        ("temp_min_c", "10", 10.0),
        ("temp_min_c", 500, None),
        ("pests", "aphids", ["aphids"]),
        ("light", "", None),
        ("light", "acid (<6.0)\n", "acid (<6.0)"),  # plain text: the page escapes it
    ],
)
def test_values_are_typed_in_code(field, value, out):
    assert clean_value(field, value) == out


def test_extracted_facts_are_checked_before_any_model_verifies_them():
    passages = {"P1": {"url": "https://extension.example.edu/t", "title": "T", "text": TOMATO_PAGE}}
    facts = [
        ExtractedFact(
            field="light", value="full sun", quote="full sun, at least 6 to 8 hours", passage="P1"
        ),
        ExtractedFact(
            field="toxic_to_pets", value="non_toxic", quote="safe for all pets", passage="P1"
        ),  # invented quote
        ExtractedFact(field="water", value="weekly", quote="water deeply", passage="P9"),
        ExtractedFact(field="favourite_colour", value="red", quote="x y z w", passage="P1"),
    ]
    kept, rejected = check_extracted(facts, passages)
    assert [f["field"] for f in kept] == ["light"]
    assert {r["note"] for r in rejected} == {
        "quote not found in the passage",
        "no such passage",
        "unknown field or value",
    }


# ---------- building a profile ----------

EXTRACTED = {
    "facts": [
        {
            "field": "light",
            "value": "full sun, 6-8 hours",
            "quote": "need full sun, at least 6 to 8 hours a day",
            "passage": "P1",
        },
        {
            "field": "toxic_to_pets",
            "value": "toxic",
            "quote": "leaves and stems of tomato are toxic to dogs and cats",
            "passage": "P1",
        },
        {"field": "water", "value": "daily", "quote": "water every single day", "passage": "P1"},
        {"field": "temp_min_c", "value": 10, "quote": "nights stay above 10 C", "passage": "P1"},
    ]
}


def searcher_with(pages: dict[str, list[str]]) -> FakeSearcher:
    s = FakeSearcher()
    s.pages = pages
    return s


async def test_a_profile_keeps_only_verified_facts_and_marks_the_rest():
    llm = FakeLLM(
        {
            "fact_extract": EXTRACTED,
            # Super rejects the temperature (the passage is about nights, not a limit).
            "fact_verify": {
                "verdicts": [
                    {"i": 0, "supported": True},
                    {"i": 1, "supported": True},
                    {"i": 2, "supported": False, "note": "about nights"},
                ]
            },
        }
    )
    url = "https://extension.umn.edu/early-blight"
    searcher = searcher_with({url: [TOMATO_PAGE]})
    trace: list = []
    built = await build_profile(
        llm, Settings(nebius_api_key="x"), searcher, "Solanum lycopersicum", ["tomato"], trace
    )
    by = {(f["field"], f["status"]) for f in built["facts"]}
    assert ("light", "verified") in by and ("toxic_to_pets", "verified") in by
    assert ("temp_min_c", "rejected") in by and ("water", "rejected") in by  # bad quote
    assert ("kc", "reference") in by and ("germination_soil_c", "reference") in by
    assert [s["url"] for s in built["sources"]] == [url]
    assert [(c.step, c.model) for c in trace] == [
        ("fact_extract", NEMOTRON_LIGHTNING),
        ("fact_verify", NEMOTRON_SUPER),
    ]
    view = profile_view({"key": "solanum lycopersicum", "status": "ready", "facts": built["facts"]})
    assert view["toxicity"] == {"toxic_to_pets": "toxic", "toxic_to_children": "unknown"}
    assert "water" in view["unsourced"] and "light" not in view["unsourced"]
    assert view["facts"]["light"][0]["source"]["url"] == url


async def test_failed_verification_shows_nothing_rather_than_unchecked_facts():
    llm = FakeLLM(
        {"fact_extract": EXTRACTED},
        failing_models={NEMOTRON_SUPER, "nvidia/Nemotron-3-Ultra-550b-a55b"},
    )
    built = await build_profile(
        llm,
        Settings(nebius_api_key="x"),
        searcher_with({"https://extension.umn.edu/early-blight": [TOMATO_PAGE]}),
        "Solanum lycopersicum",
        ["tomato"],
        [],
    )
    assert not [f for f in built["facts"] if f["status"] == "verified"]


def test_toxicity_disagreement_takes_the_more_toxic_answer():
    facts = [
        {
            "field": "toxic_to_pets",
            "value": "non_toxic",
            "status": "verified",
            "quote": "q",
            "source_title": "A",
            "source_url": "https://a.edu",
        },
        {
            "field": "toxic_to_pets",
            "value": "mildly_toxic",
            "status": "verified",
            "quote": "q",
            "source_title": "B",
            "source_url": "https://b.edu",
        },
        {
            "field": "toxic_to_children",
            "value": "toxic",
            "status": "rejected",
            "quote": "q",
            "source_title": "C",
            "source_url": "https://c.edu",
        },
    ]
    view = profile_view({"key": "x", "status": "ready", "facts": facts})
    assert view["toxicity"] == {"toxic_to_pets": "mildly_toxic", "toxic_to_children": "unknown"}


async def test_no_sources_means_no_extracted_facts_only_reference_ones():
    class Nothing(FakeSearcher):
        async def search_web(self, query, **kw):
            return [], 1

    llm = FakeLLM({})
    built = await build_profile(
        llm, Settings(nebius_api_key="x"), Nothing(), "Solanum lycopersicum", ["tomato"], []
    )
    assert {f["status"] for f in built["facts"]} == {"reference"} and llm.calls == []


async def test_toxicity_has_its_own_search_and_its_snippets_are_passages():
    llm = FakeLLM(
        {
            "fact_extract": {
                "facts": [
                    {
                        "field": "toxic_to_pets",
                        "value": "toxic",
                        "quote": "highly poisonous to people and pets",
                        "passage": "P2",
                    }
                ]
            },
            "fact_verify": {"verdicts": [{"i": 0, "supported": True}]},
        }
    )

    class Searcher(FakeSearcher):
        async def search_web(self, query, **kw):
            self.web_calls = getattr(self, "web_calls", []) + [query]
            if "toxic" in query:
                return [
                    {
                        "url": "https://www.aspca.org/foxglove",
                        "title": "ASPCA",
                        "content": FOXGLOVE_PAGE,
                        "score": 0.9,
                    }
                ], 1
            return [
                {
                    "url": "https://extension.example.edu/fox",
                    "title": "Ext",
                    "content": "care",
                    "score": 0.9,
                }
            ], 1

    s = Searcher()
    s.pages = {"https://extension.example.edu/fox": ["Foxglove grows in part shade."]}
    built = await build_profile(
        llm, Settings(nebius_api_key="x"), s, "Digitalis purpurea", ["foxglove"], []
    )
    assert len(s.web_calls) == 2 and "toxic" in s.web_calls[1]
    view = profile_view({"key": "digitalis purpurea", "status": "ready", "facts": built["facts"]})
    assert view["toxicity"]["toxic_to_pets"] == "toxic"
    assert [x["url"] for x in built["sources"]] == ["https://www.aspca.org/foxglove"]


# ---------- the species job ----------


async def species_runner(llm, searcher):
    db = SqliteDriver()
    await apply(db)
    repo = Repo(db)

    class Queue:
        sent: list = []

        async def send(self, jobs, delay_s=0):
            self.sent += [(j, delay_s) for j in jobs]

    leaf, _, _ = make_leaf(Settings(nebius_api_key="x"), {})
    leaf.llm, leaf.searcher = llm, searcher
    ctx = JobContext(repo, Queue(), Settings(nebius_api_key="x"), extra={"leaf": leaf})
    return JobRunner(ctx), repo, ctx.queue


async def test_species_job_builds_once_and_the_profile_is_shared():
    llm = FakeLLM(
        {
            "fact_extract": EXTRACTED,
            "fact_verify": {"verdicts": [{"i": 0, "supported": True}, {"i": 1, "supported": True}]},
        }
    )
    runner, repo, _ = await species_runner(
        llm, searcher_with({"https://extension.umn.edu/early-blight": [TOMATO_PAGE]})
    )
    await repo.register_species("solanum lycopersicum", "Solanum lycopersicum", ["tomato"])
    msg = JobMessage(
        kind="species_profile",
        key="species:solanum lycopersicum",
        payload={"key": "solanum lycopersicum"},
    )
    status, body = await runner.run(msg)
    assert status == 200 and body["result"]["verified"] == 2
    row = await repo.get_species("solanum lycopersicum")
    assert row["status"] == "ready" and row["sources"]
    again = await runner.run(
        JobMessage(
            kind="species_profile",
            key="species:solanum lycopersicum:2",
            payload={"key": "solanum lycopersicum"},
        )
    )
    assert again[1]["result"] == {"skipped": "already built"} and len(llm.calls) == 2


async def test_out_of_tavily_credits_waits_for_tomorrow():
    class Paused(FakeSearcher):
        async def search_web(self, query, **kw):
            raise SearchPaused("out of credits")

    runner, repo, queue = await species_runner(FakeLLM({}), Paused())
    await repo.register_species("ocimum basilicum", "Ocimum basilicum", ["basil"])
    _, body = await runner.run(
        JobMessage(
            kind="species_profile",
            key="species:ocimum basilicum",
            payload={"key": "ocimum basilicum"},
        )
    )
    assert body["result"]["why"] == "Tavily credits"
    assert queue.sent[-1][0].key.startswith("species:ocimum basilicum:")
    assert (await repo.get_species("ocimum basilicum"))["status"] == "queued"


# ---------- identification ----------

SEEN = {
    "plant_visible": True,
    "image_quality": "good",
    "features": ["palmately lobed leaves", "tall spike of tubular purple flowers"],
    "candidates": [
        {"scientific_name": "Digitalis purpurea", "common_name": "foxglove", "confidence": 0.55},
        {"scientific_name": "Symphytum officinale", "common_name": "comfrey", "confidence": 0.3},
    ],
}


async def test_super_reranks_only_the_vision_candidates_with_real_sources():
    llm = FakeLLM(
        {
            "identify": SEEN,
            "identify_check": {
                "ranking": [
                    {
                        "scientific_name": "Digitalis purpurea",
                        "probability": 0.85,
                        "consistent": True,
                        "reason": "Tubular flowers on one tall spike",
                        "source_ids": ["S1", "S99"],
                    },
                    {
                        "scientific_name": "Symphytum officinale",
                        "probability": 0.1,
                        "consistent": False,
                        "reason": "Comfrey flowers hang in curled clusters",
                    },
                    {"scientific_name": "Ricinus communis", "probability": 0.9},  # not a candidate
                ],
                "summary": "Foxglove fits.",
            },
        }
    )
    trace: list = []
    out = await identify(
        llm, Settings(nebius_api_key="x"), FakeSearcher(), "vendor/fake-vl", "aW1n", "", trace
    )
    assert [c["key"] for c in out["candidates"]] == ["digitalis purpurea", "symphytum officinale"]
    top = out["candidates"][0]
    assert top["probability"] == 0.85 and top["source_ids"] == ["S1"]  # S99 does not exist
    assert out["checked"] and out["confident"]
    assert [c.step for c in trace] == ["identify", "identify_check"]


async def test_if_the_check_fails_the_vision_ranking_stands_unconfirmed():
    llm = FakeLLM(
        {"identify": SEEN}, failing_models={NEMOTRON_SUPER, "nvidia/Nemotron-3-Ultra-550b-a55b"}
    )
    out = await identify(
        llm, Settings(nebius_api_key="x"), FakeSearcher(), "vendor/fake-vl", "aW1n", "", []
    )
    assert not out["checked"] and out["candidates"][0]["probability"] == 0.55
    assert not out["confident"]


async def test_no_plant_in_the_photo():
    llm = FakeLLM({"identify": {"plant_visible": False, "candidates": []}})
    out = await identify(
        llm, Settings(nebius_api_key="x"), FakeSearcher(), "vendor/fake-vl", "aW1n", "", []
    )
    assert out["candidates"] == [] and not out["plant_visible"] and len(llm.calls) == 1


# ---------- endpoints ----------


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (60, 120, 50)).save(buf, format="JPEG")
    return buf.getvalue()


@contextlib.contextmanager
def api(settings, responses):
    leaf, llm, searcher = make_leaf(settings, {**RESP, **responses})
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)

        class Queue:
            sent: list = []

            async def send(self, jobs, delay_s=0):
                self.sent += list(jobs)

        app.state.queue = Queue()
        yield client, llm
    app.state.leaf = app.state.deep = app.state.journal = None


CHECK = {
    "ranking": [
        {
            "scientific_name": "Digitalis purpurea",
            "probability": 0.8,
            "consistent": True,
            "reason": "fits",
        }
    ],
    "summary": "ok",
}


def test_identify_then_one_tap_confirm_into_a_garden(settings):
    with api(settings, {"identify": SEEN, "identify_check": CHECK}) as (client, _):
        r = client.post("/api/identify", files={"image": ("p.jpg", jpeg(), "image/jpeg")})
        assert r.status_code == 200
        d = r.json()
        assert d["candidates"][0]["key"] == "digitalis purpurea" and d["trace"]
        made = client.post("/api/gardens", json={"name": "g", "timezone": "UTC"}).json()
        c = client.post(
            f"/api/identify/{d['id']}/confirm",
            headers={"X-Leaf-Key": made["key"]},
            json={"scientific_name": "Digitalis purpurea", "garden_id": made["garden"]["id"]},
        )
        assert c.status_code == 200
        body = c.json()
        assert body["species"] == "digitalis purpurea" and body["from_candidates"]
        assert body["plant"]["species"] == "Digitalis purpurea"
        assert body["plant"]["common_name"] == "foxglove"
        sent = [m.key for m in app.state.queue.sent]
        assert any(k.startswith("species:digitalis purpurea") for k in sent)
        again = client.post(
            f"/api/identify/{d['id']}/confirm", json={"scientific_name": "Digitalis purpurea"}
        )
        assert again.status_code == 409  # one tap, once


def test_confirm_can_name_a_plant_the_models_missed(settings):
    with api(settings, {"identify": SEEN, "identify_check": CHECK}) as (client, _):
        d = client.post("/api/identify", files={"image": ("p.jpg", jpeg(), "image/jpeg")}).json()
        c = client.post(
            f"/api/identify/{d['id']}/confirm", json={"scientific_name": "Verbascum thapsus"}
        ).json()
        assert c["species"] == "verbascum thapsus" and not c["from_candidates"]
        assert client.get("/api/species/Verbascum thapsus").json()["status"] == "queued"
        assert c["plant"] is None


def test_confirm_into_a_garden_needs_its_key(settings):
    with api(settings, {"identify": SEEN, "identify_check": CHECK}) as (client, _):
        d = client.post("/api/identify", files={"image": ("p.jpg", jpeg(), "image/jpeg")}).json()
        made = client.post("/api/gardens", json={"name": "g", "timezone": "UTC"}).json()
        r = client.post(
            f"/api/identify/{d['id']}/confirm",
            json={"scientific_name": "Digitalis purpurea", "garden_id": made["garden"]["id"]},
        )
        assert r.status_code == 404


def test_identify_rejects_non_images_and_is_rate_limited(settings):
    with api(settings, {"identify": SEEN, "identify_check": CHECK}) as (client, _):
        bad = client.post("/api/identify", files={"image": ("x.txt", b"hello", "text/plain")})
        assert bad.status_code == 400
        app.state.identify_limiter = RateLimiter(per_ip_per_hour=1, daily_cap=100)
        ok = client.post("/api/identify", files={"image": ("p.jpg", jpeg(), "image/jpeg")})
        assert ok.status_code == 200
        assert (
            client.post(
                "/api/identify", files={"image": ("p.jpg", jpeg(), "image/jpeg")}
            ).status_code
            == 429
        )


def test_species_lookup_never_queues_a_build(settings):
    with api(settings, {}) as (client, _):
        r = client.get("/api/species/Digitalis purpurea")
        assert r.status_code == 404 and app.state.queue.sent == []
        assert client.get("/api/species/" + "x" * 130).status_code == 422


def test_adding_a_garden_plant_queues_its_profile_once(settings):
    with api(settings, {}) as (client, _):
        made = client.post("/api/gardens", json={"name": "g", "timezone": "UTC"}).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        for _ in range(2):
            client.post(
                f"/api/gardens/{gid}/plants",
                headers=h,
                json={"species": "Ocimum basilicum", "common_name": "basil"},
            )
        keys = [m.key for m in app.state.queue.sent if m.kind == "species_profile"]
        assert len(keys) == 1 and keys[0].startswith("species:ocimum basilicum")
        assert client.get("/api/species/Ocimum basilicum").json()["status"] == "queued"


def test_a_wrong_garden_key_does_not_use_up_the_identification(settings):
    with api(settings, {"identify": SEEN, "identify_check": CHECK}) as (client, _):
        d = client.post("/api/identify", files={"image": ("p.jpg", jpeg(), "image/jpeg")}).json()
        made = client.post("/api/gardens", json={"name": "g", "timezone": "UTC"}).json()
        body = {"scientific_name": "Digitalis purpurea", "garden_id": made["garden"]["id"]}
        app.state.queue.sent.clear()
        bad = client.post(
            f"/api/identify/{d['id']}/confirm", headers={"X-Leaf-Key": "no"}, json=body
        )
        assert bad.status_code == 404 and app.state.queue.sent == []
        good = client.post(
            f"/api/identify/{d['id']}/confirm", headers={"X-Leaf-Key": made["key"]}, json=body
        )
        assert good.status_code == 200


def test_identify_page_is_served(settings):
    with api(settings, {}) as (client, _):
        r = client.get("/identify")
        assert r.status_code == 200 and "Identify a plant" in r.text


def test_quotes_ignore_punctuation_but_not_words_or_numbers():
    page = "All parts of the plant are poisonous, possibly even deadly, if swallowed."
    assert quote_in_passage("all parts of the plant are poisonous possibly even deadly", page)
    assert not quote_in_passage("all parts of the plant are poisonous and deadly", page)
    assert quote_in_passage("pH of 5.8 to 6.8", "Grow in soil with a pH of 5.8 to 6.8.")
    assert not quote_in_passage("pH of 5.8 to 7.8", "Grow in soil with a pH of 5.8 to 6.8.")
    assert not quote_in_passage("of the plant", page)  # too short
    assert not quote_in_passage(
        "parts of the plant are", "Allparts of the plant are"
    )  # word bounds


# ---------- review fixes (2026-10-16) ----------


@pytest.mark.parametrize(
    ("field", "value", "passage", "quote", "ok"),
    [
        (
            "toxic_to_pets",
            "toxic",
            "Not toxic to cats and dogs, says the ASPCA.",
            "toxic to cats and dogs says the",
            False,
        ),  # reversed by "not" just before
        (
            "toxic_to_pets",
            "non_toxic",
            "It is non-toxic to cats and dogs.",
            "non-toxic to cats and dogs",
            True,
        ),
        (
            "toxic_to_pets",
            "non_toxic",
            "Leaves are toxic to cats and dogs.",
            "toxic to cats and dogs",
            False,
        ),
        (
            "toxic_to_children",
            "toxic",
            "All parts are poisonous if swallowed by children.",
            "All parts are poisonous if swallowed",
            True,
        ),
        (
            "toxic_to_children",
            "toxic",
            "Toxic to dogs, toxic to cats, toxic to horses.",
            "Toxic to dogs, toxic to cats",
            False,
        ),  # says nothing about people
    ],
)
def test_toxicity_quotes_must_agree_with_the_value_in_code(field, value, passage, quote, ok):
    passages = {"P1": {"url": "https://x.edu", "title": "t", "text": passage}}
    kept, rejected = check_extracted(
        [ExtractedFact(field=field, value=value, quote=quote, passage="P1")], passages
    )
    assert bool(kept) == ok, rejected


def test_reference_data_needs_the_right_plant():
    from backend.knowledge.profile import reference_facts

    assert reference_facts("Lathyrus odoratus", ["sweet pea"]) == []  # not the garden pea
    assert reference_facts("", ["sweet pea"]) == []
    assert {f["field"] for f in reference_facts("", ["pea"])} >= {"kc"}
    assert {f["field"] for f in reference_facts("Solanum lycopersicum", [])} >= {"kc"}


async def test_without_sources_an_identification_is_never_checked_or_confident():
    class Nothing(FakeSearcher):
        async def search_web(self, query, **kw):
            return [], 1

    llm = FakeLLM(
        {
            "identify": SEEN,
            "identify_check": {
                "ranking": [{"scientific_name": "Digitalis purpurea", "probability": 0.95}]
            },
        }
    )
    out = await identify(
        llm, Settings(nebius_api_key="x"), Nothing(), "vendor/fake-vl", "aW1n", "", []
    )
    assert not out["checked"] and not out["confident"]
    skipped = await identify(
        llm,
        Settings(nebius_api_key="x"),
        FakeSearcher(),
        "vendor/fake-vl",
        "aW1n",
        "",
        [],
        use_search=False,
    )
    assert skipped["sources"] == [] and not skipped["checked"]


async def test_a_candidate_the_check_left_out_cannot_win_on_the_vision_score():
    llm = FakeLLM(
        {
            "identify": SEEN,
            "identify_check": {
                "ranking": [{"scientific_name": "Symphytum officinale", "probability": 0.3}]
            },
        }
    )
    out = await identify(
        llm, Settings(nebius_api_key="x"), FakeSearcher(), "vendor/fake-vl", "aW1n", "", []
    )
    assert out["candidates"][0]["key"] == "symphytum officinale"
    assert out["candidates"][1]["probability"] == 0.0


async def test_the_daily_cap_counts_attempts_and_credits():
    from backend.knowledge import budget

    runner, repo, queue = await species_runner(FakeLLM({}), FakeSearcher())
    for i in range(5):
        await repo.register_species(f"plant {i}", "", [f"plant {i}"])
        await repo.start_species(f"plant {i}")
        await repo.fail_species(f"plant {i}", "boom")  # failures count too
    await repo.register_species("ocimum basilicum", "Ocimum basilicum", ["basil"])
    msg = JobMessage(
        kind="species_profile", key="species:ocimum basilicum", payload={"key": "ocimum basilicum"}
    )
    assert (await runner.run(msg))[1]["result"]["why"] == "daily species limit"
    runner2, repo2, _ = await species_runner(FakeLLM({}), FakeSearcher())
    await repo2.add_api_usage(budget.SERVICE, budget.KNOWLEDGE_CREDITS_PER_DAY - 2)
    await repo2.register_species("ocimum basilicum", "Ocimum basilicum", ["basil"])
    assert (await runner2.run(msg))[1]["result"]["why"] == "daily knowledge credits"


async def test_a_failed_build_is_recorded_not_retried_and_can_be_requeued_later(monkeypatch):
    import backend.data.repo as repo_mod

    class Boom(FakeSearcher):
        async def search_web(self, query, **kw):
            raise RuntimeError("tavily exploded")

    runner, repo, _ = await species_runner(FakeLLM({}), Boom())
    await repo.register_species("ocimum basilicum", "Ocimum basilicum", ["basil"])
    status, body = await runner.run(
        JobMessage(kind="species_profile", key="species:o", payload={"key": "ocimum basilicum"})
    )
    assert status == 200 and body["result"] == {"failed": "RuntimeError"}  # no queue retry
    assert (await repo.get_species("ocimum basilicum"))["status"] == "failed"
    assert not await repo.register_species("ocimum basilicum")  # cooling down
    now = repo_mod.now_s()
    monkeypatch.setattr(repo_mod, "now_s", lambda: now + 7 * 3600)
    assert await repo.register_species("ocimum basilicum")
    assert (await repo.get_species("ocimum basilicum"))["status"] == "queued"


def test_profile_text_is_stored_plain_not_prompt_escaped():
    assert clean_value("soil_ph", "acid (<6.0)") == "acid (<6.0)"
