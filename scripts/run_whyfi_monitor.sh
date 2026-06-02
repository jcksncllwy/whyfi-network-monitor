#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p data

exec /usr/bin/env python3 scripts/whyfi_monitor.py \
  --interval 15 \
  --timeout 3 \
  --state-interval 4 \
  --wifi-state-interval 7 \
  --burst-ping-interval 9 \
  --burst-ping-count 8 \
  --burst-ping-spacing 0.2 \
  --burst-ping-target gateway \
  --burst-ping-target 1.1.1.1 \
  --http-cache-mode headers \
  --http-timing-timeout 12 \
  --http-timing-interval 4 \
  --http-timing-target https://www.apple.com/ \
  --http-timing-target https://www.cloudflare.com/cdn-cgi/trace \
  --http-timing-target https://www.wikipedia.org/ \
  --http-timing-target https://www.reddit.com/ \
  --http-timing-target https://questionablecontent.net/ \
  --http-timing-target https://killsixbilliondemons.com/ \
  --http-timing-target https://wikiroulette.co/ \
  --sqlite data/whyfi.sqlite \
  --privileged-wifi
