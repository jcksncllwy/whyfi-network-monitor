const colors = {
  gateway: "#2864a6",
  wan: "#13847d",
  http: "#b7791f",
  rx: "#6750a4",
  tx: "#b42318",
  wifi: "#2f7d57",
};

const state = {
  data: null,
  refreshTimer: null,
  selectedNetworkId: "current",
};

document.getElementById("refreshButton").addEventListener("click", load);
document.getElementById("windowSelect").addEventListener("change", load);
document.getElementById("networkSelect").addEventListener("change", (event) => {
  state.selectedNetworkId = event.target.value;
  load();
});
document.getElementById("saveAliasButton").addEventListener("click", saveAlias);
window.addEventListener("resize", () => renderCharts(state.data));

load();
state.refreshTimer = setInterval(load, 30000);

async function load() {
  try {
    const hours = document.getElementById("windowSelect").value;
    const network = state.selectedNetworkId || "current";
    const response = await fetch(
      `/api/summary?hours=${encodeURIComponent(hours)}&network_id=${encodeURIComponent(network)}`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(`API ${response.status}`);
    state.data = await response.json();
    render(state.data);
  } catch (error) {
    document.getElementById("subtitle").textContent = `Dashboard API unavailable: ${error.message}`;
  }
}

function render(data) {
  const latest = data.latest_timestamp ? formatTime(data.latest_timestamp) : "no rows";
  const network = data.selected_network || {};
  const networkName = networkDisplayName(network);
  document.getElementById("subtitle").textContent =
    `${networkName} · ${data.row_count.toLocaleString()} rows in ${data.window_hours}h window · latest ${latest}`;
  renderNetworkControls(data);
  renderStatus(data);
  renderAnomalies(data.anomalies || []);
  renderHttpTable(data.summary?.http_timing || []);
  renderWifiDetails(data.state?.wifi || {});
  renderCharts(data);
}

function renderNetworkControls(data) {
  const select = document.getElementById("networkSelect");
  const selected = data.selected_network?.id ? String(data.selected_network.id) : "current";
  const options = [`<option value="current"${state.selectedNetworkId === "current" ? " selected" : ""}>Current network</option>`]
    .concat(
      (data.networks || []).map((network) => {
        const value = String(network.id);
        const isSelected = state.selectedNetworkId !== "current" && value === selected;
        return `<option value="${escapeHtml(value)}"${isSelected ? " selected" : ""}>${escapeHtml(networkDisplayName(network))}</option>`;
      }),
    )
    .join("");
  select.innerHTML = options;
  document.getElementById("networkAlias").value = data.selected_network?.alias || "";
}

async function saveAlias() {
  const network = state.data?.selected_network;
  if (!network?.id) return;
  const alias = document.getElementById("networkAlias").value;
  const response = await fetch("/api/network_alias", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ network_id: network.id, alias }),
  });
  if (!response.ok) throw new Error(`API ${response.status}`);
  state.selectedNetworkId = String(network.id);
  await load();
}

