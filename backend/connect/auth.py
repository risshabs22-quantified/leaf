"""API keys: who is calling, what they may do, and how often.

A key (`Authorization: Bearer lf_...`) acts for one garden. Its scopes say what it may do:
`read` (the garden, Today, the season plan), `readings` (post soil moisture), `tools`
(diagnose, identify, weather risk, start a season plan). Limits are per key and counted in D1:
an hour, a day, and a smaller daily budget for calls that can spend model credits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request

from backend.context import REQUEST
from backend.data.driver import DataError
from backend.data.repo import ApiKey, Garden

log = logging.getLogger("leaf.connect")

SCOPES = ("read", "readings", "tools")
MAX_KEYS_PER_GARDEN = 5
BEARER = {"WWW-Authenticate": 'Bearer realm="leaf"'}


@dataclass
class Caller:
    """Who is calling a Connect surface, and for which garden."""

    garden: Garden
    identity: str  # for the shared limiters: "garden:<id>"
    scopes: tuple[str, ...] = SCOPES
    key: ApiKey | None = None


def _wait(until: datetime) -> dict[str, str]:
    return {"Retry-After": str(max(1, int((until - datetime.now(UTC)).total_seconds())))}


async def authenticate(request: Request, scope: str, model: bool = False) -> Caller:
    """The caller for a request with a Leaf API key. Raises 401 (no or bad key), 403 (the key
    lacks the scope) or 429 (over its limits, with Retry-After). `model` marks a call that
    may spend model credits, which has its own smaller daily budget."""
    repo = request.app.state.data.repo
    ip_limiter = request.app.state.garden_use_limiter
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            401, "Send your Leaf API key as 'Authorization: Bearer lf_...'.", headers=BEARER
        )
    try:
        key = await repo.api_key_by_secret(token)
        garden = await repo.get_garden(key.garden_id) if key else None
    except DataError as exc:
        log.error("api key lookup failed: %s", exc)
        raise HTTPException(503, "Leaf's storage is unavailable. Please try again.") from exc
    if key is None or garden is None:
        from backend.app import client_ip

        # Only failures count against the address, so guessing keys is slow and a busy
        # integration behind one address is not.
        ip_limiter.check(client_ip(request, request.app.state.leaf.settings.trusted_proxy_hops))
        raise HTTPException(401, "That API key is not valid, or it was revoked.", headers=BEARER)
    if scope not in key.scopes:
        raise HTTPException(403, f"This API key does not have the '{scope}' scope.")
    now = datetime.now(UTC)
    counts = await repo.take_api_hit(key.id, model, now)
    hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if counts["hour"] > key.per_hour:
        raise HTTPException(
            429, f"This API key may make {key.per_hour} calls an hour.", headers=_wait(hour_end)
        )
    if counts["day"] > key.per_day:
        raise HTTPException(
            429, f"This API key may make {key.per_day} calls a day.", headers=_wait(day_end)
        )
    if model and counts.get("model", 0) > key.model_per_day:
        raise HTTPException(
            429,
            f"This API key may make {key.model_per_day} calls a day that use Leaf's models.",
            headers=_wait(day_end),
        )
    ctx = REQUEST.get()
    if ctx is not None:
        ctx.actor = f"g:{garden.id}"
    # The shared limiters are per garden, so many keys cannot multiply a garden's share.
    return Caller(garden, f"garden:{garden.id}", tuple(key.scopes), key)
