#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$PROJECT_DIR/tools/wifi-bssid-helper.swift"
OUT_DIR="$PROJECT_DIR/bin"
OUT="$OUT_DIR/wifi-bssid-helper"
APP="$OUT_DIR/WhyfiWiFiStatus.app"
APP_EXE="$APP/Contents/MacOS/WhyfiWiFiStatus"

mkdir -p "$OUT_DIR"

swiftc \
  -framework Foundation \
  -framework CoreWLAN \
  -framework CoreLocation \
  "$SRC" \
  -o "$OUT"

codesign --force --sign - "$OUT" >/dev/null
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$OUT" "$APP_EXE"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>WhyfiWiFiStatus</string>
  <key>CFBundleIdentifier</key>
  <string>local.whyfi.wifi-status-helper</string>
  <key>CFBundleName</key>
  <string>Whyfi Wi-Fi Status Helper</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSLocationWhenInUseUsageDescription</key>
  <string>Whyfi uses location and Wi-Fi identifiers to separate diagnostics for networks with the same name.</string>
  <key>NSLocationUsageDescription</key>
  <string>Whyfi uses location and Wi-Fi identifiers to separate diagnostics for networks with the same name.</string>
</dict>
</plist>
PLIST
codesign --force --deep --sign - "$APP" >/dev/null
echo "$OUT"
echo "$APP"
