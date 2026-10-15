"""Key-value store for shared caches: Workers KV in production, memory locally.

KV allows one write per second to the same key, and a write can take a minute or more to
reach other locations. It is for caches and seen-ids only, never counters or locks (those
live in D1).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Protocol
from urllib.parse import quote

from backend.data.driver import Bridge, DataError

KV_MIN_TTL_S = 60  # Workers KV rejects shorter expirations
MAX_KEY_BYTES = 512


class KV(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def put(self, key: str, value: str, ttl_s: int) -> None: ...
    async def delete(self, key: str) -> None: ...


def _check_key(key: str) -> None:
    if not key or len(key.encode()) > MAX_KEY_BYTES:
        raise DataError("KV key must be 1-512 bytes")


class MemoryKV:
    """Bounded TTL cache in this process."""

    def __init__(self, max_items: int = 5000):
        self.max_items = max_items
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def get(self, key: str) -> str | None:
        item = self._items.get(key)
        if item is None:
            return None
        if item[0] < time.time():
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return item[1]

    async def put(self, key: str, value: str, ttl_s: int) -> None:
        _check_key(key)
        self._items[key] = (time.time() + max(ttl_s, 1), value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    async def delete(self, key: str) -> None:
        self._items.pop(key, None)


class BridgeKV:
    """Workers KV through the Worker bridge: GET, PUT and DELETE /kv/<key>."""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    @staticmethod
    def _path(key: str) -> str:
        _check_key(key)
        return "/kv/" + quote(key, safe="")

    async def get(self, key: str) -> str | None:
        r = await self.bridge.call("GET", self._path(key))
        return None if r.status_code == 404 else r.text

    async def put(self, key: str, value: str, ttl_s: int) -> None:
        await self.bridge.call(
            "PUT",
            self._path(key),
            params={"ttl": max(ttl_s, KV_MIN_TTL_S)},
            content=value.encode(),
        )

    async def delete(self, key: str) -> None:
        await self.bridge.call("DELETE", self._path(key))
