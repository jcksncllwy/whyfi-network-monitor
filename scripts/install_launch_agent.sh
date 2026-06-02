#!/bin/zsh
set -euo pipefail

LABEL="local.whyfi.monitor"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$TARGET_DIR" "$PROJECT_DIR/data"
cat > "$TARGET_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT_DIR/scripts/run_whyfi_monitor.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR</string>
  <key>StandardOutPath</key>
  <string>$PROJECT_DIR/data/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$PROJECT_DIR/data/launchd.err.log</string>
</dict>
</plist>
PLIST
chmod 644 "$TARGET_PLIST"

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Installed and started $LABEL"
echo "Data: $PROJECT_DIR/data/whyfi.sqlite"
echo "Logs: $PROJECT_DIR/data/launchd.out.log and launchd.err.log"
