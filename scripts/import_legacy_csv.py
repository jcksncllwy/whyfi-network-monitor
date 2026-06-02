#!/usr/bin/env python3
"""Backfill Whyfi SQLite storage from the existing CSV log."""

from __future__ import annotations

import argparse
from pathlib import Path

import whyfi_store


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="legacy CSV file to import")
    parser.add_argument("--sqlite", type=Path, default=PROJECT_ROOT / "data" / "whyfi.sqlite")
    args = parser.parse_args()

    conn = whyfi_store.connect(args.sqlite)
    inserted = whyfi_store.import_csv(conn, args.csv)
    total = whyfi_store.sample_count(conn)
    print(f"sqlite={args.sqlite}")
    print(f"inserted={inserted}")
    print(f"total={total}")
    print(f"latest={whyfi_store.latest_timestamp(conn) or 'none'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
