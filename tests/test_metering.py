"""The meter and the spend guard: every call recorded, caps enforced before anything is sent."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from fastapi.testclient import TestClient

from backend.app import RateLimiter, app
from backend.config import NEMOTRON_LIGHTNING, NEMOTRON_SUPER, NEMOTRON_ULTRA
from backend.context import RequestContext, request_scope, task_scope
from backend.data.driver import DataError, SqliteDriver
from backend.data.migrate import apply
from backend.data.repo import Repo, utc_day
from backend.journal import MemoryJournal
from backend.metering import Meter, MeteredLLM
from backend.models import ModelError, SpendCapReached, complete_json
from backend.schemas import Diagnosis, ModelCall
from tests.conftest import DIAG_HIGH, FakeLLM, make_leaf
from tests.test_agent import RESP


async def new_repo() -> Repo:
    db = SqliteDriver()
    await apply(db)
    return Repo(db)


MSG = [{"role": "user", "content": "hi"}]


async def test_calls_are_recorded_with_task_trace_tokens_and_cost():
    meter = Meter(cap_usd=1.0)
    llm = MeteredLLM(FakeLLM({"repair": "ok"}), meter)
    with request_scope(RequestContext(trace_id="t-1")) as ctx, task_scope("chat"):
        await llm.complete(model="m", messages=MSG)
    row = meter.pending[0]
    assert (row.task, row.model, row.trace_id) == ("chat", "m", "t-1")
    assert (row.prompt_tokens, row.completion_tokens, row.cost_usd, row.ok) == (
        100,
        50,
        0.0005,
        True,
    )
    assert meter.spent == pytest.approx(0.0005) and ctx.model_calls == 1


async def test_a_failed_call_is_recorded_with_what_it_was_billed():
    class Billed(FakeLLM):
        async def complete(self, *, model, messages, **kw):
            raise ModelError("truncated", cost_usd=0.002)

    meter = Meter(cap_usd=1.0)
    with pytest.raises(ModelError):
        await MeteredLLM(Billed({}), meter).complete(model="m", messages=MSG)
    assert meter.pending[0].ok is False and meter.spent == pytest.approx(0.002)


async def test_at_the_cap_nothing_is_sent():
    inner = FakeLLM({"repair": "ok"})
    meter = Meter(cap_usd=0.01)
    meter.spent = 0.01
    with pytest.raises(SpendCapReached), task_scope("diagnose"):
        await MeteredLLM(inner, meter).complete(model="m", messages=MSG)
    assert inner.calls == [] and meter.mode() == "rules_only"


async def test_brownout_pauses_optional_tasks_only():
    meter = Meter(cap_usd=1.0, brownout=0.8)
    meter.spent = 0.8
    assert meter.mode() == "brownout"
    assert not meter.allows("front_door") and not meter.allows("care_wording")
    assert meter.allows("diagnose") and meter.allows("plan_verify")


async def test_a_new_utc_day_resets_the_spend():
    now = [1_760_400_000.0]
    meter = Meter(cap_usd=1.0, clock=lambda: now[0])
    meter.record("chat", "m", 1, 1, 2.0, 1)
    assert meter.mode() == "rules_only"
    now[0] += 86400
    assert meter.mode() == "normal" and meter.spent == 0


async def test_spend_cap_stops_a_fallback_chain_at_once():
    meter = Meter(cap_usd=0.0001)
    meter.spent = 1
    inner = FakeLLM({"diagnose": DIAG_HIGH})
    trace: list[ModelCall] = []
    with pytest.raises(SpendCapReached):
        await complete_json(
            MeteredLLM(inner, meter),
            step="diagnose",
            models=[NEMOTRON_ULTRA, NEMOTRON_SUPER, NEMOTRON_LIGHTNING],
            messages=MSG,
            schema=Diagnosis,
            trace=trace,
        )
    assert [c.note for c in trace] == ["spend cap"] and inner.calls == []


async def test_flush_writes_rows_and_restore_brings_spend_back():
    repo = await new_repo()
    meter = Meter(cap_usd=1.0, repo=repo)
    meter.record("chat", "m", 10, 5, 0.25, 7)
    meter.cache_event("front_door", hit=True)
    meter.active("g:abc")
    meter.active("g:abc")
    await meter.flush()
    assert meter.pending == [] and await repo.spend_on(utc_day()) == pytest.approx(0.25)
    raw = await repo.usage(utc_day(), utc_day())
    assert raw["active"][0]["active"] == 1 and raw["cache"][0]["hits"] == 1
    fresh = Meter(cap_usd=1.0, repo=repo)  # after a container restart
    await fresh.restore()
    assert fresh.spent == pytest.approx(0.25)


async def test_a_failed_flush_keeps_the_rows_for_next_time():
    class Down(Repo):
        async def record_usage(self, *a, **k):
            raise DataError("bridge down")

    meter = Meter(cap_usd=1.0, repo=Down(SqliteDriver()))
    meter.record("chat", "m", 1, 1, 0.1, 1)
    meter.cache_event("front_door", hit=False)
    await meter.flush()
    assert len(meter.pending) == 1 and dict(meter.cache) == {(meter.day, "front_door"): [0, 1]}


async def test_visitor_ids_are_salted_per_day():
    repo = await new_repo()
    now = [1_760_400_000.0]
    meter = Meter(cap_usd=1.0, repo=repo, clock=lambda: now[0])
    a = await meter.visitor("203.0.113.9")
    assert a.startswith("v:") and a == await meter.visitor("203.0.113.9")
    assert a != await meter.visitor("203.0.113.10")
    now[0] += 86400
    assert a != await meter.visitor("203.0.113.9")


# ---------- through the app ----------


@contextlib.contextmanager
def api(settings, responses):
    leaf, llm, _ = make_leaf(settings, responses)
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        yield client, llm
    app.state.leaf = app.state.deep = app.state.journal = None


def test_diagnosis_at_the_spend_cap_is_a_clear_503(settings):
    with api(settings, RESP) as (client, llm):
        app.state.meter.spent = app.state.meter.cap_usd
        r = client.post("/api/diagnose", data={"notes": "brown rings on tomato leaves"})
        assert r.status_code == 503 and "budget" in r.json()["detail"]
        assert llm.calls == []


def test_usage_endpoint_reports_cost_per_active_user_and_no_personal_data(settings):
    with api(settings, {**RESP, "front_door": {"intent": "doctor"}}) as (client, _):
        r = client.post("/api/diagnose", data={"notes": "brown rings on tomato leaves"})
        assert r.status_code == 200
        client.post("/api/route", json={"text": "spots on my squash"})
        client.post("/api/route", json={"text": "spots on my squash"})  # cached
        u = client.get("/api/ops/usage").json()
        assert u["calls"] >= 3 and u["cost_usd"] > 0
        assert {"diagnose", "plan", "front_door"} <= set(u["tasks"])
        assert u["tasks"]["diagnose"]["models"] == {NEMOTRON_ULTRA: 1}
        assert u["caches"]["front_door"] == {"hits": 1, "misses": 1}
        assert u["active_user_days"] == 1  # one visitor, hashed
        assert u["cost_per_active_user_day"] == pytest.approx(u["cost_usd"])
        assert u["guard"]["mode"] == "normal" and u["routes"]["front_door"]["optional"]
        text = r.text + str(u)
        assert "testclient" not in str(u) and "203." not in text


def test_usage_endpoint_is_rate_limited_like_the_platform_one(settings):
    with api(settings, RESP) as (client, _):
        app.state.ops_limiter = RateLimiter(per_ip_per_hour=2, daily_cap=100)
        codes = [client.get("/api/ops/usage").status_code for _ in range(3)]
        assert codes == [200, 200, 429]


def test_health_shows_the_front_door_model_and_platform(settings):
    with api(settings, RESP) as (client, _):
        h = client.get("/api/health").json()
        assert h["models"]["front_door"] == "nvidia/Nemotron-3_5-Lightning"
        assert h["models"]["background_screen"] == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
        assert h["platform"]["data"] == "sqlite" and h["platform"]["schema_pending"] == []
        assert h["platform"]["guard"]["mode"] == "normal"


async def test_a_cancelled_call_is_counted_at_the_routes_typical_cost():
    class Slow(FakeLLM):
        async def complete(self, *, model, messages, **kw):
            await asyncio.sleep(10)

        def cost(self, model, p, c):
            return p * 1e-6 + c * 3e-6

    meter = Meter(cap_usd=1.0)
    with task_scope("diagnose"), pytest.raises(TimeoutError):
        await asyncio.wait_for(MeteredLLM(Slow({}), meter).complete(model="m", messages=MSG), 0.01)
    row = meter.pending[0]
    assert row.ok is False and row.cost_usd == pytest.approx(3000e-6 + 2000 * 3e-6)


async def test_until_spend_is_restored_optional_tasks_pause():
    class Down(Repo):
        async def spend_on(self, day):
            raise DataError("bridge down")

    meter = Meter(cap_usd=1.0, repo=Down(SqliteDriver()))
    await meter.restore()
    assert meter.mode() == "brownout" and not meter.allows("front_door")
    meter.repo = await new_repo()
    await meter.flush()  # retries the restore
    assert meter.mode() == "normal"


async def test_a_new_day_counts_returning_users_again():
    now = [1_760_400_000.0]
    meter = Meter(cap_usd=1.0, clock=lambda: now[0])
    meter.active("v:1")
    now[0] += 86400
    meter.active("v:1")
    assert len(meter.actors_pending) == 2


async def test_model_users_are_counted_when_the_call_is_recorded():
    meter = Meter(cap_usd=1.0)
    with request_scope(RequestContext(visitor="v:abc")), task_scope("chat"):
        await MeteredLLM(FakeLLM({"repair": "ok"}), meter).complete(model="m", messages=MSG)
    assert {a for _, a in meter.actors_pending} == {"v:abc"}


def test_unpriced_models_are_charged_at_the_dearest_known_price():
    from backend.config import Settings
    from backend.models import TokenFactory

    tf = TokenFactory(Settings(nebius_api_key="x"))
    assert tf.cost("someone/new-model", 1000, 1000) == max(
        tf.cost(m, 1000, 1000) for m in tf.prices
    )
