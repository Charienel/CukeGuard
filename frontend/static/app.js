const API = "/api";
let climateChart;
let latestAlert = null;
let nextAutomaticScanAt = null;

function parseApiDate(value) {
  if (!value) return null;
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  return new Date(timestamp);
}

function updateClock() {
  const now = new Date();
  document.getElementById("live-clock").textContent = now.toLocaleTimeString();
  document.getElementById("live-date").textContent = now.toLocaleDateString(undefined, {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
  });
}

function updateScheduleDisplay() {
  const output = document.getElementById("next-scan-countdown");
  if (!nextAutomaticScanAt) {
    output.textContent = "Waiting for schedule…";
    return;
  }

  const secondsRemaining = Math.max(0, Math.ceil((nextAutomaticScanAt - Date.now()) / 1000));
  if (secondsRemaining === 0) {
    output.textContent = "Capture due";
    return;
  }
  const minutes = Math.floor(secondsRemaining / 60);
  const seconds = secondsRemaining % 60;
  output.textContent = `${minutes}:${String(seconds).padStart(2, "0")} remaining`;
}

function updateAlertBanner() {
  const banner = document.getElementById("status-banner");
  const text = document.getElementById("status-text");
  const age = latestAlert ? Date.now() - parseApiDate(latestAlert.sent_at).getTime() : Infinity;
  if (age < 0 || age >= 30000) {
    banner.classList.remove("alert");
    return;
  }

  banner.classList.remove("correcting", "limited");
  banner.classList.add("alert");
  const status = latestAlert.delivery_status;
  const delivery = status.startsWith("failed")
    ? `SMS ${status}`
    : ({
        submitted: "SMS accepted by provider",
        simulated: "SMS simulated, not delivered",
      }[status] || "SMS status unknown");
  text.textContent = `Bad cucumber detected — ${delivery} to ${latestAlert.phone_number}`;
}

