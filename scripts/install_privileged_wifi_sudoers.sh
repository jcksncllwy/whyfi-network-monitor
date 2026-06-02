#!/bin/zsh
set -euo pipefail

USER_NAME="${SUDO_USER:-$(id -un)}"
RULE_NAME="whyfi-wdutil"
TARGET="/etc/sudoers.d/$RULE_NAME"
TMP="$(mktemp)"

cat > "$TMP" <<'EOF'
# Allow Whyfi to read lightweight Wi-Fi association state.
# This permits only wdutil info, not full Wireless Diagnostics collection.
__USER_NAME__ ALL=(root) NOPASSWD: /usr/bin/wdutil info
EOF

/usr/bin/sed -i '' "s/__USER_NAME__/$USER_NAME/g" "$TMP"

visudo -cf "$TMP"

echo "Installing sudoers rule at $TARGET"
echo "This will ask for your macOS admin password."
sudo install -o root -g wheel -m 0440 "$TMP" "$TARGET"
rm -f "$TMP"

echo "Installed $TARGET"
echo "Restarting whyfi LaunchAgent"
"$(dirname "$0")/install_launch_agent.sh"
echo
echo "After the next state interval, whyfi.sqlite should include bssid=... in wifi_association rows."
