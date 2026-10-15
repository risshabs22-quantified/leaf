"""Platform data: D1 (SQL), KV (caches) and the job queue, behind small interfaces.

Production reaches all three through the Worker bridge at LEAF_PLATFORM_URL; locally and in
tests they are SQLite, memory and an in-process queue, with the same SQL and migrations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.config import Settings
from backend.data.driver import Bridge, D1Driver, DataError, Driver, SqliteDriver
from backend.data.kv import KV, BridgeKV, MemoryKV
from backend.data.migrate import apply, pending
from backend.data.repo import Repo

log = logging.getLogger("leaf.data")


@dataclass
class PlatformData:
    db: Driver
    repo: Repo
    kv: KV
    bridge: Bridge | None = None
    schema_pending: tuple[str, ...] = ()


async def open_data(settings: Settings) -> PlatformData:
    """Connect to the configured stores. Locally, migrations are applied here; on D1 they are
    applied at deploy time, and this only reports any that are missing."""
    if settings.platform_url:
        bridge = Bridge(settings.platform_url)
        db: Driver = D1Driver(bridge)
        try:
            missing = tuple(await pending(db))
        except DataError as exc:
            # Never let the data layer stop Leaf starting: diagnosis works without it, and
            # garden endpoints answer 503 until the bridge is back.
            log.error("platform data unavailable at startup: %s", exc)
            missing = ("unknown: bridge unavailable",)
        return PlatformData(db, Repo(db), BridgeKV(bridge), bridge, missing)
    path = ":memory:" if settings.platform_db in ("", "memory") else settings.platform_db
    sqlite = SqliteDriver(path)
    await apply(sqlite)
    return PlatformData(sqlite, Repo(sqlite), MemoryKV())
