#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENDOR_DIR="$PROJECT_ROOT/vendor"
WAVESHARE_DIR="$VENDOR_DIR/waveshare"

EPAPER_REPO_URL="${EPAPER_REPO_URL:-https://github.com/waveshareteam/e-Paper}"

if ! command -v git &> /dev/null; then
    echo "Error: git is not installed."
    echo "Please install it first:"
    echo "  sudo apt update && sudo apt install -y git"
    exit 1
fi

echo "Waveshare e-Paper library setup"
echo "================================"
echo "  Target:  $WAVESHARE_DIR/waveshare_epd"
echo ""

if [[ -d "$WAVESHARE_DIR/waveshare_epd" ]]; then
    echo "  Already exists at $WAVESHARE_DIR/waveshare_epd — skipping."
    echo "  Remove the directory and re-run to force re-download."
    exit 0
fi

echo "==> Creating vendor directory..."
mkdir -p "$WAVESHARE_DIR"

TEMP_REPO_PATH="$HOME/e-Paper"

if [[ -d "$TEMP_REPO_PATH" ]]; then
    echo "==> Found existing clone at $TEMP_REPO_PATH — pulling updates..."
    git -C "$TEMP_REPO_PATH" pull --quiet --ff-only > /dev/null
else
    echo "==> Cloning e-Paper repository..."
    git clone --quiet "$EPAPER_REPO_URL" "$TEMP_REPO_PATH" > /dev/null
fi

SRC_LIB="$TEMP_REPO_PATH/RaspberryPi_JetsonNano/python/lib/waveshare_epd"

if [[ ! -d "$SRC_LIB" ]]; then
    echo "Error: Could not find waveshare_epd in the cloned repository."
    rm -rf "$TEMP_REPO_PATH"
    exit 1
fi

echo "==> Copying waveshare_epd (excluding __pycache__ and *.pyc)..."
mkdir -p "$WAVESHARE_DIR/waveshare_epd"
rsync -a --exclude='__pycache__' --exclude='*.pyc' "$SRC_LIB/" "$WAVESHARE_DIR/waveshare_epd"

# Cleanup
rm -rf "$TEMP_REPO_PATH"

echo ""
echo "Done. Contents of $WAVESHARE_DIR:"
ls -1 "$WAVESHARE_DIR/"