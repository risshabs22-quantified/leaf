#!/usr/bin/env bash
# Build Leaf's image, push it, and create a Nebius Serverless Endpoint on the smallest CPU preset.
#
#   IMAGE=docker.io/<you>/leaf:v1 ./scripts/deploy_nebius.sh            # validate, then create
#   IMAGE=docker.io/<you>/leaf:v1 DRY_RUN=1 ./scripts/deploy_nebius.sh  # validate only, create nothing
#
# Requires: docker, the nebius CLI logged in (`nebius profile create`), jq, a filled .env.
# IMAGE can be Docker Hub (public) or Nebius Container Registry in the same project:
#   cr.<region>.nebius.cloud/<registry_path>/leaf:v1  (run `nebius registry configure-helper` first)
#
# Cost (Compute pricing from Oct 1, 2026, without VAT; Serverless bills at Compute rates):
#   cpu-d3 2vcpu-8gb  2 x $0.015 + 8 x $0.0045 = $0.066 per hour (all regions)
#   cpu-e2 2vcpu-8gb  2 x $0.012 + 8 x $0.0045 = $0.060 per hour (eu-north1 only)
#   Network SSD $0.071 per GiB per 730 h, so a 32 GiB disk is about $3.11 a month.
# A stopped endpoint is not billed:  nebius ai endpoint stop --id <ID>
#
# Secrets never go on a command line or into the image: API keys are stored in SecretStash
# (MysteryBox, free in preview) through a private temp file and passed with --env-secret.
set -euo pipefail

: "${IMAGE:?set IMAGE, e.g. docker.io/you/leaf:v1}"
NAME="${ENDPOINT_NAME:-leaf}"
PLATFORM="${PLATFORM:-cpu-d3}"     # cpu-e2 is 10% cheaper but only in eu-north1
PRESET="${PRESET:-2vcpu-8gb}"      # the smallest CPU preset; Leaf runs no models locally
DISK_SIZE="${DISK_SIZE:-32Gi}"     # the image is under 1 GiB; the default would be 250Gi
SECRET_NAME="${SECRET_NAME:-leaf-secrets}"
NEBIUS="${NEBIUS_BIN:-nebius}"
command -v "$NEBIUS" >/dev/null || NEBIUS="$HOME/.nebius/bin/nebius"

# Parse .env with python-dotenv (never `source` it: values like a;b break bash). Nothing is echoed.
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

PARENT_ID="${PARENT_ID:-$("$NEBIUS" config get parent-id 2>/dev/null || true)}"
: "${PARENT_ID:?no project: set PARENT_ID or create a profile with a default project}"

if "$NEBIUS" ai endpoint get-by-name --name "$NAME" >/dev/null 2>&1; then
  echo "Endpoint '$NAME' already exists. To replace it (its URL will change):"
  echo "  $NEBIUS ai endpoint delete --id \$($NEBIUS ai endpoint get-by-name --name $NAME --format jsonpath='{.metadata.id}')"
  exit 1
fi

