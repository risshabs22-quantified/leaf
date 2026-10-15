"""The Telegram Bot API client: send messages, read photos, set up the webhook.

The bot token is a Worker secret (TELEGRAM_BOT_TOKEN). It is part of every Bot API URL, so
this module never logs a URL, a request or an exception message from the HTTP library; errors
are reduced to the method name and Telegram's own error code and description.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("leaf.telegram")

API = "https://api.telegram.org"
MAX_TEXT = 4096  # sendMessage limit
MAX_DOWNLOAD = 10 * 1024 * 1024  # Leaf's own cap, below Telegram's 20 MB
TIMEOUT_S = 20


class TelegramError(Exception):
    """A Bot API call failed. `code` is Telegram's error_code (0 for a network failure)."""

    def __init__(self, method: str, code: int, description: str = ""):
        super().__init__(f"{method}: {code} {description}".strip())
        self.code, self.description = code, description


@dataclass
class SendResult:
    ok: bool
    gone: bool = False  # the chat blocked the bot or no longer exists


class TelegramApi:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._me: dict[str, Any] | None = None

    async def call(self, method: str, **params: Any) -> Any:
        try:
            resp = await self._http.post(f"{API}/bot{self._token}/{method}", json=params)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Not the exception text: for httpx it can include the URL, and so the token.
            raise TelegramError(method, 0, type(exc).__name__) from None
        if not data.get("ok"):
            raise TelegramError(method, int(data.get("error_code", 0)), data.get("description", ""))
        return data.get("result")

    async def me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = await self.call("getMe")
        return self._me

    async def username(self) -> str:
        return str((await self.me()).get("username", ""))

    async def send(self, chat_id: int | str, text: str) -> SendResult:
        """Send text (split at 4096 characters), with link previews off."""
        parts = [text[i : i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
        for part in parts:
            if not part.strip():
                continue
            try:
                await self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=part,
                    link_preview_options={"is_disabled": True},
                )
            except TelegramError as exc:
                gone = exc.code == 403 or (exc.code == 400 and "chat not found" in exc.description)
                log.warning("telegram send failed: %s", exc)
                return SendResult(ok=False, gone=gone)
        return SendResult(ok=True)

    async def typing(self, chat_id: int | str) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass  # cosmetic

    async def photo_bytes(self, file_id: str) -> bytes:
        """Download a photo a user sent (at most 10 MB)."""
        info = await self.call("getFile", file_id=file_id)
        if int(info.get("file_size") or 0) > MAX_DOWNLOAD:
            raise TelegramError("getFile", 413, "file too large")
        try:
            resp = await self._http.get(f"{API}/file/bot{self._token}/{info['file_path']}")
        except httpx.HTTPError as exc:
            raise TelegramError("download", 0, type(exc).__name__) from None
        if resp.status_code != 200 or len(resp.content) > MAX_DOWNLOAD:
            raise TelegramError("download", resp.status_code, "could not download the photo")
        return resp.content

    async def set_webhook(self, url: str, secret: str) -> None:
        await self.call(
            "setWebhook",
            url=url,
            secret_token=secret,
            allowed_updates=["message"],
            drop_pending_updates=True,
        )

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands],
        )
