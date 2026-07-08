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

exec "$@"
