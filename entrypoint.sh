#!/bin/sh
set -e

mkdir -p /app/data/configs/cockpit /app/data/logs

# Idempotent: each vendor's is_installed() skips when already present, so
# this is a fast no-op once /app/vendor + /app/models are populated (they
# persist via named volumes in prod). First boot on fresh volumes builds
# whisper.cpp and downloads the whisper/kokoro models — needs network.
kai vendors install all

# Morphik auth is enforced (bypass_auth_mode = false); KAI_BRAIN_MORPHIK_TOKEN
# must be minted manually, once per environment, against that environment's
# own morphik (see .env.example). No auto-mint fallback exists — warn loudly
# rather than let the brain tool fail silently.
if [ -z "${KAI_BRAIN_MORPHIK_TOKEN:-}" ]; then
  echo "[entrypoint] WARNING: KAI_BRAIN_MORPHIK_TOKEN is unset — brain tool will be disabled."
  echo "[entrypoint] Mint one against this environment's morphik (see KAI_BRAIN_MORPHIK_TOKEN in .env.example) and set it, then restart."
fi

echo "[entrypoint] running DB migrations..."
alembic upgrade head || { echo "[entrypoint] migrations failed"; exit 1; }

exec "$@"
