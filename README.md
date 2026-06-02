# Whyfi Network Monitor

Whyfi is a laptop-first network diagnostics and monitoring tool. It is designed
for a MacBook that moves between Wi-Fi networks and needs local evidence about
latency, DNS, packet loss/jitter, HTTP request timing, Wi-Fi association, and
collector sleep/wake artifacts.

The goal is to collect enough evidence during a bad window to distinguish among:

- local Wi-Fi or mesh backhaul drops
- gateway/router reachability problems
- upstream ISP outages
- DNS-specific failures
- HTTP/TLS/application-level stalls while basic connectivity still works

Whyfi groups samples into **Networks**. A network profile is based on SSID plus
observed BSSIDs, gateway/network context, and local geolocation when available.
The same SSID can split into separate profiles when it appears in a different
place. Profiles are automatic, and aliases can be edited in the dashboard.

## Quick Start

Run the monitor while using the network normally:

```sh
python3 scripts/whyfi_monitor.py
```

It writes probe samples to SQLite:

```text
data/whyfi.sqlite
```

Let it run for a few hours, ideally through at least one outage. Stop it with
`Ctrl-C`.

For more frequent probes:

```sh
python3 scripts/whyfi_monitor.py --interval 5
```

If gateway auto-detection fails, pass it explicitly:

```sh
python3 scripts/whyfi_monitor.py --gateway 192.168.4.1
```

## Run Continuously With launchd

Install a user LaunchAgent:

```sh
scripts/install_launch_agent.sh
```

This starts `local.whyfi.monitor` at login and keeps it running. It appends probe samples to:

```text
data/whyfi.sqlite
```

Check status:

```sh
launchctl print gui/$(id -u)/local.whyfi.monitor
```

Stop and remove the LaunchAgent:

```sh
scripts/uninstall_launch_agent.sh
```

## Reading Results

Summarize a completed or in-progress run with:

```sh
python3 scripts/summarize_whyfi.py
```

Run the local dashboard:

```sh
scripts/run_dashboard.sh
open http://127.0.0.1:8765/
```

The dashboard reads `data/whyfi.sqlite` and refreshes every 30 seconds. It
defaults to the current network, lets you select past networks, and lets you edit
the selected network alias. It shows current Wi-Fi association, gateway/WAN
latency, HTTP timing summaries, burst-ping jitter, interface usage, and recent
anomaly windows.

Legacy CSV files can be imported into SQLite with:

```sh
python3 scripts/import_legacy_csv.py path/to/legacy.csv
```

Optional local experiment markers can be tracked in:

```text
notes/experiments.md
```

That file is ignored by git because it may contain timestamps, SSIDs, BSSIDs,
locations, and other local network details.

Each sample has:

- `timestamp`: local ISO timestamp with timezone
- `cycle`: sample number
- `probe`: probe type, such as `ping`, `dns`, or `http`
- `target`: host or URL tested
- `ok`: `true` or `false`
- `latency_ms`: elapsed time when available
- `detail`: error or status details

The monitor also records aggregate interface usage samples:

```text
probe=usage,target=interface:en0,detail=rx_bytes=...,tx_bytes=...,rx_bps=...,tx_bps=...
```

These are sampled from macOS interface counters and represent total network
traffic through the laptop's active interface, not per-app usage. They are useful for
answering questions like "was the laptop actively downloading/uploading during
this slow window?" and "did traffic stall even though DNS and ping probes passed?"

Collector health samples are recorded as `monitor,loop_timing`:

```text
probe=monitor,target=loop_timing,detail=started_at=...,cycle_start_gap_s=...,previous_cycle_runtime_s=...
```

Use these to avoid over-interpreting a long collector cycle as a network-wide
event. Detailed HTTP probes run sequentially, not simultaneously; a broad
multi-target spike is stronger evidence when loop timing is normal.

The monitor also compares wall-clock cycle spacing against the configured
interval. If the laptop sleeps, such as when the lid is closed, the next
`monitor,loop_timing` sample includes:

```text
wall_start_gap_s=...,wall_start_gap_over_interval_s=...,sleep_likely=true,sleep_gap_s=...
```

Treat probe failures or latency spikes around those rows as sleep/wake artifacts
unless other devices were affected at the same time.

Burst ping samples are recorded as `ping_burst`:

```text
probe=ping_burst,target=1.1.1.1,latency_ms=31.4,detail=transmitted=20,received=20,loss_pct=0.0,avg_ms=...,jitter_ms=...
```

These send a short packet train instead of one ICMP packet, so they can catch
brief loss and jitter that normal 15-second single pings miss. The LaunchAgent
currently runs 8-packet burst probes to the gateway and `1.1.1.1` every 9 cycles.

