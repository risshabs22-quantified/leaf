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
json.dump({k: env[k] for k in keys}, open(sys.argv[1], "w"))
print(f"{len(keys)} secrets ready")
PY

echo "==> Building the image and deploying the Worker"
"${WRANGLER[@]}" deploy
echo "==> Setting secrets"
"${WRANGLER[@]}" secret bulk "$SECRETS_FILE"

echo "==> Deployed. Open the URL Wrangler printed above, once, in a browser."
echo "    Container status:  npx --prefix cloudflare wrangler containers list"
echo "    Logs:              npx --prefix cloudflare wrangler tail leaf"
echo "    Remove after judging (Dec 15): npx --prefix cloudflare wrangler delete leaf"
