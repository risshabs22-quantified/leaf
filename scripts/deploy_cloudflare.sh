#!/usr/bin/env bash
# Deploy Leaf to Cloudflare Containers behind a Worker at https://leaf.<subdomain>.workers.dev.
#
#   ./scripts/deploy_cloudflare.sh            # build the image, deploy, then set the secrets
#   DRY_RUN=1 ./scripts/deploy_cloudflare.sh  # bundle and validate only, create nothing
#
# Requires: Docker running, Node, `npx wrangler login` done once, Workers Paid on the account,
# and NEBIUS_API_KEY and TAVILY_API_KEY in .env (plus R2 keys for share links that last).
# Model calls still go only to Nebius Token Factory; Cloudflare only hosts the app.
#
# Cost: Workers Paid is $5 a month and includes 25 GiB-hours of container memory. The
# `basic` instance (1/4 vCPU, 1 GiB) sleeps after 30 idle minutes and bills only while awake;
# awake all month it would add about $7. R2 is free up to 10 GB. Settings live in
# cloudflare/wrangler.jsonc.
set -euo pipefail
cd "$(dirname "$0")/.."

PY_BIN="${PY_BIN:-.venv/bin/python}"
[[ -x "$PY_BIN" ]] || PY_BIN=python3

(cd cloudflare && npm ci --no-fund --no-audit --silent)
WRANGLER=(npx --prefix cloudflare wrangler --config cloudflare/wrangler.jsonc)

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  "${WRANGLER[@]}" deploy --dry-run
  echo "DRY_RUN=1: the image was built locally; nothing was uploaded or created."
  exit 0
fi

# Secrets go to Wrangler through a private temp file, never argv or the terminal.
umask 077
SECRETS_FILE="$(mktemp)"
trap 'rm -f "$SECRETS_FILE"' EXIT
"$PY_BIN" - "$SECRETS_FILE" <<'PY'
import json, sys
from dotenv import dotenv_values

env = dotenv_values(".env")
for required in ("NEBIUS_API_KEY", "TAVILY_API_KEY"):
    if not env.get(required):
        sys.exit(f"{required} is missing from .env")
keys = ["NEBIUS_API_KEY", "TAVILY_API_KEY"]
if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
    keys += ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
else:
    print("Note: no R2 keys in .env, so reports and share links are lost when the container sleeps.")
secrets = {k: env[k] for k in keys}
# Web Push private key: created once by scripts/gen_vapid.py in a user-only file (never printed).
import os
vapid = os.path.expanduser(os.environ.get("LEAF_VAPID_KEY_FILE", "~/.config/leaf/vapid_private.pem"))
if os.path.exists(vapid):
    secrets["VAPID_PRIVATE_KEY"] = open(vapid).read()
else:
    print("Note: no VAPID key file (run scripts/gen_vapid.py), so Web Push stays off.")
# Telegram bot (optional): the token is TELEGRAM_BOT_TOKEN in the env file (from @BotFather). The
# webhook secret is generated once into a user-only file, like the VAPID key, never printed.
if env.get("TELEGRAM_BOT_TOKEN"):
    import secrets as _secrets
    tg_file = os.path.expanduser(
        os.environ.get("LEAF_TELEGRAM_SECRET_FILE", "~/.config/leaf/telegram_webhook_secret")
    )
    if not os.path.exists(tg_file):
        os.makedirs(os.path.dirname(tg_file), exist_ok=True)
        fd = os.open(tg_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, _secrets.token_urlsafe(32).encode())
        os.close(fd)
    secrets["TELEGRAM_BOT_TOKEN"] = env["TELEGRAM_BOT_TOKEN"]
    secrets["TELEGRAM_WEBHOOK_SECRET"] = open(tg_file).read().strip()
else:
    print("Note: no TELEGRAM_BOT_TOKEN set, so the Telegram bot stays off (and hidden).")
json.dump(secrets, open(sys.argv[1], "w"))
print(f"{len(secrets)} secrets ready")
PY

# Platform schema first: the new version's code expects it. Applies only migrations D1 has not
# recorded (backend/data/migrations); CI=1 answers Wrangler's confirmation prompt.
echo "==> Applying D1 migrations"
# One retry: the D1 API has failed the first call of two deploys in a row (Oct 2026), then
# succeeded at once.
CI=1 "${WRANGLER[@]}" d1 migrations apply leaf-platform --remote \
  || { sleep 5; CI=1 "${WRANGLER[@]}" d1 migrations apply leaf-platform --remote; }

# Secrets ship with the version (--secrets-file). Setting them afterwards is too late: the
# rollout restarts the container as soon as the version is live, with the env it had then.
echo "==> Building the image and deploying the Worker with its secrets"
"${WRANGLER[@]}" deploy --secrets-file "$SECRETS_FILE"

echo "==> Deployed. Open the URL Wrangler printed above, once, in a browser."
echo "    Container status:  npx --prefix cloudflare wrangler containers list"
echo "    Logs:              npx --prefix cloudflare wrangler tail leaf"
echo "    Remove after judging (Dec 15): npx --prefix cloudflare wrangler delete leaf"