# ---------------------------------------------------------------- secrets -> SecretStash
SECRET_KEYS=(NEBIUS_API_KEY TAVILY_API_KEY)
[[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] &&
  SECRET_KEYS+=(AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY)

umask 077
REQ="$(mktemp)"
trap 'rm -f "$REQ"' EXIT
SECRET_ID="$("$NEBIUS" mysterybox secret get-by-name --name "$SECRET_NAME" --parent-id "$PARENT_ID" \
  --format jsonpath='{.metadata.id}' 2>/dev/null || true)"
# Build the request in Python so values are JSON-escaped and never pass through argv.
SECRET_KEYS="${SECRET_KEYS[*]}" SECRET_NAME="$SECRET_NAME" PARENT_ID="$PARENT_ID" \
SECRET_ID="$SECRET_ID" "$PY_BIN" - "$REQ" <<'PY'
import json, os, sys
payload = [{"key": k, "string_value": os.environ[k]} for k in os.environ["SECRET_KEYS"].split()]
if os.environ["SECRET_ID"]:  # a new primary version of the existing secret
    req = {"metadata": {"parent_id": os.environ["SECRET_ID"]},
           "spec": {"payload": payload, "set_primary": True}}
else:
    req = {"metadata": {"name": os.environ["SECRET_NAME"], "parent_id": os.environ["PARENT_ID"]},
           "spec": {"secret_version": {"payload": payload, "set_primary": True}}}
json.dump(req, open(sys.argv[1], "w"))
PY

SECRET_ARGS=()
for KEY in "${SECRET_KEYS[@]}"; do
  SECRET_ARGS+=(--env-secret "${KEY}=${SECRET_NAME}")
done
ENV_ARGS=()
# Non-secret settings travel as plain environment variables.
for VAR in NEBIUS_BASE_URL LEAF_MODEL_BASE_URLS LEAF_REASON_MODEL LEAF_FALLBACK_MODEL \
           LEAF_FAST_MODEL LEAF_VISION_MODEL LEAF_PER_IP_PER_HOUR LEAF_DAILY_CAP LEAF_TIMEOUT_S \
           LEAF_DEEP_DAILY_CAP LEAF_TAVILY_DAILY_CAP LEAF_JOURNAL_BUCKET AWS_ENDPOINT_URL AWS_REGION; do
  [[ -n "${!VAR:-}" ]] && ENV_ARGS+=(--env "${VAR}=${!VAR}")
done
ENV_ARGS+=(--env "LEAF_TRUSTED_PROXY_HOPS=${LEAF_TRUSTED_PROXY_HOPS:-1}")
if [[ -z "${LEAF_JOURNAL_BUCKET:-}" ]]; then
  echo "Note: LEAF_JOURNAL_BUCKET is not set, so reports live on the container disk and are lost"
  echo "      when the endpoint is recreated. Set it (see .env.example) for share links that last."
fi

CREATE=(ai endpoint create
  --name "$NAME"
  --image "$IMAGE"
  --platform "$PLATFORM"
  --preset "$PRESET"
  --disk-size "$DISK_SIZE"
  --container-port 8000
  --auth none          # judges open it without a token; abuse is limited in the app itself
  "${ENV_ARGS[@]}")

echo "==> Validating: $PLATFORM/$PRESET, disk $DISK_SIZE"
if [[ -n "$SECRET_ID" ]]; then
  "$NEBIUS" "${CREATE[@]}" "${SECRET_ARGS[@]}" --dry-run >/dev/null
else  # the secret is created below, so check everything else now
  "$NEBIUS" "${CREATE[@]}" --dry-run >/dev/null
fi
echo "    The endpoint request is valid."
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "DRY_RUN=1: nothing was built, stored or created."
  exit 0
fi

echo "==> Building $IMAGE (linux/amd64)"
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

echo "==> Storing ${#SECRET_KEYS[@]} secrets in SecretStash '$SECRET_NAME'"
if [[ -n "$SECRET_ID" ]]; then
  "$NEBIUS" mysterybox secret-version create --file "$REQ" >/dev/null
else
  "$NEBIUS" mysterybox secret create --file "$REQ" >/dev/null
fi
rm -f "$REQ"

echo "==> Creating endpoint '$NAME'"
"$NEBIUS" "${CREATE[@]}" "${SECRET_ARGS[@]}"

ID=$("$NEBIUS" ai endpoint get-by-name --name "$NAME" --format jsonpath='{.metadata.id}')
URL=$("$NEBIUS" ai endpoint get --id "$ID" --format json \
  | jq -r '.status.public_endpoints[] | select(startswith("https://"))' | head -1)
echo "==> Endpoint: $URL"
echo "    Check it once, in a browser or with one request: $URL/api/health"
echo "    Logs:  $NEBIUS ai endpoint logs $ID"
echo "    Pause (no charge while stopped): $NEBIUS ai endpoint stop --id $ID"
echo "    Delete after judging ends (Dec 15): $NEBIUS ai endpoint delete --id $ID"
