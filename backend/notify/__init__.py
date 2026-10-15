"""The notifications service: one place decides whether, when and how a garden hears from
Leaf. Channels the gardener chose; dedupe (the same reminder or alert never twice); quiet
hours (sent when they end, through the job queue); a daily rate limit. Urgent alerts (frost,
freeze, heat, wind, flood) skip both: a freeze warning held until morning comes too late.
Web Push now, Telegram in M6.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from backend.data.repo import Garden, NotificationSettings, Repo, now_s

log = logging.getLogger("leaf.notify")


class Pusher(Protocol):
    async def send(self, sub: dict[str, Any], payload: dict[str, Any]) -> Any: ...


class Queue(Protocol):
    async def send(self, jobs: Any, delay_s: int = 0) -> None: ...


def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def quiet_until(settings: NotificationSettings, tz: str, now: datetime) -> datetime | None:
    """When quiet hours end, if `now` is inside them (they may cross midnight), else None."""
    local = now.astimezone(ZoneInfo(tz))
    start, end = _hm(settings.quiet_start), _hm(settings.quiet_end)
    t = local.time()
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if not inside or start == end:
        return None
    day = local.date() if t < end else local.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=local.tzinfo)


async def notify(
    repo: Repo,
    queue: Queue,
    pusher: Pusher | None,
    garden: Garden,
    *,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str,
    urgent: bool = False,
    url: str = "/today",
    now: datetime | None = None,
    respect_quiet: bool = True,
) -> str:
    """Decide and, when it is time, send. Returns what happened: sent, queued (quiet hours),
    duplicate, no_channels, daily_limit, no_subscribers, failed."""
    from backend.jobs import JobMessage  # jobs imports this module

    settings = await repo.get_notifications(garden.id)
    if "push" not in settings.channels:
        return "no_channels"
    nid = await repo.create_notification(garden.id, dedupe_key, kind, title, body, url, urgent)
    if nid is None:
        return "duplicate"
    now = now or datetime.now(ZoneInfo("UTC"))
    until = quiet_until(settings, garden.timezone, now) if respect_quiet and not urgent else None
    if until is not None:
        delay = int((until - now).total_seconds()) + 30
        await queue.send(
            [JobMessage(kind="notify_send", key=f"notify_send:{nid}", payload={"id": nid})],
            delay_s=min(delay, 24 * 3600),
        )
        return "queued"
    return await deliver(repo, pusher, garden, nid, now)


async def deliver(
    repo: Repo, pusher: Pusher | None, garden: Garden, nid: str, now: datetime | None = None
) -> str:
    row = await repo.get_notification(nid)
    if row is None or row["status"] in ("sent", "skipped"):
        return "duplicate"
    settings = await repo.get_notifications(garden.id)
    now = now or datetime.now(ZoneInfo("UTC"))
    if not row["urgent"]:
        day_start = int(
            datetime.combine(
                now.astimezone(ZoneInfo(garden.timezone)).date(),
                time(),
                tzinfo=ZoneInfo(garden.timezone),
            ).timestamp()
        )
        if await repo.sent_since(garden.id, day_start, urgent=False) >= settings.max_per_day:
            await repo.mark_notification(nid, "skipped", "daily limit")
            return "daily_limit"
    subs = await repo.list_push(garden.id)
    if pusher is None or not subs:
        await repo.mark_notification(nid, "skipped", "no subscribers")
        return "no_subscribers"
    payload = {
        "title": row["title"],
        "body": row["body"],
        "url": row["url"],
        "tag": row["dedupe_key"][:64],
        "urgent": bool(row["urgent"]),
    }
    delivered = 0
    for sub in subs:
        result = await pusher.send(sub, payload)
        await repo.push_result(sub["id"], result.ok, result.gone)
        delivered += int(result.ok)
    status = "sent" if delivered else "failed"
    await repo.mark_notification(nid, status, f"{delivered}/{len(subs)} devices")
    log.info("notification %s %s to %d/%d devices", row["kind"], status, delivered, len(subs))
    return status


def unix(dt: datetime) -> int:
    return int(dt.timestamp())


__all__ = ["deliver", "notify", "quiet_until", "now_s", "unix"]
