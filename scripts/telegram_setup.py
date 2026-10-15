"""Register Leaf's Telegram webhook (run once after `make deploy-cloudflare` with a token).

    1. Make a bot with @BotFather in Telegram and copy its token.
    2. Add one line to the project's env file:   TELEGRAM_BOT_TOKEN=<the token>
    3. make deploy-cloudflare   (ships the token and a generated webhook secret as Worker secrets)
    4. .venv/bin/python -m scripts.telegram_setup

The script reads the token from the env file and the webhook secret from
~/.config/leaf/telegram_webhook_secret (made by the deploy script). It prints the bot's
username and never the token or the secret.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import dotenv_values

from backend.connect.telegram import COMMANDS
from backend.notify.telegram import TelegramApi, TelegramError

URL = (
    os.environ.get("LEAF_PUBLIC_URL", "https://leafathon.site").rstrip("/")
    + "/api/telegram/webhook"
)


async def main() -> int:
    token = dotenv_values(".env").get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing. Add it to the env file (see this file's docstring).")
        return 1
    secret_file = os.path.expanduser(
        os.environ.get("LEAF_TELEGRAM_SECRET_FILE", "~/.config/leaf/telegram_webhook_secret")
    )
    if not os.path.exists(secret_file):
        print("No webhook secret yet: run `make deploy-cloudflare` once with the token set.")
        return 1
    secret = open(secret_file).read().strip()
    api = TelegramApi(token)
    try:
        name = await api.username()
        await api.set_webhook(URL, secret)
        await api.set_commands(COMMANDS)
    except TelegramError as exc:
        print(f"Telegram refused: {exc}")
        return 1
    print(f"Webhook set for @{name}: {URL}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
