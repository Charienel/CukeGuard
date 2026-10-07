const API = "/api";
let climateChart;
let latestAlert = null;
let nextAutomaticScanAt = null;
let cameraStream = null;
let latestRenderedScanId = null;

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
  const alertLabel = latestAlert.alert_type === "climate"
    ? "Climate alert"
    : "Bad cucumber detected";
  text.textContent = `${alertLabel} — ${delivery} to ${latestAlert.phone_number}`;
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
    updateActuatorCard(
      "cooler-state",
      r.cooler_active,
      r.cooler_active ? `ACTIVE · ${r.peltier_pwm_pct.toFixed(0)}%` : "Standby",
    );
    updateActuatorCard(
      "humidifier-state",
      r.humidifier_active,
      r.humidifier_active ? "BOOSTED · MIST ON" : "Standby",
    );

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

function updateActuatorCard(id, active, label) {
  const element = document.getElementById(id);
  element.textContent = label;
  element.classList.toggle("active", active);
  element.classList.toggle("warning", false);
}

async function loadAlerts() {
  try {
    const rows = await fetchJSON("/alerts/history?limit=5");
    const tbody = document.querySelector("#alerts-table tbody");
    tbody.innerHTML = "";
    rows.forEach(a => {
      const tr = document.createElement("tr");
      [
        parseApiDate(a.sent_at).toLocaleString(),
        a.alert_type === "climate" ? "Climate" : "Condition",
        a.phone_number,
        a.message,
        a.delivery_status,
      ].forEach(value => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    latestAlert = rows[0] || null;
    updateAlertBanner();
  } catch (e) { console.warn(e); }
}

async function loadAutomationHistory() {
  try {
    const history = await fetchJSON("/automation/history?limit=12");
    document.getElementById("adjustment-count").textContent = history.count;
    const tbody = document.querySelector("#automation-table tbody");
    tbody.replaceChildren();
    if (!history.events.length) {
      const row = document.createElement("tr");
      row.innerHTML = '<td colspan="5">No automatic corrections recorded today.</td>';
      tbody.appendChild(row);
      return;
    }
    history.events.forEach(event => {
      const tr = document.createElement("tr");
      const values = [
        parseApiDate(event.timestamp).toLocaleTimeString(),
        `${event.temperature_c.toFixed(1)}°C`,
        `${event.humidity_pct.toFixed(1)}% RH`,
        event.note,
        "Simulation",
      ];
      values.forEach(value => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  } catch (error) {
    console.warn(error);
  }
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
    if (scan.scan_session_id === latestRenderedScanId) return;
    renderScanMap(scan);
    latestRenderedScanId = scan.scan_session_id;

    document.getElementById("metric-condition").textContent =
      `${scan.counts.good}/${scan.counts.bad}/${scan.counts.needs_inspection || 0}`;

    tbody.innerHTML = "";
    scan.samples.forEach(s => {
      const tr = document.createElement("tr");
      const conditionLabel = s.condition === "needs_inspection" ? "Needs inspection" : s.condition;
      tr.innerHTML = `
        <td>B-${String(s.batch_id).padStart(4, "0")}</td>
        <td>S-${String(s.scan_id).padStart(4, "0")}</td>
        <td>#${s.cucumber_number}</td>
        <td class="cond-${s.condition}">${conditionLabel}</td>
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
      document.getElementById("scan-map-meta").textContent = "No completed scans";
      document.querySelector("#bad-location-list").innerHTML = "<p>No scan locations yet.</p>";
      latestRenderedScanId = null;
      renderCucumberShelf([]);
    } else {
      console.warn(e);
    }
  }
}

function renderScanMap(scan) {
  const locationList = document.getElementById("bad-location-list");
  locationList.replaceChildren();
  const scanTime = parseApiDate(scan.started_at);
  document.getElementById("scan-map-meta").textContent =
    `${scan.samples.length} detected · Scan S-${String(scan.scan_session_id).padStart(4, "0")} · ${scanTime.toLocaleTimeString()}`;
  const badSamples = scan.samples.filter(sample => sample.condition === "bad");
  const reviewSamples = scan.samples.filter(sample => sample.condition === "needs_inspection");
  if (!badSamples.length && !reviewSamples.length) {
    const item = document.createElement("p");
    item.textContent = scan.samples.length
      ? "No bad cucumbers in the latest scan."
      : "No cucumbers detected in the latest scan.";
    locationList.appendChild(item);
  } else if (!badSamples.length) {
    const item = document.createElement("p");
    item.textContent = `${reviewSamples.length} cucumber(s) need inspection; no confirmed bad cucumbers.`;
    locationList.appendChild(item);
  }

  renderCucumberShelf(scan.samples, locationList);
}

function renderCucumberShelf(samples, locationList) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("scan-map-svg");
  const layer = document.getElementById("scan-map-boxes");
  const locations = locationList || document.getElementById("bad-location-list");
  const mapWidth = 800;
  const shelfPadding = 24;
  const shelfWidth = mapWidth - shelfPadding * 2;
  const slotWidth = samples.length ? shelfWidth / samples.length : shelfWidth;
  const cucumberScale = Math.min(1, slotWidth / 112);
  svg.setAttribute("viewBox", `0 0 ${mapWidth} 270`);
  svg.style.width = "100%";
  svg.setAttribute(
    "aria-label",
    `Latest scan shelf map showing ${samples.length} cucumber${samples.length === 1 ? "" : "s"}`,
  );
  layer.replaceChildren();

  const background = document.createElementNS(ns, "rect");
  background.setAttribute("class", "map-background");
  background.setAttribute("x", "0");
  background.setAttribute("y", "0");
  background.setAttribute("width", String(mapWidth));
  background.setAttribute("height", "270");
  background.setAttribute("rx", "12");
  layer.appendChild(background);

  const grid = document.createElementNS(ns, "path");
  grid.setAttribute("class", "map-grid");
  grid.setAttribute(
    "d",
    [
      ...[54, 108, 162, 216].map(y => `M0 ${y}H${mapWidth}`),
      ...Array.from({ length: Math.ceil(mapWidth / 100) + 1 }, (_, index) => {
        const x = index * 100;
        return `M${x} 0V270`;
      }),
    ].join(" "),
  );
  layer.appendChild(grid);

  const shelfTop = document.createElementNS(ns, "path");
  shelfTop.setAttribute("class", "shelf-top");
  shelfTop.setAttribute("d", `M22 47H${mapWidth - 22}`);
  layer.appendChild(shelfTop);
  const shelfRail = document.createElementNS(ns, "path");
  shelfRail.setAttribute("class", "shelf-rail");
  shelfRail.setAttribute("d", `M22 216H${mapWidth - 22}`);
  layer.appendChild(shelfRail);
  const shelfLegs = document.createElementNS(ns, "path");
  shelfLegs.setAttribute("class", "shelf-leg");
  shelfLegs.setAttribute("d", `M35 216V245 M${mapWidth - 35} 216V245`);
  layer.appendChild(shelfLegs);

  if (samples.length === 0) {
    const emptyLabel = document.createElementNS(ns, "text");
    emptyLabel.setAttribute("class", "map-empty-label");
    emptyLabel.setAttribute("x", String(mapWidth / 2));
    emptyLabel.setAttribute("y", "150");
    emptyLabel.textContent = "No cucumbers detected in the latest scan";
    layer.appendChild(emptyLabel);
  }

  samples.forEach((sample, index) => {
    const number = Number(sample.cucumber_number);
    const isBad = sample?.condition === "bad";
    const needsInspection = sample?.condition === "needs_inspection";
    const centerX = shelfPadding + slotWidth * (index + 0.5);
    const cucumber = document.createElementNS(ns, "g");
    cucumber.setAttribute("id", `shelf-cucumber-${sample.sample_id}`);
    cucumber.setAttribute(
      "class",
      `cartoon-cucumber${isBad ? " cucumber-bad" : needsInspection ? " cucumber-review" : ""}`,
    );
    cucumber.setAttribute("transform", `translate(${centerX} 0)`);
    cucumber.setAttribute("aria-label", `Cucumber ${number}: ${sample.condition}`);
    const scaledSlotWidth = Math.min(106, slotWidth - 4);
    cucumber.innerHTML = `
      <rect class="shelf-slot" x="${-scaledSlotWidth / 2}" y="55" width="${scaledSlotWidth}" height="149" rx="18"></rect>
      <g class="cucumber-character" transform="translate(0 135) scale(${cucumberScale}) translate(0 -135)">
        <path class="cucumber-body" d="M-21 82 C-32 91 -35 114 -30 139 L-24 173 C-22 185 -14 192 0 193 C14 192 22 185 24 173 L30 139 C35 114 32 91 21 82 C11 73 -11 73 -21 82Z"></path>
        <path class="cucumber-highlight" d="M-16 91 C-23 104 -22 121 -18 139 L-15 156 C-13 164 -9 168 -5 166 C-1 164 -3 157 -5 148 L-8 119 C-9 107 -8 96 -3 88 C-7 84 -12 86 -16 91Z"></path>
        <ellipse class="cucumber-spot" cx="16" cy="111" rx="3" ry="5"></ellipse>
        <ellipse class="cucumber-spot" cx="18" cy="153" rx="2.5" ry="4"></ellipse>
        <ellipse class="cucumber-spot" cx="-18" cy="164" rx="2.5" ry="4"></ellipse>
        <path class="cucumber-stem" d="M-5 80 Q-10 70 -7 64 Q1 69 1 79 Q8 69 14 69 Q13 78 5 84Z"></path>
        <ellipse class="cucumber-cheek" cx="-12" cy="127" rx="5" ry="3"></ellipse>
        <ellipse class="cucumber-cheek" cx="12" cy="127" rx="5" ry="3"></ellipse>
        <circle class="cucumber-eye" cx="-8" cy="116" r="3.2"></circle>
        <circle class="cucumber-eye" cx="8" cy="116" r="3.2"></circle>
        <path class="cucumber-smile" d="M-6 128 Q0 134 6 128"></path>
      </g>
      <text class="cucumber-number" x="0" y="235" font-size="${Math.max(5, Math.min(13, slotWidth * 0.18))}">#${number}</text>`;
    layer.appendChild(cucumber);

    if (isBad || needsInspection) {
      const frame = document.createElementNS(ns, "rect");
      frame.setAttribute("id", `scan-box-${sample.sample_id}`);
      frame.setAttribute("x", centerX - scaledSlotWidth / 2 - 3);
      frame.setAttribute("y", 52);
      frame.setAttribute("width", scaledSlotWidth + 6);
      frame.setAttribute("height", 156);
      frame.setAttribute("rx", 20);
      frame.setAttribute("class", `detection-box ${isBad ? "bad" : "review"}`);
      layer.appendChild(frame);

      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", centerX);
      label.setAttribute("y", 35);
      label.setAttribute("class", `detection-label ${isBad ? "bad" : "review"}`);
      label.setAttribute("font-size", String(Math.max(5, Math.min(14, slotWidth * 0.16))));
      label.textContent = `#${number} ${isBad ? "BAD" : "REVIEW"}`;
      layer.appendChild(label);

      const item = document.createElement("button");
      item.type = "button";
      item.className = `bad-location-item${needsInspection ? " review-location-item" : ""}`;
      item.textContent = needsInspection
        ? `Inspect cucumber #${number} · model and HSI/LBP disagree`
        : `Remove cucumber #${number} · ${(sample.yolo_confidence * 100).toFixed(0)}% confidence`;
      item.addEventListener("click", () => {
        document.querySelectorAll(".detection-box").forEach(node => node.classList.remove("highlighted"));
        frame.classList.add("highlighted");
      });
      locations.appendChild(item);
    }
  });
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
        <td class="cond-bad">${r.bad_count}</td>
        <td class="cond-needs_inspection">${r.needs_inspection_count || 0}</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) { console.warn(e); }
}

async function refreshAll() {
  await Promise.all([loadLatestSensor(), loadHistoryChart(), loadLatestScan(), loadScanHistory(), loadAlerts(), loadAutomationHistory()]);
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
setInterval(loadAutomationHistory, 15000);
setInterval(loadLatestScan, 5000);

document.getElementById("start-camera-btn").addEventListener("click", async () => {
  const status = document.getElementById("camera-status");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    status.textContent = "Camera access is unavailable; use localhost or HTTPS.";
    return;
  }
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    const video = document.getElementById("camera-preview");
    video.srcObject = cameraStream;
    document.getElementById("camera-placeholder").hidden = true;
    document.getElementById("start-camera-btn").disabled = true;
    document.getElementById("stop-camera-btn").disabled = false;
    status.textContent = "Live local preview · not analyzed";
  } catch (error) {
    status.textContent = `Camera unavailable: ${error.message}`;
  }
});

document.getElementById("stop-camera-btn").addEventListener("click", () => {
  if (cameraStream) {
    cameraStream.getTracks().forEach(track => track.stop());
    cameraStream = null;
  }
  document.getElementById("camera-preview").srcObject = null;
  document.getElementById("camera-placeholder").hidden = false;
  document.getElementById("start-camera-btn").disabled = false;
  document.getElementById("stop-camera-btn").disabled = true;
  document.getElementById("camera-status").textContent = "Camera not started";
});