function renderStatus(data) {
  const ping = findSummary(data.summary?.ping, "192.168.4.1");
  const wanBurst = findSummary(data.summary?.burst, "1.1.1.1");
  const tinyHttp = findSummary(data.summary?.http, "https://www.gstatic.com/generate_204");
  const wifi = data.state?.wifi || {};
  const usage = data.state?.usage || {};
  const loop = data.state?.loop || {};
  const sleepGap = Number(loop.sleep_gap_s);
  const wallGapOver = Number(loop.wall_start_gap_over_interval_s);
  const cards = [
    {
      label: "Gateway",
      value: fmtMs(ping?.p95_ms),
      sub: `${ping?.failures || 0} failures · p95 ping`,
      level: levelFor(ping?.failures, ping?.p95_ms, 50, 150),
    },
    {
      label: "WAN Jitter",
      value: fmtMs(wanBurst?.p95_jitter_ms),
      sub: `${wanBurst?.failures || 0} burst failures · p95 jitter`,
      level: levelFor(wanBurst?.failures, wanBurst?.p95_jitter_ms, 35, 90),
    },
    {
      label: "Tiny HTTP",
      value: fmtMs(tinyHttp?.p95_ms),
      sub: `${tinyHttp?.slow_500 || 0} slow · p95 total`,
      level: levelFor(tinyHttp?.failures, tinyHttp?.p95_ms, 500, 1000),
    },
    {
      label: "Wi-Fi",
      value: wifi.tx_rate || "unknown",
      sub: `${wifi.bssid || "no BSSID"} · RSSI ${wifi.rssi || "?"}`,
      level: wifi.ok === false ? "bad" : "good",
    },
    {
      label: "Collector",
      value: loop.sleep_likely === "true" ? fmtDuration(sleepGap) : fmtDuration(wallGapOver),
      sub: loop.sleep_likely === "true" ? "likely sleep/wake gap" : "cycle gap over interval",
      level: loop.sleep_likely === "true" ? "warn" : levelFor(0, Math.max(0, wallGapOver || 0), 8, 60),
    },
    {
      label: "Usage RX",
      value: fmtBps(Number(usage.rx_bps)),
      sub: "laptop receive rate",
      level: "good",
    },
    {
      label: "Usage TX",
      value: fmtBps(Number(usage.tx_bps)),
      sub: "laptop transmit rate",
      level: "good",
    },
  ];
  document.getElementById("statusGrid").innerHTML = cards
    .map(
      (card) => `
        <div class="card ${card.level}">
          <div class="label">${escapeHtml(card.label)}</div>
          <div class="value">${escapeHtml(card.value)}</div>
          <div class="sub">${escapeHtml(card.sub)}</div>
        </div>
      `,
    )
    .join("");
}

function renderAnomalies(anomalies) {
  document.getElementById("anomalyCount").textContent = `${anomalies.length} recent`;
  const items = anomalies.slice(0, 30);
  document.getElementById("anomalyList").innerHTML = items.length
    ? items
        .map((item) => {
          const cls = item.severity >= 3 ? "bad" : item.severity <= 1 ? "good" : "";
          return `
            <div class="anomaly ${cls}">
              <time>${escapeHtml(formatTime(item.timestamp))} · cycle ${escapeHtml(item.cycle)}</time>
              <ul>${item.findings.map((finding) => `<li>${escapeHtml(finding)}</li>`).join("")}</ul>
            </div>
          `;
        })
        .join("")
    : `<div class="anomaly good"><time>Current window</time><ul><li>No anomalies detected.</li></ul></div>`;
}

