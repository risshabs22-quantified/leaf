#!/usr/bin/env bash
# Build Leaf's image, push it, and create a Nebius Serverless Endpoint.
#
#   IMAGE=docker.io/<you>/leaf:v1 SUBNET_ID=<id> ./scripts/deploy_nebius.sh
#
# Requires: docker, nebius CLI (logged in: `nebius profile create`), jq, a filled .env.
# IMAGE can be Docker Hub (public) or Nebius Container Registry in the same project:
#   cr.<region>.nebius.cloud/<registry_path>/leaf:v1  (run `nebius registry configure-helper` first)
# Find your subnet: nebius vpc subnet list --format json | jq -r '.items[] | "\(.metadata.id) \(.metadata.name)"'
#   (verify that command with `nebius vpc subnet list --help` if it errors)
set -euo pipefail

: "${IMAGE:?set IMAGE, e.g. docker.io/you/leaf:v1}"
: "${SUBNET_ID:?set SUBNET_ID (see header comment)}"
NAME="${ENDPOINT_NAME:-leaf}"
PLATFORM="${PLATFORM:-cpu-d3}"     # Leaf needs no GPU: all models run on Token Factory
PRESET="${PRESET:-4vcpu-16gb}"

# Parse .env with python-dotenv (never `source` it: values like a;b break bash).
PY_BIN="${PY_BIN:-.venv/bin/python}"
[[ -x "$PY_BIN" ]] || PY_BIN=python3
eval "$("$PY_BIN" - <<'PY'
import shlex
from dotenv import dotenv_values
for k, v in dotenv_values(".env").items():
    if v and k.isidentifier():
        print(f"export {k}={shlex.quote(v)}")
PY
)"
: "${NEBIUS_API_KEY:?NEBIUS_API_KEY missing from .env}"
: "${TAVILY_API_KEY:?TAVILY_API_KEY missing from .env (needed for citations and the Tavily prize)}"

echo "==> Building $IMAGE (linux/amd64)"
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

if nebius ai endpoint get-by-name --name "$NAME" >/dev/null 2>&1; then
  echo "Endpoint '$NAME' already exists. Delete it first (its URL will change):"
  echo "  nebius ai endpoint delete \$(nebius ai endpoint get-by-name --name $NAME --format jsonpath='{.metadata.id}')"
  echo "or check 'nebius ai endpoint --help' for an update command in your CLI version."
  exit 1
fi

# Secrets: plain --env is simplest. For better hygiene store keys in Nebius SecretStash
# and pass --env-secret NEBIUS_API_KEY=<secret_selector> instead (see docs.nebius.com).
ENV_ARGS=()
for VAR in NEBIUS_API_KEY TAVILY_API_KEY NEBIUS_BASE_URL LEAF_MODEL_BASE_URLS \
           LEAF_REASON_MODEL LEAF_FALLBACK_MODEL LEAF_FAST_MODEL LEAF_VISION_MODEL \
           LEAF_PER_IP_PER_HOUR LEAF_DAILY_CAP LEAF_TIMEOUT_S; do
  [[ -n "${!VAR:-}" ]] && ENV_ARGS+=(--env "${VAR}=${!VAR}")
done

echo "==> Creating endpoint '$NAME' on $PLATFORM/$PRESET (public, no auth so judges can open it)"
nebius ai endpoint create \
  --name "$NAME" \
  --image "$IMAGE" \
  --platform "$PLATFORM" \
  --preset "$PRESET" \
  --container-port 8000 \
  --public \
  --subnet-id "$SUBNET_ID" \
  "${ENV_ARGS[@]}"

ID=$(nebius ai endpoint get-by-name --name "$NAME" --format jsonpath='{.metadata.id}')
URL=$(nebius ai endpoint get "$ID" --format json \
  | jq -r '.status.public_endpoints[] | select(startswith("https://"))' | head -1)
echo "==> Endpoint: $URL"
echo "    Health:   curl $URL/api/health   (expect \"search\": \"TavilySearcher\")"
echo "    Logs:     nebius ai endpoint logs $ID"
echo "    Delete after judging ends (Dec 15): nebius ai endpoint delete $ID"
