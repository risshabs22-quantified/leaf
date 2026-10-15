"""Plant journal: finished reports, shareable by an unguessable link, with check-ins.

A journal entry is what a gardener keeps: the report (Quick or Deep), small thumbnails of the
photos (never the full uploads), and later check-ins. Entries expire after 30 days unless
pinned (scripts/pin_case.py keeps a demo link alive). Storage sits behind JournalStore:
SQLite locally (default), memory for tests, or an Object Storage bucket in production.
Nothing here is logged: no images, no notes.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal, Protocol

from PIL import Image
from pydantic import BaseModel, Field

from backend.case import new_case_id
from backend.schemas import PlanStep

THUMB_SIDE = 512
MAX_CHECKINS = 20


class CheckIn(BaseModel):
    at: float = Field(default_factory=time.time)
    status: Literal["spreading", "stable", "recovering", "unclear"] = "unclear"
    change: str = ""
    advice: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)  # steps dropped for the garden's preferences
    cautions: list[str] = Field(default_factory=list)
    escalate: bool = False
    escalate_reason: str = ""
    photo: str = ""  # thumbnail of the check-in photo


class JournalEntry(BaseModel):
    id: str = Field(default_factory=new_case_id)
    created: float = Field(default_factory=time.time)
    expires: float = 0.0
    mode: Literal["quick", "deep"] = "quick"
    report: dict[str, Any] = Field(default_factory=dict)
    photos: list[str] = Field(default_factory=list)  # thumbnails, base64 JPEG
    labels: list[str] = Field(default_factory=list)
    checkins: list[CheckIn] = Field(default_factory=list)
    owner_token: str = Field(default_factory=lambda: secrets.token_urlsafe(16))

    def latest_photo(self) -> str | None:
        for c in reversed(self.checkins):
            if c.photo:
                return c.photo
        return self.photos[0] if self.photos else None


def thumbnail(b64: str, side: int = THUMB_SIDE) -> str:
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    return base64.b64encode(out.getvalue()).decode()


class JournalStore(Protocol):
    async def get(self, entry_id: str) -> JournalEntry | None: ...
    async def put(self, entry: JournalEntry) -> None: ...
    async def purge(self) -> int: ...


class MemoryJournal:
    """For tests and for running without a disk; lost on restart."""

    def __init__(self, max_entries: int = 1000):
        self._entries: dict[str, JournalEntry] = {}
        self.max_entries = max_entries

    async def get(self, entry_id: str) -> JournalEntry | None:
        e = self._entries.get(entry_id)
        return e if e and e.expires > time.time() else None

    async def put(self, entry: JournalEntry) -> None:
        self._entries[entry.id] = entry
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))

    async def purge(self) -> int:
        now = time.time()
        old = [k for k, e in self._entries.items() if e.expires <= now]
        for k in old:
            del self._entries[k]
        return len(old)


class SQLiteJournal:
    """One table, one JSON document per entry. sqlite3 calls run in a worker thread."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS journal "
                "(id TEXT PRIMARY KEY, expires REAL NOT NULL, data TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS journal_expires ON journal (expires)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def _get(self, entry_id: str) -> JournalEntry | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM journal WHERE id = ? AND expires > ?", (entry_id, time.time())
            ).fetchone()
        return JournalEntry.model_validate(json.loads(row[0])) if row else None

    def _put(self, entry: JournalEntry) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO journal (id, expires, data) VALUES (?, ?, ?)",
                (entry.id, entry.expires, entry.model_dump_json()),
            )

    def _purge(self) -> int:
        with self._connect() as db:
            return db.execute("DELETE FROM journal WHERE expires <= ?", (time.time(),)).rowcount

    async def get(self, entry_id: str) -> JournalEntry | None:
        return await asyncio.to_thread(self._get, entry_id)

    async def put(self, entry: JournalEntry) -> None:
        await asyncio.to_thread(self._put, entry)

    async def purge(self) -> int:
        return await asyncio.to_thread(self._purge)


