#!/bin/zsh
set -euo pipefail

TARGET="/etc/sudoers.d/whyfi-wdutil"

echo "Removing sudoers rule at $TARGET"
echo "This will ask for your macOS admin password."
sudo rm -f "$TARGET"
"$(dirname "$0")/install_launch_agent.sh"
echo "Removed privileged Wi-Fi sudoers rule and restarted Whyfi."
