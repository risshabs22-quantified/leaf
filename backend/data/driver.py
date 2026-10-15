"""SQL drivers: D1 in production (through the Worker bridge), SQLite locally and in tests.

D1 is SQLite, so one set of SQL statements and migrations serves both. Only
`backend/data/repo.py` writes SQL; everything else uses the repository.

A batch is atomic on both drivers (D1 runs a batch as one transaction), so use it for any
group of writes that must land together. D1 has no BEGIN/COMMIT from a client.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

Statement = tuple[str, Sequence[Any]]

# D1 allows 100 bound parameters per statement, and the Worker bridge takes at most 200
# statements per batch; SQLite keeps to the same rules so tests catch what D1 would refuse.
MAX_PARAMS = 100
MAX_STATEMENTS = 200
CHUNK = 100  # statements per batch when a write is split (see write_chunked)


class DataError(RuntimeError):
    """The database could not run a statement (network, constraint or SQL error)."""


@dataclass
class Result:
    rows: list[dict[str, Any]] = field(default_factory=list)
    changes: int = 0


class Driver(Protocol):
    name: str

    async def batch(self, statements: Sequence[Statement]) -> list[Result]: ...


async def query(db: Driver, sql: str, *params: Any) -> list[dict[str, Any]]:
    return (await db.batch([(sql, params)]))[0].rows


async def write_chunked(db: Driver, statements: Sequence[Statement]) -> list[Result]:
    """Large writes in batches of CHUNK statements. Each batch is atomic; the whole is not, so
    use it only for writes that are safe to land partly (INSERT OR IGNORE, idempotent rows)."""
    out: list[Result] = []
    for i in range(0, len(statements), CHUNK):
        out += await db.batch(statements[i : i + CHUNK])
    return out


async def execute(db: Driver, sql: str, *params: Any) -> int:
    """Run one write; returns the number of rows it changed."""
    return (await db.batch([(sql, params)]))[0].changes


def _check(statements: Sequence[Statement]) -> None:
    if len(statements) > MAX_STATEMENTS:
        raise DataError(f"{len(statements)} statements in one batch (max {MAX_STATEMENTS})")
    for sql, params in statements:
        if len(params) > MAX_PARAMS:
            raise DataError(f"{len(params)} parameters (D1 allows {MAX_PARAMS}): {sql[:60]}")


class SqliteDriver:
    """SQLite in a worker thread. ":memory:" keeps everything in this process."""

    name = "sqlite"

    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")  # D1 enforces foreign keys by default
        self._lock = threading.Lock()

    def _run(self, statements: Sequence[Statement]) -> list[Result]:
        with self._lock:
            out: list[Result] = []
            self._conn.execute("BEGIN")
            try:
                for sql, params in statements:
                    cur = self._conn.execute(sql, tuple(params))
                    rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                    out.append(Result(rows=rows, changes=max(cur.rowcount, 0)))
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise DataError(str(exc)) from exc
            self._conn.execute("COMMIT")
            return out

    async def batch(self, statements: Sequence[Statement]) -> list[Result]:
        _check(statements)
        return await asyncio.to_thread(self._run, statements)

    def executescript(self, sql: str) -> None:
        """Migrations only: several statements in one transaction."""
        with self._lock:
            try:
                self._conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise DataError(str(exc)) from exc


class Bridge:
    """HTTP client for the Worker's data bridge (cloudflare/src/index.ts).

    In production the container's requests to LEAF_PLATFORM_URL never leave Cloudflare: the
    Worker intercepts them and answers with its D1, KV and Queue bindings. Nobody outside the
    container can reach these handlers, so no token is involved.
    """

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            r = await self.client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise DataError(f"bridge {path}: {type(exc).__name__}") from exc
        if r.status_code >= 500 or r.status_code in (400, 401, 403):
            detail = r.text[:300] if r.headers.get("content-type", "").startswith("app") else ""
            raise DataError(f"bridge {path}: HTTP {r.status_code} {detail}".strip())
        return r


class D1Driver:
    """D1 through the bridge: POST /d1 runs the statements as one D1 batch."""

    name = "d1"

    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    async def batch(self, statements: Sequence[Statement]) -> list[Result]:
        _check(statements)
        body = {"statements": [{"sql": sql, "params": list(params)} for sql, params in statements]}
        r = await self.bridge.call("POST", "/d1", json=body)
        try:
            data = r.json()
            return [
                Result(rows=list(res.get("rows") or []), changes=int(res.get("changes") or 0))
                for res in data["results"]
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise DataError("bridge /d1: malformed reply") from exc