Detailed request timing probes are recorded as `http_timing` samples. The LaunchAgent
currently requests a few representative URLs once per minute and logs curl's
request phases:

```text
probe=http_timing,target=https://www.apple.com/,latency_ms=842.1,detail=http_code=200,remote_ip=...,namelookup_ms=...,connect_ms=...,appconnect_ms=...,starttransfer_ms=...,total_ms=...
```

These probes now default to `cache_mode=headers`: they send `Cache-Control:
no-cache` and `Pragma: no-cache` headers but do not append a unique query string.
This is less likely to force cold-origin fetches on static CDNs than the earlier
query-param cache busting, while still asking intermediaries to revalidate. Use
`--http-cache-mode query` for stronger cache busting or `--http-cache-mode none`
for the closest browser-cache-like fixed URL timing. Each detailed timing sample
also includes its actual `started_at` in the detail field because rows in a cycle
are written after the cycle completes.

Useful patterns:

- Gateway ping fails: likely local Wi-Fi, mesh, router, or LAN issue.
- Gateway works but public IP pings fail: likely router uplink, modem, or ISP path issue.
- IP pings work but DNS fails: likely DNS resolver issue.
- OS DNS fails but `dns_server` probes work: likely local resolver/cache behavior.
- `dns_server` probes fail for one configured server only: likely resolver-specific trouble.
- Ping and DNS work but HTTP fails: likely packet loss, latency spikes, TLS/HTTP path issues, or captive/security filtering.
- `http_timing` has high `namelookup_ms`: DNS or local resolver delay.
- `http_timing` has high `connect_ms`: TCP path, packet loss, or remote reachability delay.
- `http_timing` has high `appconnect_ms`: TLS handshake delay.
- `http_timing` has high `starttransfer_ms` but normal connect/TLS: server/CDN wait or request path delay.
- `http_timing` has normal `starttransfer_ms` but high `total_ms`: download throughput or transfer stall.
- Slow `http_timing` samples accompanied by high `monitor loop_timing` gaps:
  possible collector scheduling/runtime artifact; treat as lower-confidence.
- `ping_burst` shows packet loss or high jitter while single pings look normal:
  likely bursty loss, airtime contention, bufferbloat, or path jitter.
- `usage` samples show near-zero throughput during a user-visible stall: the laptop
  was probably blocked waiting on DNS/TCP/TLS/server response rather than moving data slowly.
- `usage` samples show high throughput during a stall: another tab/app/download,
  iCloud sync, backup, or update may have been competing for airtime or uplink.
- `state` samples show IP, gateway MAC, or DNS server changes: likely roaming, DHCP renewal, router topology changes, or network reconfiguration.

## Privileged Wireless Diagnostics

macOS exposes deeper Wi-Fi details through privileged tools such as:

```sh
sudo wdutil info
sudo wdutil diagnose -q -f data/wireless-diagnostics
```

Those can be useful for RSSI/noise/channel/MCS and system Wi-Fi logs, but they are not enabled in the LaunchAgent by default because they require admin privileges, can collect sensitive local network metadata, and may produce large diagnostic bundles. A good workflow is to run `sudo wdutil diagnose` manually during or immediately after a bad window, then compare its timestamp with `data/whyfi.sqlite`.

Capture a timestamped baseline:

```sh
scripts/capture_wireless_diagnostics.sh
```

For lightweight current association details, the monitor uses a tiny Location-authorized app helper at:

```text
bin/WhyfiWiFiStatus.app
```

That helper is launched by the user LaunchAgent and writes live Wi-Fi association fields, including BSSID, into `wifi_association` state rows. `sudo wdutil info` is still available as a fallback for privileged metrics, but it redacts SSID/BSSID in non-interactive launchd execution on this macOS build.

Enable privileged `wdutil info` fallback metrics with a narrow sudoers rule:

```sh
scripts/install_privileged_wifi_sudoers.sh
```

Remove that permission later with:

```sh
scripts/uninstall_privileged_wifi_sudoers.sh
```

## Suggested Manual Checks

During or right after a bad window, note:

- current time and duration
- whether other devices on the same Wi-Fi network are affected
- whether wired devices, if any, are affected
- which access point or mesh node the laptop is connected to, if visible in your router app
- whether the router app reports WAN outage, node offline, or weak mesh/backhaul

If the data points to Wi-Fi or mesh/backhaul, the next practical test is a
temporary Ethernet cable or moving one access point to compare behavior.

## Device Inventory

Optional router/access-point identifiers can be tracked in:

```text
inventory/access-points.yaml
```

Use a local inventory file to map monitor observations like gateway MACs and
Wi-Fi BSSIDs back to physical devices. The `inventory/` directory is ignored by
git because it can contain exact SSIDs, BSSIDs, local device names, and location
notes.
