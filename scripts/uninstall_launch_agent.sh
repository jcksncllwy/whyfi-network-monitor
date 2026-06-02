#!/bin/zsh
set -euo pipefail

LABEL="local.whyfi.monitor"
TARGET_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$TARGET_PLIST"

echo "Stopped and removed $LABEL"

