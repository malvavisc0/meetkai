#!/bin/sh
# Container entrypoint for kai.
#
# Ensures the runtime data directory tree exists (so the cockpit and spawned
# bot subprocesses can write state without a manual init step), then execs
# the passed command using the installed `kai` console script (PATH already
# includes /app/.venv/bin via the Dockerfile). We deliberately do NOT wrap
# in `uv run` — the package is installed in the image venv, so invoking
# `kai` directly is faster and avoids re-resolution on every subprocess
# spawn (the cockpit itself spawns `kai start ...` per bot).
#
# All state lives under /app/data: cockpit.db, per-bot task/sleep/seen/
# history files, and the cockpit-managed bot configs under
# data/configs/cockpit/. No separate configs/ volume.
set -e

mkdir -p /app/data/configs/cockpit /app/data/logs

# Idempotent: each vendor's install() checks for its binary/model first and
# skips the build/download when already present (see
# src/kai/vendors/manager.py), so this is a fast no-op on every boot after
# the first. First boot on a fresh ./data/vendor + ./data/models actually
# builds whisper.cpp and downloads the whisper/kokoro models — this can take
# a few minutes and needs network access to GitHub + Hugging Face.
#
# A failure here (e.g. no network, missing build deps) is fatal by design:
# `set -e` means the container exits non-zero instead of the cockpit coming
# up in a degraded state where every bot start fails with a delayed,
# less-obvious "media services not ready" timeout. Fail loudly here, at
# boot, where `docker compose logs cockpit` shows exactly why.
kai vendors install all

exec "$@"