class S3Journal:
    """One JSON object per entry in an S3-compatible bucket (Nebius Object Storage).

    Credentials, endpoint and region come from the standard AWS_* environment variables, so
    none of them live in code. Each object carries its expiry as metadata: expired entries are
    deleted when someone asks for them, and purge() (run at startup) deletes the rest. Pinned
    entries (scripts/pin_case.py) simply carry a later expiry.
    """

    PREFIX = "journal/"
    MISSING = {"NoSuchKey", "404", "NotFound", "AccessDenied", "403"}

    def __init__(self, bucket: str, client: Any = None):
        if client is None:
            import boto3  # only needed when the bucket is configured

            client = boto3.client("s3")
        self.bucket, self.client = bucket, client

    def _key(self, entry_id: str) -> str:
        return f"{self.PREFIX}{entry_id}.json"

    def _missing(self, exc: Exception) -> bool:
        # Without ListBucket permission, S3 answers a missing key with AccessDenied.
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in self.MISSING

    def _get(self, entry_id: str) -> JournalEntry | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(entry_id))
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            if self._missing(exc):
                return None
            raise
        entry = JournalEntry.model_validate_json(obj["Body"].read())
        if entry.expires <= time.time():
            self.client.delete_object(Bucket=self.bucket, Key=self._key(entry_id))
            return None
        return entry

    def _put(self, entry: JournalEntry) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._key(entry.id),
            Body=entry.model_dump_json().encode(),
            ContentType="application/json",
            Metadata={"expires": str(int(entry.expires))},
        )

    def _purge(self) -> int:
        now, removed, token = time.time(), 0, None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self.PREFIX}
            if token:
                kw["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kw)
            for obj in page.get("Contents", []):
                head = self.client.head_object(Bucket=self.bucket, Key=obj["Key"])
                expires = float((head.get("Metadata") or {}).get("expires", "0") or 0)
                if expires <= now:
                    self.client.delete_object(Bucket=self.bucket, Key=obj["Key"])
                    removed += 1
            if not page.get("IsTruncated"):
                return removed
            token = page.get("NextContinuationToken")

    async def get(self, entry_id: str) -> JournalEntry | None:
        if not entry_id or not all(c.isalnum() or c in "-_" for c in entry_id):
            return None  # ids are url-safe tokens; anything else cannot be a key we wrote
        return await asyncio.to_thread(self._get, entry_id)

    async def put(self, entry: JournalEntry) -> None:
        await asyncio.to_thread(self._put, entry)

    async def purge(self) -> int:
        return await asyncio.to_thread(self._purge)


def open_journal(path: str, bucket: str = "") -> JournalStore:
    """The configured store: a bucket when LEAF_JOURNAL_BUCKET is set, else SQLite or memory."""
    if bucket:
        return S3Journal(bucket)
    return MemoryJournal() if path in ("", "memory") else SQLiteJournal(path)


SHARED_FIELDS = (
    "case_id",
    "mode",
    "status",
    "notice",
    "observation",
    "diagnosis",
    "differential",
    "plan",
    "sources",
    "confidence",
    "critic",
    "summary",
    "low_confidence",
    "claim_flags",
    "handoff",
    "verdict",
    "verdict_reason",
    "auto",
)


def public_view(entry: JournalEntry) -> dict[str, Any]:
    """What a share link shows. A whitelist, so new report fields stay private by default;
    the gardener's notes and answers (kept in the lab handoff's history) are never shared."""
    report = {k: v for k, v in entry.report.items() if k in SHARED_FIELDS}
    if isinstance(report.get("handoff"), dict):
        report["handoff"] = {k: v for k, v in report["handoff"].items() if k != "history"}
    return {
        "id": entry.id,
        "created": entry.created,
        "expires": entry.expires,
        "mode": entry.mode,
        "report": report,
        "photos": entry.photos,
        "labels": entry.labels,
        "checkins": [c.model_dump() for c in entry.checkins],
    }
