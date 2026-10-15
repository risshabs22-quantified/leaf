"""Shared cache: compute once, serve every user. Memory first, then Workers KV.

Keys are namespaced ("front_door", later "forecast", "species" ...) and hit rates per
namespace go to the meter, so /api/ops/usage shows what caching saves. A KV failure is a
miss, never an error.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.data.driver import DataError
from backend.data.kv import KV, MemoryKV
from backend.metering import Meter

log = logging.getLogger("leaf.cache")

LOCAL_TTL_S = 300  # memory copy of a KV value; KV itself holds the real TTL


class SharedCache:
    def __init__(self, kv: KV | None, meter: Meter | None = None):
        self.kv = kv
        self.meter = meter
        self.local = MemoryKV(max_items=2000)

    async def get_json(self, namespace: str, key: str) -> Any | None:
        full = f"{namespace}:{key}"
        raw = await self.local.get(full)
        if raw is None and self.kv is not None:
            try:
                raw = await self.kv.get(full)
            except DataError as exc:
                log.warning("cache get %s failed: %s", namespace, exc)
            if raw is not None:
                await self.local.put(full, raw, LOCAL_TTL_S)
        value = None
        if raw is not None:
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
        if self.meter is not None:
            self.meter.cache_event(namespace, value is not None)
        return value

    async def put_json(self, namespace: str, key: str, value: Any, ttl_s: int) -> None:
        full = f"{namespace}:{key}"
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self.local.put(full, raw, min(ttl_s, LOCAL_TTL_S))
        if self.kv is not None:
            try:
                await self.kv.put(full, raw, ttl_s)
            except DataError as exc:
                log.warning("cache put %s failed: %s", namespace, exc)
