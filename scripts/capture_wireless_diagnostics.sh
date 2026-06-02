#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAMP="$(date +%Y-%m-%d-%H%M%S)"
OUT_DIR="$PROJECT_DIR/data/wireless-diagnostics/$STAMP"

mkdir -p "$OUT_DIR"

echo "Capturing privileged Wireless Diagnostics baseline to:"
echo "$OUT_DIR"
echo
echo "This will ask for your macOS admin password."

sudo wdutil diagnose -q -f "$OUT_DIR"

echo
echo "Done. Compare this timestamp with:"
echo "$PROJECT_DIR/data/whyfi.sqlite"
