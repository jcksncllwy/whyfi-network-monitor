#!/usr/bin/env python3
"""Long-running network probe logger for intermittent connectivity issues."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import whyfi_store


PING_TIME_RE = re.compile(r"time[=<]([0-9.]+)\s*ms")
PING_LOSS_RE = re.compile(r"(\d+)\s+packets transmitted,\s+(\d+)\s+packets received,\s+([0-9.]+)%\s+packet loss")
PING_RTT_RE = re.compile(r"(?:round-trip|rtt) min/avg/max/(?:stddev|mdev) = ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms")


@dataclass
class ProbeResult:
    probe: str
    target: str
    ok: bool
    latency_ms: float | None
    detail: str


@dataclass
class InterfaceCounters:
    rx_packets: int
    rx_errors: int
    rx_bytes: int
    tx_packets: int
    tx_errors: int
    tx_bytes: int


def now_local() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def default_gateway() -> str | None:
    system = platform.system().lower()
    commands: list[list[str]]
    if system == "darwin":
        route = find_command("route", ["/sbin/route"])
        commands = [[route, "-n", "get", "default"]] if route else []
    else:
        ip = find_command("ip", ["/sbin/ip", "/usr/sbin/ip"])
        route = find_command("route", ["/sbin/route", "/usr/sbin/route"])
        commands = []
        if ip:
            commands.append([ip, "route", "show", "default"])
        if route:
            commands.append([route, "-n"])

    for command in commands:
        try:
            proc = subprocess.run(
                command,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        gateway = parse_gateway(proc.stdout)
        if gateway:
            return gateway
    return None


def find_command(name: str, fallback_paths: list[str]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for path in fallback_paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def parse_gateway(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("gateway:"):
            return stripped.split(":", 1)[1].strip()
        if stripped.startswith("default "):
            parts = stripped.split()
            if len(parts) >= 3 and parts[2].count(".") == 3:
                return parts[2]
    return None


def default_interface() -> str | None:
    route = find_command("route", ["/sbin/route"])
    if not route:
        return None
    try:
        proc = subprocess.run(
            [route, "-n", "get", "default"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("interface:"):
            return stripped.split(":", 1)[1].strip()
    return None


def current_ipv4(interface: str) -> str | None:
    ipconfig = find_command("ipconfig", ["/usr/sbin/ipconfig"])
    if not ipconfig:
        return None
    try:
        proc = subprocess.run(
            [ipconfig, "getifaddr", interface],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    address = proc.stdout.strip()
    return address or None


def gateway_mac(gateway: str) -> str | None:
    arp = find_command("arp", ["/usr/sbin/arp"])
    if not arp:
        return None
    try:
        proc = subprocess.run(
            [arp, "-n", gateway],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r" at ([0-9a-f:]{11,17}) ", proc.stdout, re.IGNORECASE)
    return match.group(1).lower() if match else None


def interface_counters(interface: str) -> InterfaceCounters | None:
    netstat = find_command("netstat", ["/usr/sbin/netstat"])
    if not netstat:
        return None
    try:
        proc = subprocess.run(
            [netstat, "-ibn"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    # The <Link#...> row is the physical interface aggregate. Address rows repeat
    # the same counters for each assigned IP and should not be treated as distinct.
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[0] != interface or not parts[2].startswith("<Link#"):
            continue
        try:
            return InterfaceCounters(
                rx_packets=int(parts[4]),
                rx_errors=int(parts[5]),
                rx_bytes=int(parts[6]),
                tx_packets=int(parts[7]),
                tx_errors=int(parts[8]),
                tx_bytes=int(parts[9]),
            )
        except ValueError:
            return None
    return None


def usage_snapshot(
    interface: str | None,
    previous: InterfaceCounters | None,
    elapsed_seconds: float | None,
) -> tuple[ProbeResult | None, InterfaceCounters | None]:
    if not interface:
        return None, previous
    current = interface_counters(interface)
    if not current:
        return ProbeResult("usage", f"interface:{interface}", False, None, "interface counters unavailable"), previous

    fields = [
        f"rx_bytes={current.rx_bytes}",
        f"tx_bytes={current.tx_bytes}",
        f"rx_packets={current.rx_packets}",
        f"tx_packets={current.tx_packets}",
        f"rx_errors={current.rx_errors}",
        f"tx_errors={current.tx_errors}",
    ]
    if previous and elapsed_seconds and elapsed_seconds > 0:
        rx_delta = current.rx_bytes - previous.rx_bytes
        tx_delta = current.tx_bytes - previous.tx_bytes
        rx_packet_delta = current.rx_packets - previous.rx_packets
        tx_packet_delta = current.tx_packets - previous.tx_packets
        rx_error_delta = current.rx_errors - previous.rx_errors
        tx_error_delta = current.tx_errors - previous.tx_errors
        fields.extend(
            [
                f"elapsed_s={elapsed_seconds:.1f}",
                f"rx_delta_bytes={rx_delta}",
                f"tx_delta_bytes={tx_delta}",
                f"rx_bps={(rx_delta * 8) / elapsed_seconds:.0f}",
                f"tx_bps={(tx_delta * 8) / elapsed_seconds:.0f}",
                f"rx_packet_delta={rx_packet_delta}",
                f"tx_packet_delta={tx_packet_delta}",
                f"rx_error_delta={rx_error_delta}",
                f"tx_error_delta={tx_error_delta}",
            ]
        )
    else:
        fields.append("rate=baseline")
    return ProbeResult("usage", f"interface:{interface}", True, None, ",".join(fields)), current


def configured_dns_servers(interface: str | None = None) -> list[str]:
    servers = dns_servers_from_scutil(interface)
    if servers:
        return servers
    if interface:
        return dns_servers_from_ipconfig(interface)
    return []


def dns_servers_from_scutil(interface: str | None) -> list[str]:
    scutil = find_command("scutil", ["/usr/sbin/scutil"])
    if not scutil:
        return []
    try:
        proc = subprocess.run(
            [scutil, "--dns"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    servers: list[str] = []
    pending: list[str] = []
    in_matching_resolver = interface is None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("resolver #"):
            if in_matching_resolver:
                servers.extend(pending)
            pending = []
            in_matching_resolver = interface is None
        elif stripped.startswith("nameserver["):
            pending.append(stripped.split(":", 1)[1].strip())
        elif interface and f"({interface})" in stripped:
            in_matching_resolver = True
    if in_matching_resolver:
        servers.extend(pending)
    return unique_ipv4(servers)


def dns_servers_from_ipconfig(interface: str) -> list[str]:
    ipconfig = find_command("ipconfig", ["/usr/sbin/ipconfig"])
    if not ipconfig:
        return []
    try:
        proc = subprocess.run(
            [ipconfig, "getpacket", interface],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    match = re.search(r"domain_name_server \(ip_mult\): \{([^}]+)\}", proc.stdout)
    if not match:
        return []
    return unique_ipv4(item.strip() for item in match.group(1).split(","))


def unique_ipv4(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value.count(".") != 3 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def monitor_snapshot(
    cycle_started_at: float,
    cycle_wall_started_at: float,
    previous_cycle_started_at: float | None,
    previous_cycle_wall_started_at: float | None,
    previous_cycle_finished_at: float | None,
    interval: float,
    sleep_gap_threshold: float,
) -> ProbeResult:
    fields = [f"started_at={now_local()}", f"interval_s={interval:.1f}"]
    if previous_cycle_started_at is None:
        fields.append("cycle_start_gap_s=baseline")
    else:
        gap = cycle_started_at - previous_cycle_started_at
        fields.extend([f"cycle_start_gap_s={gap:.1f}", f"cycle_start_gap_over_interval_s={gap - interval:.1f}"])
    if previous_cycle_wall_started_at is None:
        fields.append("wall_start_gap_s=baseline")
    else:
        wall_gap = cycle_wall_started_at - previous_cycle_wall_started_at
        wall_gap_over = wall_gap - interval
        fields.extend([f"wall_start_gap_s={wall_gap:.1f}", f"wall_start_gap_over_interval_s={wall_gap_over:.1f}"])
        if wall_gap_over >= sleep_gap_threshold:
            fields.extend([f"sleep_likely=true", f"sleep_gap_s={wall_gap_over:.1f}"])
    if previous_cycle_finished_at is None:
        fields.append("previous_cycle_runtime_s=baseline")
    else:
        runtime = previous_cycle_finished_at - previous_cycle_started_at if previous_cycle_started_at else 0
        idle = cycle_started_at - previous_cycle_finished_at
        fields.extend([f"previous_cycle_runtime_s={runtime:.1f}", f"idle_s={idle:.1f}"])
    return ProbeResult("monitor", "loop_timing", True, None, ",".join(fields))


def state_snapshot(
    gateway: str | None,
    interface: str | None,
    privileged_wifi: bool = False,
    include_wifi: bool = True,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    if interface:
        ipv4 = current_ipv4(interface)
        results.append(ProbeResult("state", f"interface:{interface}", bool(ipv4), None, f"ipv4={ipv4 or 'none'}"))
    if include_wifi:
        wifi_result = wifi_association_result(privileged_wifi)
        if wifi_result:
            results.append(wifi_result)
    if gateway:
        mac = gateway_mac(gateway)
        results.append(ProbeResult("state", f"gateway_mac:{gateway}", bool(mac), None, mac or "unknown"))
    dns_servers = configured_dns_servers(interface)
    results.append(
        ProbeResult("state", "dns_servers", bool(dns_servers), None, ",".join(dns_servers) if dns_servers else "none")
    )
    return results


def wifi_association_result(privileged_wifi: bool = False) -> ProbeResult | None:
    wifi = helper_app_wifi_status()
    if not wifi and privileged_wifi:
        wifi = wdutil_wifi_status()
    if not wifi:
        wifi = wifi_status()
    if not wifi:
        return None
    return ProbeResult(
        "state",
        "wifi_association",
        bool(wifi.get("ssid")),
        None,
        ",".join(f"{key}={value}" for key, value in wifi.items()),
    )


def helper_app_wifi_status() -> dict[str, str]:
    app = Path("bin/WhyfiWiFiStatus.app")
    if not app.exists():
        return {}
    out_path = Path("/tmp/whyfi-wifi-status-helper.txt")
    command = ["open", "-W", str(app), "--args", "--out", str(out_path)]
    try:
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0 or not out_path.exists():
        return {}
    try:
        output = out_path.read_text().strip()
    except OSError:
        return {}
    return parse_key_value_detail(output)


def parse_key_value_detail(output: str) -> dict[str, str]:
    first_line = output.splitlines()[0] if output else ""
    data: dict[str, str] = {}
    for item in first_line.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        data[key] = value
    return data


def wdutil_wifi_status() -> dict[str, str]:
    wdutil = find_command("wdutil", ["/usr/bin/wdutil"])
    if not wdutil:
        return {}
    command = [wdutil, "info"] if os.geteuid() == 0 else ["sudo", "-n", wdutil, "info"]
    try:
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    return parse_wdutil_wifi_info(proc.stdout)


def parse_wdutil_wifi_info(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    in_wifi = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "WIFI":
            in_wifi = True
            continue
        if in_wifi and stripped in {"BLUETOOTH", "AWDL", "POWER", "WIFI FAULTS LAST HOUR"}:
            break
        if not in_wifi or ":" not in stripped:
            continue
        key, value = [part.strip() for part in stripped.split(":", 1)]
        if key == "SSID":
            data["ssid"] = compact_value(value)
        elif key == "BSSID":
            data["bssid"] = compact_value(value).lower()
        elif key == "RSSI":
            data["rssi"] = compact_value(value)
        elif key == "CCA":
            data["cca"] = compact_value(value)
        elif key == "Noise":
            data["noise"] = compact_value(value)
        elif key == "Tx Rate":
            data["tx_rate"] = compact_value(value)
        elif key == "PHY Mode":
            data["phy"] = compact_value(value)
        elif key == "MCS Index":
            data["mcs"] = compact_value(value)
        elif key == "NSS":
            data["nss"] = compact_value(value)
        elif key == "Channel":
            data["channel"] = compact_value(value)
    return data


def compact_value(value: str) -> str:
    return value.strip().replace(" ", "")


def wifi_status() -> dict[str, str]:
    profiler = find_command("system_profiler", ["/usr/sbin/system_profiler"])
    if not profiler:
        return {}
    try:
        proc = subprocess.run(
            [profiler, "SPAirPortDataType"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    data: dict[str, str] = {}
    in_current = False
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped == "Current Network Information:":
            in_current = True
            continue
        if not in_current:
            continue
        if stripped.endswith(":") and "ssid" not in data:
            data["ssid"] = stripped[:-1]
            continue
        if stripped.startswith("PHY Mode:"):
            data["phy"] = stripped.split(":", 1)[1].strip().replace(" ", "")
        elif stripped.startswith("Channel:"):
            data["channel"] = stripped.split(":", 1)[1].strip().replace(" ", "")
        elif stripped.startswith("Signal / Noise:"):
            data["signal_noise"] = stripped.split(":", 1)[1].strip().replace(" ", "")
        elif stripped.startswith("Transmit Rate:"):
            data["tx_rate"] = stripped.split(":", 1)[1].strip().replace(" ", "")
        elif stripped.startswith("MCS Index:"):
            data["mcs"] = stripped.split(":", 1)[1].strip().replace(" ", "")
        elif stripped == "Other Local Wi-Fi Networks:":
            break
    return data


def ping_once(host: str, timeout: float) -> ProbeResult:
    ping = find_command("ping", ["/sbin/ping", "/bin/ping", "/usr/sbin/ping"])
    if not ping:
        return ProbeResult("ping", host, False, None, "ping command not found")

    system = platform.system().lower()
    if system == "darwin":
        command = [ping, "-n", "-c", "1", "-W", str(int(timeout * 1000)), host]
    else:
        command = [ping, "-n", "-c", "1", "-W", str(max(1, int(timeout))), host]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult("ping", host, False, None, type(exc).__name__)

    elapsed_ms = (time.monotonic() - start) * 1000
    output = f"{proc.stdout}\n{proc.stderr}".strip()
    match = PING_TIME_RE.search(output)
    latency = float(match.group(1)) if match else elapsed_ms
    ok = proc.returncode == 0
    detail = "ok" if ok else last_line(output) or f"exit {proc.returncode}"
    return ProbeResult("ping", host, ok, latency if ok else None, detail)


def ping_burst(host: str, count: int, interval: float, timeout: float) -> ProbeResult:
    ping = find_command("ping", ["/sbin/ping", "/bin/ping", "/usr/sbin/ping"])
    if not ping:
        return ProbeResult("ping_burst", host, False, None, "ping command not found")

    system = platform.system().lower()
    if system == "darwin":
        command = [
            ping,
            "-n",
            "-c",
            str(count),
            "-i",
            f"{interval:.3f}",
            "-W",
            str(int(timeout * 1000)),
            host,
        ]
    else:
        command = [
            ping,
            "-n",
            "-c",
            str(count),
            "-i",
            f"{interval:.3f}",
            "-W",
            str(max(1, int(timeout))),
            host,
        ]

    start = time.monotonic()
    command_timeout = max(timeout + 1, count * interval + timeout + 2)
    try:
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=command_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult("ping_burst", host, False, None, type(exc).__name__)

    elapsed_ms = (time.monotonic() - start) * 1000
    output = f"{proc.stdout}\n{proc.stderr}".strip()
    loss_match = PING_LOSS_RE.search(output)
    rtt_match = PING_RTT_RE.search(output)
    transmitted = received = None
    loss_pct = None
    if loss_match:
        transmitted = int(loss_match.group(1))
        received = int(loss_match.group(2))
        loss_pct = float(loss_match.group(3))
    if rtt_match:
        min_ms, avg_ms, max_ms, jitter_ms = (float(rtt_match.group(i)) for i in range(1, 5))
    else:
        min_ms = avg_ms = max_ms = jitter_ms = None

    fields = [f"duration_ms={elapsed_ms:.1f}"]
    if transmitted is not None and received is not None and loss_pct is not None:
        fields.extend([f"transmitted={transmitted}", f"received={received}", f"loss_pct={loss_pct:.1f}"])
    if avg_ms is not None:
        fields.extend(
            [
                f"min_ms={min_ms:.1f}",
                f"avg_ms={avg_ms:.1f}",
                f"max_ms={max_ms:.1f}",
                f"jitter_ms={jitter_ms:.1f}",
            ]
        )
    else:
        fields.append(last_line(output) or f"exit {proc.returncode}")

    ok = proc.returncode == 0 and (loss_pct is None or loss_pct == 0)
    return ProbeResult("ping_burst", host, ok, avg_ms, ",".join(fields))


def dns_lookup(host: str, timeout: float) -> ProbeResult:
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    start = time.monotonic()
    try:
        results = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        socket.setdefaulttimeout(original_timeout)
        return ProbeResult("dns", host, False, None, f"{type(exc).__name__}: {exc}")
    finally:
        socket.setdefaulttimeout(original_timeout)

    elapsed_ms = (time.monotonic() - start) * 1000
    addresses = sorted({item[4][0] for item in results})
    return ProbeResult("dns", host, True, elapsed_ms, ",".join(addresses[:3]))


def dns_server_lookup(server: str, host: str, timeout: float) -> ProbeResult:
    dig = find_command("dig", ["/usr/bin/dig"])
    target = f"{host}@{server}"
    if not dig:
        return ProbeResult("dns_server", target, False, None, "dig command not found")
    command = [
        dig,
        f"@{server}",
        host,
        "A",
        "+short",
        "+tries=1",
        f"+time={max(1, int(timeout))}",
    ]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult("dns_server", target, False, None, type(exc).__name__)
    elapsed_ms = (time.monotonic() - start) * 1000
    answers = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    ok = proc.returncode == 0 and bool(answers)
    detail = ",".join(answers[:3]) if ok else last_line(proc.stderr) or f"exit {proc.returncode}"
    return ProbeResult("dns_server", target, ok, elapsed_ms if ok else None, detail)


def http_request(url: str, timeout: float) -> ProbeResult:
    curl = find_command("curl", ["/usr/bin/curl"])
    if curl:
        return curl_request(curl, url, timeout)
    return urllib_request(url, timeout)


def curl_request(curl: str, url: str, timeout: float) -> ProbeResult:
    command = [
        curl,
        "--silent",
        "--show-error",
        "--location",
        "--output",
        os.devnull,
        "--write-out",
        "%{http_code}",
        "--max-time",
        str(timeout),
        url,
    ]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult("http", url, False, None, type(exc).__name__)

    elapsed_ms = (time.monotonic() - start) * 1000
    status_text = proc.stdout.strip()
    try:
        status = int(status_text)
    except ValueError:
        status = 0
    ok = proc.returncode == 0 and 200 <= status < 400
    detail = f"HTTP {status}" if status else last_line(proc.stderr) or f"exit {proc.returncode}"
    return ProbeResult("http", url, ok, elapsed_ms if ok else None, detail)


def http_timing_request(url: str, timeout: float, cache_mode: str = "headers") -> ProbeResult:
    curl = find_command("curl", ["/usr/bin/curl"])
    if not curl:
        return ProbeResult("http_timing", url, False, None, "curl command not found")

    request_url = add_cache_buster(url) if cache_mode == "query" else url
    started_at = now_local()
    write_out = (
        "http_code=%{http_code},"
        "remote_ip=%{remote_ip},"
        "namelookup_ms=%{time_namelookup},"
        "connect_ms=%{time_connect},"
        "appconnect_ms=%{time_appconnect},"
        "pretransfer_ms=%{time_pretransfer},"
        "starttransfer_ms=%{time_starttransfer},"
        "total_ms=%{time_total},"
        "size_download=%{size_download},"
        "speed_download=%{speed_download}"
    )
    command = [
        curl,
        "--silent",
        "--show-error",
        "--location",
        "--output",
        os.devnull,
        "--write-out",
        write_out,
        "--max-time",
        str(timeout),
        "--user-agent",
        "whyfi/1.0",
    ]
    if cache_mode in {"headers", "query"}:
        command.extend(["--header", "Cache-Control: no-cache", "--header", "Pragma: no-cache"])
    command.append(request_url)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult("http_timing", url, False, None, type(exc).__name__)

    elapsed_ms = (time.monotonic() - start) * 1000
    metrics = parse_key_value_detail(proc.stdout)
    metrics["started_at"] = started_at
    status = parse_int(metrics.get("http_code", ""))
    total_ms = seconds_metric_to_ms(metrics.get("total_ms")) or elapsed_ms
    ok = proc.returncode == 0 and status is not None and 200 <= status < 400
    if proc.returncode != 0:
        detail = last_line(proc.stderr) or f"exit {proc.returncode}"
        if metrics:
            detail = f"{detail},{format_timing_metrics(metrics, cache_mode)}"
        return ProbeResult("http_timing", url, False, total_ms, detail)
    return ProbeResult("http_timing", url, ok, total_ms, format_timing_metrics(metrics, cache_mode))


def add_cache_buster(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("whyfi_bust", f"{int(time.time() * 1000)}-{os.getpid()}"))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def format_timing_metrics(metrics: dict[str, str], cache_mode: str) -> str:
    formatted: list[str] = []
    for key, value in metrics.items():
        if key.endswith("_ms"):
            ms = seconds_metric_to_ms(value)
            formatted.append(f"{key}={ms:.1f}" if ms is not None else f"{key}={value}")
        else:
            formatted.append(f"{key}={value}")
    formatted.append(f"cache_mode={cache_mode}")
    return ",".join(formatted)


def seconds_metric_to_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value) * 1000
    except ValueError:
        return None


def parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def resolve_burst_ping_targets(targets: list[str], gateway: str | None) -> list[str]:
    resolved: list[str] = []
    for target in targets:
        value = gateway if target == "gateway" else target
        if value and value not in resolved:
            resolved.append(value)
    return resolved


def urllib_request(url: str, timeout: float) -> ProbeResult:
    request = urllib.request.Request(url, headers={"User-Agent": "whyfi/1.0"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        ok = 200 <= exc.code < 400
        return ProbeResult("http", url, ok, elapsed_ms if ok else None, f"HTTP {exc.code}")
    except (OSError, urllib.error.URLError) as exc:
        return ProbeResult("http", url, False, None, f"{type(exc).__name__}: {exc}")

    elapsed_ms = (time.monotonic() - start) * 1000
    ok = 200 <= status < 400
    return ProbeResult("http", url, ok, elapsed_ms if ok else None, f"HTTP {status}")


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def result_rows(cycle: int, results: list[ProbeResult]) -> list[dict[str, object]]:
    timestamp = now_local()
    rows: list[dict[str, object]] = []
    for result in results:
        rows.append(
            {
                "timestamp": timestamp,
                "cycle": cycle,
                "probe": result.probe,
                "target": result.target,
                "ok": str(result.ok).lower(),
                "latency_ms": f"{result.latency_ms:.1f}" if result.latency_ms is not None else "",
                "detail": result.detail,
            }
        )
    return rows


def append_results(path: Path, rows: list[dict[str, object]]) -> None:
    conn = whyfi_store.connect(path)
    try:
        whyfi_store.insert_rows(conn, rows)
    finally:
        conn.close()


def summarize(results: list[ProbeResult]) -> str:
    failed = [result for result in results if not result.ok]
    if not failed:
        slow = [result for result in results if result.latency_ms is not None and result.latency_ms > 500]
        if slow:
            return "slow: " + ", ".join(f"{item.probe}:{item.target} {item.latency_ms:.0f}ms" for item in slow)
        return "ok"
    return "fail: " + ", ".join(f"{item.probe}:{item.target} ({item.detail})" for item in failed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between probe cycles")
    parser.add_argument("--timeout", type=float, default=3.0, help="per-probe timeout in seconds")
    parser.add_argument("--sqlite", type=Path, default=Path("data/whyfi.sqlite"), help="SQLite output path")
    parser.add_argument("--gateway", help="gateway/router IP; auto-detected when omitted")
    parser.add_argument("--interface", help="network interface; auto-detected when omitted")
    parser.add_argument("--state-interval", type=int, default=4, help="cycles between local state snapshots")
    parser.add_argument(
        "--wifi-state-interval",
        type=int,
        default=7,
        help="cycles between Wi-Fi association snapshots; separate from state snapshots to avoid probe coupling",
    )
    parser.add_argument(
        "--privileged-wifi",
        action="store_true",
        help="use wdutil info for BSSID-capable Wi-Fi state; requires privileged execution",
    )
    parser.add_argument(
        "--ping-target",
        action="append",
        default=["1.1.1.1", "8.8.8.8"],
        help="public IP to ping; can be passed more than once",
    )
    parser.add_argument(
        "--dns-target",
        action="append",
        default=["www.apple.com", "www.google.com"],
        help="hostname to resolve; can be passed more than once",
    )
    parser.add_argument(
        "--dns-server-target",
        default="www.google.com",
        help="hostname to query directly against configured DNS servers",
    )
    parser.add_argument(
        "--http-target",
        action="append",
        default=["https://www.gstatic.com/generate_204"],
        help="URL to request; can be passed more than once",
    )
    parser.add_argument(
        "--http-timing-target",
        action="append",
        default=[],
        help="URL to request with detailed curl timing; can be passed more than once",
    )
    parser.add_argument(
        "--http-timing-interval",
        type=int,
        default=4,
        help="cycles between detailed HTTP timing probes",
    )
    parser.add_argument(
        "--http-timing-timeout",
        type=float,
        help="per-request timeout for detailed HTTP timing probes; defaults to --timeout",
    )
    parser.add_argument(
        "--http-cache-mode",
        choices=["headers", "query", "none"],
        default="headers",
        help="cache handling for detailed HTTP timing: headers sends no-cache headers; query also appends a unique query param",
    )
    parser.add_argument(
        "--burst-ping-target",
        action="append",
        default=["gateway", "1.1.1.1"],
        help="target for periodic burst ping loss/jitter probes; use 'gateway' for the detected router",
    )
    parser.add_argument(
        "--burst-ping-interval",
        type=int,
        default=9,
        help="cycles between burst ping probes",
    )
    parser.add_argument(
        "--burst-ping-count",
        type=int,
        default=8,
        help="packets per burst ping probe",
    )
    parser.add_argument(
        "--burst-ping-spacing",
        type=float,
        default=0.2,
        help="seconds between packets in a burst ping probe",
    )
    parser.add_argument(
        "--sleep-gap-threshold",
        type=float,
        default=60.0,
        help="seconds over the configured interval before a wall-clock cycle gap is marked as likely sleep",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    gateway = args.gateway or default_gateway()
    interface = args.interface or default_interface()
    whyfi_store.connect(args.sqlite).close()

    if gateway:
        print(f"gateway={gateway}", flush=True)
    else:
        print("gateway=unknown; pass --gateway if local reachability should be tracked", flush=True)
    print(f"interface={interface or 'unknown'}", flush=True)
    print(f"sqlite={args.sqlite}", flush=True)

    cycle = 0
    previous_counters: InterfaceCounters | None = None
    previous_counters_at: float | None = None
    previous_cycle_started_at: float | None = None
    previous_cycle_wall_started_at: float | None = None
    previous_cycle_finished_at: float | None = None
    while True:
        cycle += 1
        cycle_started_at = time.monotonic()
        cycle_wall_started_at = time.time()
        results: list[ProbeResult] = [
            monitor_snapshot(
                cycle_started_at,
                cycle_wall_started_at,
                previous_cycle_started_at,
                previous_cycle_wall_started_at,
                previous_cycle_finished_at,
                args.interval,
                args.sleep_gap_threshold,
            )
        ]
        if gateway:
            results.append(ping_once(gateway, args.timeout))
        for target in args.ping_target:
            results.append(ping_once(target, args.timeout))
        for target in args.dns_target:
            results.append(dns_lookup(target, args.timeout))
        for server in configured_dns_servers(interface):
            results.append(dns_server_lookup(server, args.dns_server_target, args.timeout))
        for target in args.http_target:
            results.append(http_request(target, args.timeout))
        if args.http_timing_interval > 0 and (
            cycle == 1 or cycle % args.http_timing_interval == 0
        ):
            http_timing_timeout = args.http_timing_timeout or args.timeout
            for target in args.http_timing_target:
                results.append(http_timing_request(target, http_timing_timeout, args.http_cache_mode))
        if args.burst_ping_interval > 0 and cycle % args.burst_ping_interval == 0:
            for target in resolve_burst_ping_targets(args.burst_ping_target, gateway):
                results.append(ping_burst(target, args.burst_ping_count, args.burst_ping_spacing, args.timeout))
        elapsed = cycle_started_at - previous_counters_at if previous_counters_at is not None else None
        usage_result, previous_counters = usage_snapshot(interface, previous_counters, elapsed)
        previous_counters_at = cycle_started_at
        if usage_result:
            results.append(usage_result)
        if args.state_interval > 0 and (cycle == 1 or cycle % args.state_interval == 0):
            results.extend(state_snapshot(gateway, interface, args.privileged_wifi, include_wifi=False))
        if args.wifi_state_interval > 0 and (cycle == 1 or cycle % args.wifi_state_interval == 0):
            wifi_result = wifi_association_result(args.privileged_wifi)
            if wifi_result:
                results.append(wifi_result)

        rows = result_rows(cycle, results)
        try:
            append_results(args.sqlite, rows)
        except Exception as exc:
            print(f"{now_local()} sqlite_write_error={type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        print(f"{now_local()} cycle={cycle} {summarize(results)}", flush=True)
        previous_cycle_started_at = cycle_started_at
        previous_cycle_wall_started_at = cycle_wall_started_at
        previous_cycle_finished_at = time.monotonic()
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        raise SystemExit(130)