async function fetchJSON(path, opts) {
  const res = await fetch(API + path, opts);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function loadBatch() {
  try {
    const batch = await fetchJSON("/batch/active");
    document.getElementById("batch-label").textContent =
      `B-${String(batch.id).padStart(4, "0")} · ${batch.batch_code}`;
  } catch (e) { console.warn(e); }
}

async function loadSystemStatus() {
  try {
    const status = await fetchJSON("/system/status");
    const simulated = status.mode === "simulation";
    document.getElementById("system-mode-label").textContent = simulated ? "SIMULATION MODE" : "HARDWARE MODE";
    const smsMode = status.components.sms;
    const smsDetail = smsMode === "semaphore"
      ? "Semaphore SMS configured"
      : smsMode === "iprog"
        ? "iProg SMS configured"
        : smsMode.endsWith("_unconfigured")
          ? `${smsMode.split("_")[0]} selected; API token missing`
          : "SMS simulated";
    document.getElementById("system-mode-detail").textContent = simulated
      ? `Sensors, camera and controls simulated · ${smsDetail}`
      : `Connected hardware readings and controls · ${smsDetail}`;
    document.getElementById("manual-scan-btn").textContent = simulated ? "Run Demo Scan" : "Run Scan";
    document.getElementById("vision-title").textContent = simulated ? "Latest Scan (Simulated)" : "Latest Scan";

    const temp = status.climate_ranges.temperature_c;
    const humidity = status.climate_ranges.humidity_pct;
    document.getElementById("target-temp").textContent = `Target ${temp.min}–${temp.max}°C`;
    document.getElementById("target-humidity").textContent = `Target ${humidity.min}–${humidity.max}% RH`;
    document.getElementById("climate-range-chip").textContent =
      `${temp.min}–${temp.max}°C · ${humidity.min}–${humidity.max}% RH`;
    document.getElementById("scan-interval").textContent =
      `${status.auto_scan_interval_seconds / 60} minutes`;
    nextAutomaticScanAt = parseApiDate(status.next_auto_scan_at);
    updateScheduleDisplay();
  } catch (e) { console.warn(e); }
}

async function loadLatestSensor() {
  try {
    const r = await fetchJSON("/sensors/latest");
    document.getElementById("metric-temp").textContent = `${r.temperature_c.toFixed(1)}°C`;
    document.getElementById("metric-humidity").textContent = `${r.humidity_pct.toFixed(1)}%`;
    document.getElementById("correction-log").textContent = r.correction_note
      ? r.correction_note
      : "Both readings are within the target ranges.";

    const banner = document.getElementById("status-banner");
    const text = document.getElementById("status-text");
    banner.classList.remove("correcting", "alert", "limited");
    if (r.control_status === "stable") {
      text.textContent = `Climate in range · ${r.temperature_c.toFixed(1)}°C / ${r.humidity_pct.toFixed(1)}% RH`;
    } else if (r.control_status === "needs_hardware") {
      banner.classList.add("limited");
      text.textContent = `Outside target; required correction hardware is unavailable. ${r.correction_note}`;
    } else if (r.control_status === "partial") {
      banner.classList.add("limited", "correcting");
      text.textContent = `Some correction is active; additional hardware is required. ${r.correction_note}`;
    } else if (r.is_correcting) {
      banner.classList.add("correcting");
      text.textContent = `Outside target; automatic correction active. ${r.correction_note}`;
    }
    updateAlertBanner();
  } catch (e) { console.warn(e); }
}

async function loadAlerts() {
  try {
    const rows = await fetchJSON("/alerts/history?limit=5");
    const tbody = document.querySelector("#alerts-table tbody");
    tbody.innerHTML = "";
    rows.forEach(a => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${parseApiDate(a.sent_at).toLocaleString()}</td>
        <td>${a.phone_number}</td>
        <td>${a.bad_count}</td>
        <td>${a.message}</td>
        <td>${a.delivery_status}</td>`;
      tbody.appendChild(tr);
    });
    latestAlert = rows[0] || null;
    updateAlertBanner();
  } catch (e) { console.warn(e); }
}

async function loadHistoryChart() {
  try {
    const rows = (await fetchJSON("/sensors/history?hours=24")).slice(-10);
    const labels = rows.map((_, index) => index + 1);
    const temps = rows.map(r => r.temperature_c);
    const hums = rows.map(r => r.humidity_pct);

    if (!climateChart) {
      const ctx = document.getElementById("climate-chart").getContext("2d");
      climateChart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: "Temp (°C)", data: temps, borderColor: "#16834b", tension: 0.3, pointRadius: 0 },
            { label: "Humidity (%)", data: hums, borderColor: "#c18116", tension: 0.3, pointRadius: 0, yAxisID: "y1" },
          ],
        },
        options: {
          responsive: true,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { title: { display: true, text: "Latest readings" } },
            y: { title: { display: true, text: "°C" } },
            y1: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "%" } },
          },
          plugins: { legend: { labels: { color: "#31483a" } } },
        },
      });
    } else {
      climateChart.data.labels = labels;
      climateChart.data.datasets[0].data = temps;
      climateChart.data.datasets[1].data = hums;
      climateChart.update();
    }
  } catch (e) { console.warn(e); }
}

async function loadLatestScan() {
  const tbody = document.querySelector("#sample-table tbody");
  try {
    const scan = await fetchJSON("/scan/latest");

    document.getElementById("metric-condition").textContent =
      `${scan.counts.good}/${scan.counts.bad}`;

    tbody.innerHTML = "";
    scan.samples.forEach(s => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>B-${String(s.batch_id).padStart(4, "0")}</td>
        <td>S-${String(s.scan_id).padStart(4, "0")}</td>
        <td>#${s.cucumber_number}</td>
        <td class="cond-${s.condition}">${s.condition}</td>
        <td>${s.est_shelf_life_days == null ? "N/A" : s.est_shelf_life_days.toFixed(1)}</td>
        <td>${(s.yolo_confidence * 100).toFixed(1)}%</td>
        <td>${s.hsi_hue_mean == null ? "N/A" : s.hsi_hue_mean.toFixed(1)}</td>
        <td>${s.lbp_texture_score == null ? "N/A" : s.lbp_texture_score.toFixed(2)}</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) {
    if (e.message.endsWith("-> 404")) {
      document.getElementById("metric-condition").textContent = "0/0";
      tbody.innerHTML = '<tr><td colspan="8">No scans for this batch yet.</td></tr>';
    } else {
      console.warn(e);
    }
  }
}

async function loadScanHistory() {
  try {
    const rows = await fetchJSON("/scan/history?limit=5");
    const tbody = document.querySelector("#scan-history-table tbody");
    tbody.innerHTML = "";
    rows.forEach(r => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${parseApiDate(r.started_at).toLocaleString()}</td>
        <td>${r.trigger_type}</td>
        <td>B-${String(r.batch_id).padStart(4, "0")}</td>
        <td>S-${String(r.id).padStart(4, "0")}</td>
        <td>${r.detected_count}</td>
        <td class="cond-good">${r.good_count}</td>
        <td class="cond-bad">${r.bad_count}</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) { console.warn(e); }
}

async function refreshAll() {
  await Promise.all([loadLatestSensor(), loadHistoryChart(), loadLatestScan(), loadScanHistory(), loadAlerts()]);
}

document.getElementById("manual-scan-btn").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  const defaultLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    await fetchJSON("/scan/manual", { method: "POST" });
    await Promise.all([loadSystemStatus(), loadLatestScan(), loadScanHistory(), loadAlerts()]);
  } catch (err) {
    alert("Scan failed: " + err.message);
  } finally {
    button.disabled = false;
    button.textContent = defaultLabel;
  }
});

document.getElementById("new-batch-btn").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Creating…";
  try {
    await fetchJSON("/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await Promise.all([loadBatch(), loadLatestScan(), loadScanHistory()]);
  } catch (error) {
    alert("New batch failed: " + error.message);
  } finally {
    button.disabled = false;
    button.textContent = "New Batch";
  }
});

loadBatch();
loadSystemStatus();
refreshAll();
updateClock();
updateScheduleDisplay();
setInterval(updateClock, 1000);
setInterval(updateScheduleDisplay, 1000);
setInterval(loadSystemStatus, 5000);
setInterval(loadLatestSensor, 5000);
setInterval(loadHistoryChart, 15000);
setInterval(loadAlerts, 5000);
