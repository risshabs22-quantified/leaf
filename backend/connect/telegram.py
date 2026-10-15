"""The Telegram bot: a garden's day, photo diagnosis and notifications in a chat.

Telegram posts each update to /api/telegram/webhook with the secret token that Leaf gave
`setWebhook`; a request without it is refused. The bot answers private chats only. A chat is
linked to a garden by a one-time code from the garden's Connect page (`/start <code>`), so no
one reaches a garden by guessing; until then the bot only explains how to link.

Updates are acknowledged at once and handled in the background, because a diagnosis takes
longer than Telegram waits and Telegram retries a slow webhook. Replays are dropped by update id.
Every message is plain text: nothing a model or a web page wrote is ever parsed as markup.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend.connect import tools
from backend.connect.auth import Caller
from backend.context import RequestContext, request_scope
from backend.notify.telegram import TelegramApi, TelegramError
from backend.router import front_door

log = logging.getLogger("leaf.telegram")

router = APIRouter(tags=["Telegram"])

COMMANDS = [
    ("today", "Today's tasks and watering"),
    ("season", "This week in your season plan"),
    ("help", "What Leaf can do"),
    ("unlink", "Stop messages and disconnect this chat"),
]
HELP = (
    "I'm Leaf, your plant doctor.\n\n"
    "Send me a photo of a sick plant (add a note if you like) and I'll tell you what it might "
    "be, what to do, and where that comes from.\n\n"
    "/today: your garden's tasks and watering\n"
    "/season: this week in your season plan\n"
    "/unlink: stop messages and disconnect this chat\n\n"
    "Leaf gives guidance, not certainty. For food crops and any chemical, follow the product label."
)
NOT_LINKED = (
    "This chat is not connected to a garden yet. Open your garden in Leaf, go to Connect, "
    "press Link Telegram and tap the link it gives you."
)
FOOTER = "Leaf gives guidance, not certainty. Follow the product label for any chemical."
MAX_REPLY = 3800
SEEN = 2000  # update ids remembered, to drop Telegram's retries
PER_CHAT_PER_HOUR = 40

_tasks: set[asyncio.Task] = set()


class BotRequest:
    """What the shared tools need from a request, for work that runs after the webhook
    answered (they read app state and the public address)."""

    def __init__(self, app: Any):
        self.app = app
        self.base_url = app.state.leaf.settings.public_url or "https://leafathon.site"
        self.headers: dict[str, str] = {}


def clip(text: str, limit: int = MAX_REPLY) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------- formatting (plain text) ----------


def today_text(view: dict[str, Any], name: str) -> str:
    lines = [f"{name or 'Your garden'}, {view['date']}"]
    for u in view["urgent"]:
        lines.append(f"URGENT: {u['title']}: {u['text']}")
    open_tasks = [t for t in view["tasks"] if t["status"] == "open"]
    if open_tasks:
        lines.append("\nTo do today:")
        for t in open_tasks[:12]:
            mark = "! " if t["urgency"] == "urgent" else ""
            lines.append(f"• {mark}{t['title']}\n  {t['reason']}")
    else:
        lines.append("\nNothing to do today.")
    water = [w for w in view["water"] if w["action"] != "check"]
    if water:
        lines.append("\nWater:")
        lines += [
            f"• {w['name']}: {'water today' if w['action'] == 'water' else 'no water needed'}"
            + (" (TEST reading)" if w.get("label") == "test" else "")
            for w in water[:10]
        ]
    return clip("\n".join(lines))


def season_text(view: dict[str, Any]) -> str:
    plan = view["plan"]
    if plan is None:
        if view.get("pending") and view["pending"]["status"] in ("writing", "unverified"):
            return "Leaf is still writing your season plan. Try again in a couple of minutes."
        return "You have no season plan yet. Write one in the app: open Season plan."
    week = next((w for w in view["weeks"] if w["week"] == view["this_week"]), None)
    items = week["items"] if week else []
    if not items:
        return f"Nothing planned for the week of {view['this_week']}."
    lines = [f"This week ({view['this_week']}) in your season plan:"]
    for it in items[:12]:
        lines.append(f"• {it['action']} ({it['plant']})\n  {it['why']}")
        lines += [f"  Note: {c['text']}" for c in it.get("cautions", [])[:2]]
    return clip("\n".join(lines))


def diagnosis_text(d: dict[str, Any]) -> str:
    top = d["diagnosis"][0] if d["diagnosis"] else None
    head = (
        f"{top['name']} ({round(top['confidence'] * 100)} % confidence), {d['verdict']}"
        if top
        else "Leaf could not name a cause"
    )
    lines = [head]
    if d["verdict_reason"]:
        lines.append(d["verdict_reason"])
    if d["summary"]:
        lines.append("\n" + d["summary"])
    plan = d["plan"]
    if plan["escalate"]:
        lines.append("\nSend a sample to a plant diagnostic lab: " + plan["escalate_reason"])
    if plan["steps"]:
        lines.append("\nWhat to do:")
        for i, st in enumerate(plan["steps"], 1):
            lines.append(f"{i}. {st['action']}" + (f"\n   {st['why']}" if st["why"] else ""))
            lines += [f"   Careful: {p}" for p in st["precautions"][:1]]
    lines += [f"\nNote: {c}" for c in plan["cautions"][:3]]
    if plan["removed_for_your_choices"]:
        lines.append("\nLeft out because of your garden's choices (organic or pets).")
    if plan["watch_for"]:
        lines.append("\nWatch for: " + "; ".join(plan["watch_for"]))
    if d["sources"]:
        lines.append("\nSources:")
        lines += [f"• {s['title']}\n  {s['url']}" for s in d["sources"][:4]]
    if d["report_url"]:
        lines.append(f"\nFull report: {d['report_url']}")
    lines.append("\n" + FOOTER)
    return clip("\n".join(lines))


# ---------- the webhook ----------


def bot(app: Any) -> TelegramApi | None:
    return getattr(app.state, "telegram", None)


@router.post("/api/telegram/webhook", include_in_schema=False)
async def webhook(request: Request) -> dict[str, bool]:
    api = bot(request.app)
    secret = request.app.state.leaf.settings.telegram_webhook_secret
    if api is None or not secret:
        raise HTTPException(404, "Not found.")
    given = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(403, "Forbidden.")
    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Bad request.") from exc
    if not isinstance(update, dict) or not isinstance(update.get("message"), dict):
        return {"ok": True}  # an update type Leaf does not use
    uid = update.get("update_id")
    seen: deque = request.app.state.tg_seen
    if uid in seen:
        return {"ok": True}  # a retry of an update already taken
    seen.append(uid)
    task = asyncio.create_task(
        handle_message(request.app, api, update["message"], f"tg-{uid}"[:40])
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"ok": True}


async def drain(timeout_s: float = 20.0) -> None:
    """At shutdown, give messages still being handled a moment to finish."""
    if _tasks:
        await asyncio.wait(list(_tasks), timeout=timeout_s)


async def handle_message(
    app: Any, api: TelegramApi, msg: dict[str, Any], trace_id: str = "tg"
) -> None:
    """One message, in its own request context (so its model calls are metered under its own
    trace, not the finished webhook request's): never raises (an unhandled error is logged
    and the user told)."""
    with request_scope(RequestContext(trace_id=trace_id)):
        await _handle_message(app, api, msg)


async def _handle_message(app: Any, api: TelegramApi, msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if not chat_id or chat.get("type") != "private" or (msg.get("from") or {}).get("is_bot"):
        return
    try:
        app.state.tg_limiter.check(chat_id)
    except HTTPException:
        # Say so once in a while, never on every message: the bot must not amplify a flood.
        warned: dict[str, float] = app.state.tg_warned
        if time.time() - warned.get(chat_id, 0) > 600:
            if len(warned) > 5000:
                warned.clear()
            warned[chat_id] = time.time()
            await api.send(chat_id, "That's a lot of messages. Please try again in a little while.")
        return
    try:
        await _handle(app, api, chat_id, msg)
    except HTTPException as exc:  # busy, spend cap, too large, bad photo
        await api.send(chat_id, str(exc.detail))
    except TelegramError as exc:
        log.warning("telegram error while handling a message: %s", exc)
        await api.send(chat_id, "I could not read that file from Telegram. Please try again.")
    except Exception:  # noqa: BLE001
        log.exception("telegram handler failed")
        await api.send(chat_id, "Something went wrong on my side. Please try again.")


async def _handle(app: Any, api: TelegramApi, chat_id: str, msg: dict[str, Any]) -> None:
    repo = app.state.data.repo
    text = (msg.get("text") or "").strip()
    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower() if command.startswith("/") else ""

    if command == "/start" and arg.strip():
        before = await repo.telegram_chat(chat_id)
        garden_id = await repo.consume_telegram_link(arg.strip(), chat_id)
        if garden_id is None:
            await api.send(chat_id, "That link has expired. Make a new one on the Connect page.")
            return
        moved = before is not None and before["garden_id"] != garden_id
        if moved:  # the chat now follows another garden: the old one stops messaging it
            old = before["garden_id"]
            if not await repo.list_telegram_chats(old):
                old_settings = await repo.get_notifications(old)
                old_settings.channels = [c for c in old_settings.channels if c != "telegram"]
                await repo.put_notifications(old, old_settings)
        garden = await repo.get_garden(garden_id)
        settings = await repo.get_notifications(garden_id)
        if "telegram" not in settings.channels:
            settings.channels = [*settings.channels, "telegram"]
            await repo.put_notifications(garden_id, settings)
        name = garden.name if garden else "your garden"
        await api.send(
            chat_id,
            f"Connected to {name}"
            + (" (this chat no longer follows its previous garden)" if moved else "")
            + ". I'll send your morning summary and frost, heat and storm warnings here.\n\n"
            + HELP,
        )
        return
    if command in ("/start", "/help"):
        await api.send(chat_id, HELP if await repo.telegram_chat(chat_id) else NOT_LINKED)
        return

    row = await repo.telegram_chat(chat_id)
    garden = await repo.get_garden(row["garden_id"]) if row else None
    if garden is None:
        await api.send(chat_id, NOT_LINKED)
        return
    caller = Caller(garden, f"garden:{garden.id}", ("read", "tools"))
    req = BotRequest(app)

    if command == "/unlink":
        await repo.delete_telegram_chat(chat_id)
        if not await repo.list_telegram_chats(garden.id):
            settings = await repo.get_notifications(garden.id)
            settings.channels = [c for c in settings.channels if c != "telegram"]
            await repo.put_notifications(garden.id, settings)
        await api.send(chat_id, "Disconnected. No more messages here.")
    elif command == "/today":
        view = await tools.today_tool(req, caller)
        await api.send(chat_id, today_text(view, garden.name))
    elif command == "/season":
        await api.send(chat_id, season_text(await tools.season_tool(req, caller)))
    elif msg.get("photo") or _image_document(msg):
        await _photo(api, chat_id, req, caller, msg)
    elif command:
        await api.send(chat_id, HELP)
    elif text:
        await _free_text(app, api, chat_id, req, caller, text)


def _image_document(msg: dict[str, Any]) -> dict[str, Any] | None:
    """A photo sent as a file (Telegram's "send as file") arrives as a document."""
    doc = msg.get("document")
    if (
        isinstance(doc, dict)
        and doc.get("file_id")
        and str(doc.get("mime_type", "")).startswith("image/")
    ):
        return doc
    return None


async def _photo(
    api: TelegramApi, chat_id: str, req: BotRequest, caller: Caller, msg: dict[str, Any]
) -> None:
    doc = _image_document(msg)
    sizes = (
        [doc]
        if doc
        else [p for p in msg.get("photo", []) if isinstance(p, dict) and p.get("file_id")]
    )
    if not sizes:
        return
    await api.typing(chat_id)
    data = await api.photo_bytes(sizes[-1]["file_id"])  # the largest size
    await api.send(chat_id, "Looking at your photo. This takes about a minute.")
    out = await tools.diagnose_tool(req, caller, (msg.get("caption") or "")[:1000], data)
    await api.send(chat_id, diagnosis_text(out))


async def _free_text(
    app: Any, api: TelegramApi, chat_id: str, req: BotRequest, caller: Caller, text: str
) -> None:
    """A typed message: the front door screens it (code, then a model), then it is routed."""
    leaf = app.state.leaf
    door = await front_door(leaf.llm, leaf.settings, text[:2000], app.state.cache)
    if not door.safe:
        from backend.platform_api import refusal_for

        await api.send(chat_id, refusal_for(door))
        return
    if door.intent == "doctor":
        await api.typing(chat_id)
        out = await tools.diagnose_tool(req, caller, text[:1000])
        await api.send(chat_id, diagnosis_text(out))
    elif door.intent == "care":
        view = await tools.today_tool(req, caller)
        await api.send(chat_id, today_text(view, caller.garden.name))
    elif door.intent == "planner":
        await api.send(chat_id, season_text(await tools.season_tool(req, caller)))
    else:
        await api.send(chat_id, HELP)
