#!/bin/sh
set -e

mkdir -p /app/data/configs/cockpit /app/data/logs

# Idempotent: each vendor's is_installed() skips when already present, so
# this is a fast no-op once /app/vendor + /app/models are populated (they
# persist via named volumes in prod). First boot on fresh volumes builds
# whisper.cpp and downloads the whisper/kokoro models — needs network.
kai vendors install all

exec "$@"
