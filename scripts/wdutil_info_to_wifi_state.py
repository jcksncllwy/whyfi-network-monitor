#!/usr/bin/env python3
"""Convert `wdutil info` output on stdin to a compact wifi_association detail string."""

from __future__ import annotations

import sys

from whyfi_monitor import parse_wdutil_wifi_info


def main() -> int:
    data = parse_wdutil_wifi_info(sys.stdin.read())
    if not data:
        return 1
    print(",".join(f"{key}={value}" for key, value in data.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