function renderHttpTable(rows) {
  document.getElementById("httpTable").innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(shortTarget(row.target))}</td>
          <td>${fmtMs(row.median_ms)}</td>
          <td>${fmtMs(row.p95_ms)}</td>
          <td>${fmtMs(row.max_ms)}</td>
          <td>${row.slow_500}</td>
          <td>${row.slow_1000}</td>
          <td>${row.failures}</td>
        </tr>
      `,
    )
    .join("");
}

function renderWifiDetails(wifi) {
  const entries = [
    ["BSSID", wifi.bssid || "unknown"],
    ["RSSI", wifi.rssi || "unknown"],
    ["Noise", wifi.noise || "unknown"],
    ["Tx Rate", wifi.tx_rate || "unknown"],
    ["PHY", wifi.phy || "unknown"],
    ["Channel", wifi.channel || "unknown"],
  ];
  document.getElementById("wifiDetails").innerHTML = entries
    .map(([label, value]) => `<div class="detail"><b>${escapeHtml(label)}</b>${escapeHtml(value)}</div>`)
    .join("");
}

function renderCharts(data) {
  if (!data) return;
  drawLineChart("latencyChart", [
    { label: "gateway", color: colors.gateway, points: data.series.gateway_ping || [] },
    { label: "WAN", color: colors.wan, points: data.series.wan_ping || [] },
    { label: "HTTP p95", color: colors.http, points: data.series.http_timing_p95 || [] },
  ], "ms");
  renderLegend("latencyLegend", [
    ["gateway", colors.gateway],
    ["WAN", colors.wan],
    ["HTTP p95", colors.http],
  ]);

  drawLineChart("wifiChart", [
    { label: "tx rate", color: colors.wifi, points: data.series.tx_rate || [] },
  ], "Mbps");

  drawLineChart("burstChart", [
    { label: "gateway burst", color: colors.gateway, points: data.series.burst_gateway_avg || [] },
    { label: "WAN burst", color: colors.wan, points: data.series.burst_wan_avg || [] },
  ], "ms");
  renderLegend("burstLegend", [
    ["gateway burst", colors.gateway],
    ["WAN burst", colors.wan],
  ]);
}

function drawLineChart(id, lines, unit) {
  const canvas = document.getElementById(id);
  const parentWidth = canvas.parentElement.clientWidth - 28;
  const width = Math.max(320, parentWidth);
  const height = Number(canvas.getAttribute("height"));
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  const padding = { left: 66, right: 14, top: 12, bottom: 28 };
  const all = lines.flatMap((line) => line.points.map((point) => ({ ...point, time: Date.parse(point.t) })));
  if (!all.length) {
    ctx.fillStyle = "#627067";
    ctx.fillText("No data", 16, 24);
    return;
  }
  const minT = Math.min(...all.map((point) => point.time));
  const maxT = Math.max(...all.map((point) => point.time));
  const maxV = Math.max(1, ...all.map((point) => Number(point.v) || 0));
  const yMax = niceMax(maxV);
  const chartW = width - padding.left - padding.right;
  const chartH = height - padding.top - padding.bottom;
  const x = (time) => padding.left + ((time - minT) / Math.max(1, maxT - minT)) * chartW;
  const y = (value) => padding.top + chartH - (Math.min(value, yMax) / yMax) * chartH;

  ctx.strokeStyle = "#dbe2dd";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 4; i += 1) {
    const yy = padding.top + (chartH * i) / 4;
    ctx.moveTo(padding.left, yy);
    ctx.lineTo(width - padding.right, yy);
  }
  ctx.stroke();

  ctx.fillStyle = "#627067";
  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i += 1) {
    const value = yMax - (yMax * i) / 4;
    ctx.fillText(`${Math.round(value)}${unit}`, padding.left - 8, padding.top + (chartH * i) / 4 + 4);
  }

  for (const line of lines) {
    const points = line.points.filter((point) => point.v !== null && point.v !== undefined);
    if (!points.length) continue;
    ctx.strokeStyle = line.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((point, index) => {
      const xx = x(Date.parse(point.t));
      const yy = y(Number(point.v));
      if (index === 0) ctx.moveTo(xx, yy);
      else ctx.lineTo(xx, yy);
    });
    ctx.stroke();
  }

  ctx.fillStyle = "#627067";
  ctx.textAlign = "left";
  ctx.fillText(formatTime(new Date(minT).toISOString()), padding.left, height - 8);
  ctx.textAlign = "right";
  ctx.fillText(formatTime(new Date(maxT).toISOString()), width - padding.right, height - 8);
}

function renderLegend(id, entries) {
  document.getElementById(id).innerHTML = entries
    .map(([label, color]) => `<span class="legend-item"><span class="swatch" style="background:${color}"></span>${label}</span>`)
    .join("");
}

function findSummary(rows = [], target) {
  return rows.find((row) => row.target === target);
}

function levelFor(failures = 0, p95 = 0, warn, bad) {
  if (failures > 0 || p95 >= bad) return "bad";
  if (p95 >= warn) return "warn";
  return "good";
}

function fmtMs(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  return `${Math.round(Number(value))}ms`;
}

function fmtBps(value) {
  if (!Number.isFinite(value)) return "n/a";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)} Mbps`;
  if (value >= 1_000) return `${Math.round(value / 1_000)} Kbps`;
  return `${Math.round(value)} bps`;
}

function fmtDuration(value) {
  if (!Number.isFinite(value)) return "n/a";
  if (Math.abs(value) >= 120) return `${(value / 60).toFixed(1)}m`;
  return `${Math.round(value)}s`;
}

function niceMax(value) {
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  const nice = normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return nice * magnitude;
}

function shortTarget(target) {
  return String(target).replace("https://", "").replace("www.", "").replace(/\/$/, "");
}

function networkDisplayName(network) {
  if (!network || !network.id) return "Current network";
  const alias = network.alias ? `${network.alias}` : network.ssid || "Unknown network";
  const suffix = network.alias && network.ssid ? ` (${network.ssid})` : "";
  return `${alias}${suffix}`;
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
