#!/bin/sh
set -e

mkdir -p /app/data/configs/cockpit /app/data/logs

# Idempotent: each vendor's is_installed() skips when already present, so
# this is a fast no-op once /app/vendor + /app/models are populated (they
# persist via named volumes in prod). First boot on fresh volumes builds
# whisper.cpp and downloads the whisper/kokoro models — needs network.
kai vendors install all

# --- Morphik token bootstrap -----------------------------------------------
# If KAI_BRAIN_MORPHIK_TOKEN is empty, generate a long-lived bearer token
# from the morphik container's /local/generate_uri endpoint and cache it on
# the persistent cockpit-data volume so it survives restarts. The token is
# exported into the environment so BrainSettings picks it up when the
# cockpit server starts.
#
# /local/generate_uri uses Form fields (not JSON) and requires
# LOCAL_URI_PASSWORD to match the morphik container's env. The returned URI
# is morphik://<name>:<jwt>@<host> — we extract the <jwt>.
TOKEN_FILE=/app/data/morphik_token
if [ -z "${KAI_BRAIN_MORPHIK_TOKEN:-}" ]; then
  if [ -f "$TOKEN_FILE" ]; then
    export KAI_BRAIN_MORPHIK_TOKEN="$(cat "$TOKEN_FILE")"
  else
    KAI_BRAIN_MORPHIK_TOKEN="$(python3 -c "
import urllib.request, urllib.parse, json, os

password = os.environ.get('KAI_BRAIN_MORPHIK_LOCAL_PASSWORD', '')
form = urllib.parse.urlencode({
    'name': 'kai',
    'expiry_days': '5475',
    'password_token': password,
    'server_mode': 'false',
}).encode()
req = urllib.request.Request(
    'http://morphik:8000/local/generate_uri',
    data=form,
    headers={'Content-Type': 'application/x-www-form-urlencoded'},
)
resp = urllib.request.urlopen(req, timeout=30)
uri = json.loads(resp.read())['uri']
# morphik://kai:<jwt>@host  ->  extract <jwt>
prefix = 'morphik://kai:'
rest = uri[len(prefix):]
print(rest.split('@')[0])
" 2>/dev/null || true)"
    if [ -n "$KAI_BRAIN_MORPHIK_TOKEN" ]; then
      printf '%s' "$KAI_BRAIN_MORPHIK_TOKEN" > "$TOKEN_FILE"
      chmod 600 "$TOKEN_FILE"
      export KAI_BRAIN_MORPHIK_TOKEN
      echo "[entrypoint] Generated and cached Morphik token."
    else
      echo "[entrypoint] WARNING: Could not generate Morphik token — brain tool will be disabled."
      echo "[entrypoint] Check that morphik is healthy and KAI_BRAIN_MORPHIK_LOCAL_PASSWORD is set."
    fi
  fi
fi

exec "$@"
