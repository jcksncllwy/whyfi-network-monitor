#!/usr/bin/env python3
"""Summarize a Whyfi SQLite log."""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from pathlib import Path

import whyfi_store


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_path", nargs="?", default=PROJECT_ROOT / "data" / "whyfi.sqlite", type=Path)
    parser.add_argument("--hours", type=float, help="limit summary to the latest N hours")
    parser.add_argument("--slow-ms", type=float, default=500.0, help="latency threshold for slow probes")
    parser.add_argument("--show-events", type=int, default=20, help="number of failure/slow rows to show")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.sqlite_path.exists():
        raise SystemExit(f"missing SQLite database: {args.sqlite_path}")

    conn = whyfi_store.connect(args.sqlite_path)
    try:
        rows = read_rows(conn, args.hours)
        total_rows = whyfi_store.sample_count(conn)
    finally:
        conn.close()
    if not rows:
        raise SystemExit("no probe rows found")

    totals: dict[tuple[str, str], int] = defaultdict(int)
    failures: dict[tuple[str, str], int] = defaultdict(int)
    latencies: dict[tuple[str, str], list[float]] = defaultdict(list)
    burst_metrics: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"loss_pct": [], "max_ms": [], "jitter_ms": []}
    )
    events: list[dict[str, str]] = []

    for row in rows:
        key = (row["probe"], row["target"])
        totals[key] += 1
        ok = row["ok"] == "true"
        latency = parse_float(row["latency_ms"])
        if not ok:
            failures[key] += 1
            events.append(row)
        if latency is not None:
            latencies[key].append(latency)
            if latency >= args.slow_ms:
                events.append(row)
        if row["probe"] == "ping_burst":
            detail = parse_detail(row["detail"])
            for metric in ("loss_pct", "max_ms", "jitter_ms"):
                value = parse_float(detail.get(metric, ""))
                if value is not None:
                    burst_metrics[key][metric].append(value)

    print(f"source: {args.sqlite_path}")
    print(f"rows: {len(rows)}" + (f" of {total_rows}" if args.hours else ""))
    print(f"window: {rows[0]['timestamp']} -> {rows[-1]['timestamp']}")
    print()
    print("probe summary:")
    for key in sorted(totals):
        values = latencies.get(key, [])
        failure_count = failures.get(key, 0)
        latency_summary = ""
        if values:
            latency_summary = (
                f" median={statistics.median(values):.1f}ms"
                f" p95={percentile(values, 95):.1f}ms"
                f" max={max(values):.1f}ms"
            )
        if key in burst_metrics:
            metrics = burst_metrics[key]
            if metrics["loss_pct"]:
                latency_summary += f" loss_max={max(metrics['loss_pct']):.1f}%"
            if metrics["max_ms"]:
                latency_summary += f" rtt_max={max(metrics['max_ms']):.1f}ms"
            if metrics["jitter_ms"]:
                latency_summary += f" jitter_p95={percentile(metrics['jitter_ms'], 95):.1f}ms"
        print(f"- {key[0]} {key[1]}: {failure_count}/{totals[key]} failed{latency_summary}")

    if events:
        print()
        print(f"first failure/slow events, threshold={args.slow_ms:.0f}ms:")
        for row in events[: args.show_events]:
            latency = f" {row['latency_ms']}ms" if row["latency_ms"] else ""
            print(
                f"- {row['timestamp']} cycle={row['cycle']} {row['probe']} "
                f"{row['target']} ok={row['ok']}{latency} {row['detail']}"
            )
    return 0


def read_rows(conn, hours: float | None) -> list[dict[str, str]]:
    if hours:
        return whyfi_store.rows_for_window(conn, hours)
    cursor = conn.execute(
        """
        SELECT timestamp, cycle, probe, target, ok, latency_ms, detail
        FROM samples
        ORDER BY timestamp_epoch, id
        """
    )
    return [whyfi_store.sqlite_row_to_csv_dict(row) for row in cursor]


def parse_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_detail(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            continue
        key, item_value = item.split("=", 1)
        result[key] = item_value
    return result


def percentile(values: list[float], pct: float) -> float:
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


if __name__ == "__main__":
    raise SystemExit(main())
