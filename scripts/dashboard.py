#!/usr/bin/env python3
"""Local web dashboard for Whyfi SQLite data."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
from collections import defaultdict
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import whyfi_store


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE = PROJECT_ROOT / "data" / "whyfi.sqlite"
STATIC_ROOT = PROJECT_ROOT / "dashboard"
DETAIL_ITEM_RE = re.compile(r"([^=,]+)=([^,]*)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="host interface to bind")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on")
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE, help="Whyfi SQLite path")
    return parser.parse_args()


def parse_timestamp(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_float(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace("Mbps", "").replace("dBm", "").replace("%", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_detail(value: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in DETAIL_ITEM_RE.finditer(value)}


def percentile(values: list[float], pct: float) -> float | None:
    values = sorted(value for value in values if value is not None and math.isfinite(value))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    fraction = rank - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def median(values: list[float]) -> float | None:
    return percentile(values, 50)


def summarize_probe(rows: list[dict[str, str]], probe: str) -> list[dict[str, object]]:
    totals: dict[str, int] = defaultdict(int)
    failures: dict[str, int] = defaultdict(int)
    latencies: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row["probe"] != probe:
            continue
        target = row["target"]
        totals[target] += 1
        if row["ok"] != "true":
            failures[target] += 1
        latency = parse_float(row["latency_ms"])
        if latency is not None and row["ok"] == "true":
            latencies[target].append(latency)
    result = []
    for target in sorted(totals):
        values = latencies[target]
        result.append(
            {
                "target": target,
                "count": totals[target],
                "failures": failures[target],
                "median_ms": median(values),
                "p95_ms": percentile(values, 95),
                "max_ms": max(values) if values else None,
                "slow_500": sum(1 for value in values if value >= 500),
                "slow_1000": sum(1 for value in values if value >= 1000),
            }
        )
    return result


def summarize_burst(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    totals: dict[str, int] = defaultdict(int)
    failures: dict[str, int] = defaultdict(int)
    avg_latencies: dict[str, list[float]] = defaultdict(list)
    jitters: dict[str, list[float]] = defaultdict(list)
    losses: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row["probe"] != "ping_burst":
            continue
        target = row["target"]
        detail = parse_detail(row["detail"])
        totals[target] += 1
        if row["ok"] != "true":
            failures[target] += 1
        avg = parse_float(detail.get("avg_ms"))
        jitter = parse_float(detail.get("jitter_ms"))
        loss = parse_float(detail.get("loss_pct"))
        if avg is not None and row["ok"] == "true":
            avg_latencies[target].append(avg)
        if jitter is not None and row["ok"] == "true":
            jitters[target].append(jitter)
        if loss is not None:
            losses[target].append(loss)
    result = []
    for target in sorted(totals):
        target_jitters = jitters[target]
        target_losses = losses[target]
        result.append(
            {
                "target": target,
                "count": totals[target],
                "failures": failures[target],
                "median_avg_ms": median(avg_latencies[target]),
                "p95_avg_ms": percentile(avg_latencies[target], 95),
                "median_jitter_ms": median(target_jitters),
                "p95_jitter_ms": percentile(target_jitters, 95),
                "max_jitter_ms": max(target_jitters) if target_jitters else None,
                "max_loss_pct": max(target_losses) if target_losses else None,
            }
        )
    return result


def current_state(rows: list[dict[str, str]]) -> dict[str, object]:
    state: dict[str, object] = {}
    for row in reversed(rows):
        if row["probe"] == "state" and row["target"] == "wifi_association" and "wifi" not in state:
            state["wifi"] = parse_detail(row["detail"]) | {"timestamp": row["timestamp"], "ok": row["ok"] == "true"}
        elif row["probe"] == "state" and row["target"] == "dns_servers" and "dns_servers" not in state:
            state["dns_servers"] = {"timestamp": row["timestamp"], "value": row["detail"], "ok": row["ok"] == "true"}
        elif row["probe"] == "usage" and "usage" not in state:
            state["usage"] = parse_detail(row["detail"]) | {"timestamp": row["timestamp"], "ok": row["ok"] == "true"}
        elif row["probe"] == "monitor" and row["target"] == "loop_timing" and "loop" not in state:
            state["loop"] = parse_detail(row["detail"]) | {"timestamp": row["timestamp"], "ok": row["ok"] == "true"}
        if {"wifi", "dns_servers", "usage", "loop"}.issubset(state):
            break
    return state


def build_timeseries(rows: list[dict[str, str]]) -> dict[str, list[dict[str, object]]]:
    series: dict[str, list[dict[str, object]]] = {
        "gateway_ping": [],
        "wan_ping": [],
        "http_timing_p95": [],
        "burst_gateway_avg": [],
        "burst_wan_avg": [],
        "rx_bps": [],
        "tx_bps": [],
        "tx_rate": [],
    }

    http_by_cycle: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        timestamp = row["timestamp"]
        cycle = row["cycle"]
        latency = parse_float(row["latency_ms"])
        if row["probe"] == "ping" and row["target"] == "192.168.4.1" and latency is not None:
            series["gateway_ping"].append({"t": timestamp, "v": latency, "ok": row["ok"] == "true"})
        elif row["probe"] == "ping" and row["target"] == "1.1.1.1" and latency is not None:
            series["wan_ping"].append({"t": timestamp, "v": latency, "ok": row["ok"] == "true"})
        elif row["probe"] == "http_timing" and latency is not None and row["ok"] == "true":
            http_by_cycle[(timestamp, cycle)].append(latency)
        elif row["probe"] == "ping_burst":
            detail = parse_detail(row["detail"])
            avg = parse_float(detail.get("avg_ms"))
            jitter = parse_float(detail.get("jitter_ms"))
            loss = parse_float(detail.get("loss_pct"))
            target_series = "burst_gateway_avg" if row["target"] == "192.168.4.1" else "burst_wan_avg"
            if avg is not None and target_series in series:
                series[target_series].append({"t": timestamp, "v": avg, "jitter": jitter, "loss": loss, "ok": row["ok"] == "true"})
        elif row["probe"] == "usage":
            detail = parse_detail(row["detail"])
            rx = parse_float(detail.get("rx_bps"))
            tx = parse_float(detail.get("tx_bps"))
            if rx is not None:
                series["rx_bps"].append({"t": timestamp, "v": rx})
            if tx is not None:
                series["tx_bps"].append({"t": timestamp, "v": tx})
        elif row["probe"] == "state" and row["target"] == "wifi_association":
            detail = parse_detail(row["detail"])
            tx_rate = parse_float(detail.get("tx_rate"))
            if tx_rate is not None and tx_rate > 0:
                series["tx_rate"].append({"t": timestamp, "v": tx_rate})

    for (timestamp, _cycle), values in sorted(http_by_cycle.items()):
        p95 = percentile(values, 95)
        if p95 is not None:
            series["http_timing_p95"].append({"t": timestamp, "v": p95})
    return {key: values[-240:] for key, values in series.items()}


def build_anomalies(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    anomalies: list[dict[str, object]] = []
    by_cycle: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_cycle[(row["timestamp"], row["cycle"])].append(row)

    for (timestamp, cycle), cycle_rows in sorted(by_cycle.items(), reverse=True):
        findings: list[str] = []
        severity = 0
        http_slow = []
        for row in cycle_rows:
            detail = parse_detail(row["detail"])
            latency = parse_float(row["latency_ms"])
            if row["ok"] != "true" and row["probe"] not in {"monitor"}:
                findings.append(f"{row['probe']} {short_target(row['target'])} failed")
                severity = max(severity, 3)
            if row["probe"] == "ping" and latency is not None and latency >= 250:
                findings.append(f"{short_target(row['target'])} ping {latency:.0f}ms")
                severity = max(severity, 2)
            elif row["probe"] == "http" and latency is not None and latency >= 500:
                findings.append(f"tiny HTTP {latency:.0f}ms")
                severity = max(severity, 2)
            elif row["probe"] == "http_timing" and row["ok"] == "true" and latency is not None and latency >= 500:
                http_slow.append(f"{short_target(row['target'])} {latency:.0f}ms")
                severity = max(severity, 1 if latency < 1000 else 2)
            elif row["probe"] == "ping_burst":
                loss = parse_float(detail.get("loss_pct"))
                jitter = parse_float(detail.get("jitter_ms"))
                max_ms = parse_float(detail.get("max_ms"))
                if loss and loss > 0:
                    findings.append(f"{short_target(row['target'])} burst loss {loss:.1f}%")
                    severity = max(severity, 3)
                elif jitter and jitter >= 40:
                    findings.append(f"{short_target(row['target'])} burst jitter {jitter:.0f}ms")
                    severity = max(severity, 2)
                elif max_ms and max_ms >= 200:
                    findings.append(f"{short_target(row['target'])} burst max {max_ms:.0f}ms")
                    severity = max(severity, 1)
            elif row["probe"] == "monitor" and row["target"] == "loop_timing":
                over = parse_float(detail.get("cycle_start_gap_over_interval_s"))
                sleep_gap = parse_float(detail.get("sleep_gap_s"))
                if detail.get("sleep_likely") == "true":
                    findings.append(f"system sleep/wake gap {format_seconds(sleep_gap)}")
                    severity = max(severity, 1)
                elif over and over >= 8:
                    findings.append(f"collector lag +{over:.0f}s")
                    severity = max(severity, 1)
        if len(http_slow) >= 3:
            findings.append("multi-target HTTP: " + ", ".join(http_slow[:4]))
            severity = max(severity, 2)
        elif http_slow:
            findings.extend(http_slow[:3])
        if findings:
            anomalies.append({"timestamp": timestamp, "cycle": cycle, "severity": severity, "findings": findings[:6]})
        if len(anomalies) >= 80:
            break
    return anomalies


def short_target(target: str) -> str:
    return target.replace("https://", "").replace("www.", "").rstrip("/")


def format_seconds(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 120:
        return f"{value:.0f}s"
    return f"{value / 60:.1f}m"


def parse_network_id(value: str | None) -> int | None:
    if not value or value == "current":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def load_rows(
    sqlite_path: Path,
    hours: float,
    requested_network_id: int | None,
) -> tuple[list[dict[str, str]], str | None, dict[str, object], dict[str, object] | None, list[dict[str, object]]]:
    conn = whyfi_store.connect(sqlite_path)
    try:
        total = whyfi_store.sample_count(conn)
        networks = whyfi_store.network_profiles(conn)
        current_network = whyfi_store.current_network_profile(conn)
        selected_network_id = requested_network_id or (int(current_network["id"]) if current_network else None)
        selected_network = next((network for network in networks if int(network["id"]) == selected_network_id), None)
        return (
            whyfi_store.rows_for_window(conn, hours, selected_network_id),
            whyfi_store.latest_timestamp(conn, selected_network_id),
            {
                "type": "sqlite",
                "path": str(sqlite_path),
                "size": sqlite_path.stat().st_size,
                "total_rows": total,
            },
            selected_network,
            networks,
        )
    finally:
        conn.close()


def build_summary(sqlite_path: Path, hours: float, requested_network_id: int | None) -> dict[str, object]:
    rows, latest, source, selected_network, networks = load_rows(sqlite_path, hours, requested_network_id)
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source,
        "selected_network": selected_network,
        "networks": networks,
        "window_hours": hours,
        "row_count": len(rows),
        "latest_timestamp": latest,
        "state": current_state(rows),
        "summary": {
            "ping": summarize_probe(rows, "ping"),
            "dns": summarize_probe(rows, "dns"),
            "http": summarize_probe(rows, "http"),
            "http_timing": summarize_probe(rows, "http_timing"),
            "ping_burst": summarize_probe(rows, "ping_burst"),
            "burst": summarize_burst(rows),
        },
        "series": build_timeseries(rows),
        "anomalies": build_anomalies(rows),
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    sqlite_path = DEFAULT_SQLITE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/summary":
            query = parse_qs(parsed.query)
            hours = parse_float(query.get("hours", ["6"])[0]) or 6
            hours = max(0.25, min(hours, 168))
            network_id = parse_network_id(query.get("network_id", ["current"])[0])
            self.write_json(build_summary(self.sqlite_path, hours, network_id))
            return
        if parsed.path == "/api/networks":
            conn = whyfi_store.connect(self.sqlite_path)
            try:
                self.write_json(
                    {
                        "current_network": whyfi_store.current_network_profile(conn),
                        "networks": whyfi_store.network_profiles(conn),
                    }
                )
            finally:
                conn.close()
            return
        if parsed.path == "/healthz":
            self.write_json({"ok": True})
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/network_alias":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            network_id = int(payload.get("network_id") or 0)
            alias = str(payload.get("alias") or "")
            conn = whyfi_store.connect(self.sqlite_path)
            try:
                whyfi_store.update_network_alias(conn, network_id, alias)
                self.write_json({"ok": True})
            finally:
                conn.close()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def write_json(self, value: object) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    args = parse_args()
    DashboardHandler.sqlite_path = args.sqlite
    os.chdir(STATIC_ROOT)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"dashboard=http://{args.host}:{args.port}/", flush=True)
    print(f"sqlite={DashboardHandler.sqlite_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
