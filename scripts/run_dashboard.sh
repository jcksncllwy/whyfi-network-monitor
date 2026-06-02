#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"
exec /usr/bin/env python3 scripts/dashboard.py --host 127.0.0.1 --port "${1:-8765}"
