# CukeGuard — Software Prototype

A working FastAPI backend + browser dashboard for the CukeGuard system,
built to run **right now on a laptop** with simulated sensors/camera, and
structured so the real Raspberry Pi hardware drops in later with no
restructuring.

## Run it

```bash
python -m pip install -r requirements.txt
copy .env.example .env
# Set CUKEGUARD_OWNER_PHONE in .env to the owner's number in international format.
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open `http://localhost:8000` (or `http://<pi-ip>:8000` from another
device on the same network — this is what makes it work for both the local
touchscreen in kiosk mode and remote browser access).

Automatic camera scans default to every 15 minutes. A manual scan restarts
that countdown. To change the interval later, set
`CUKEGUARD_AUTO_SCAN_MINUTES` before starting the server.

### Optional cucumber model

The selected candidate is `models/cucumber_condition.keras`. It expects one
RGB cucumber crop resized to 128×128 and returns two softmax class scores.
The verification set measured 87.14% accuracy, but the model labeled 35 of
206 Bad samples as Good. The HSI/LBP rules missed fewer Bad samples but also
flagged many Good samples, so they are used as a second opinion rather than as
the primary classifier. These verification images may overlap training data;
keep alerts simulated until the cascade passes on an independent,
camera-captured test set. To compare models, install the optional runtime and
place labeled images in `good/` and `bad/`:

```powershell
python -m pip install -r requirements-model.txt
python verify_model_mapping.py --good-dir .\verification_samples\good --bad-dir .\verification_samples\bad
```

Compare the selected model against the old classifier on the same images with
`--model .\models\cucumber_condition.keras --compare-model .\models\cucumber_good_bad_classifier.keras`.
The verifier handles each model's own input dimensions and sigmoid/softmax
output, and reports overall accuracy plus Good and Bad recall.

After verifying the class mapping, set `CUKEGUARD_MODEL_POSITIVE_LABEL` to
`good` or `bad` in the server environment. Until then, the scan pipeline
remains simulated. In model mode, Keras makes the primary prediction. HSI/LBP
runs when confidence is below 0.8 or Keras predicts Good; matching results keep
the Keras label, while disagreement is stored as `needs_inspection` and is
not sent as a Bad-condition alert. The 0.8 threshold is provisional and must
be tuned using a separate validation set. When the real camera is connected,
`capture_frame()` must provide its RGB array as the frame's `image` value; each
frame produces one sample. The classifier does not produce shelf-life
estimates or object bounding boxes, so those remain unavailable without
separate validated calculations.

To export the complete database history to a filterable Excel workbook, run
`python export_database.py`. The timestamped workbook is written to `exports/`.
It is a snapshot; rerun the script to include newer records.

Use `--start-date` and `--end-date` for inclusive UTC dates or ISO timestamps,
repeat `--batch-id` to select batches, `--exclude-batch-id` to omit batches,
and `--exclude-alert-status` (`Normal`, `Warning`, or `Threshold Breach`) to
omit sensor readings by status. Add `--active-only` to omit ended batches.
Use `--output` to choose a workbook path. For example:

```powershell
python export_database.py --start-date 2026-10-01 --end-date 2026-10-02 --batch-id 15 --output .\exports\batch-15.xlsx
```

Sensor rows include their batch code and the latest scan ID in that batch at
or before the reading timestamp. The workbook Overview explains this
association and lists all applied filters. Every dated sheet starts with
Excel date values for `Month` and `Day`, followed by records in chronological
order; AutoFilters remain enabled to select specific days, months, batches,
and alert statuses. The Batches sheet also summarizes confidence, hue,
texture, condition counts, and estimated shelf life from each batch's latest
scan in the selected export scope. Cucumber Samples contains each scan-level
cucumber observation and its nearest prior temperature/humidity reading.
Cucumber Hourly Climate lists UTC-hour averages for every cucumber number in
each batch; those values are shared batch-sensor measurements, not readings
from sensors attached to individual cucumbers. Missing hours between recorded
readings appear with blank averages and a zero reading count.

## What's real vs. simulated right now

| Piece | Status |
|---|---|
| FastAPI app, routing, background loops | Real |
| SQLite DB + SQLAlchemy models (matches your ERD) | Real |
| Dashboard (live metric cards, 24h chart, scan table, manual scan button) | Real |
| SHT31-D / DS18B20 readings | **Simulated** in `iot_controller.read_temperature_humidity()` |
| Peltier / mister control | **Simulated** in `iot_controller.apply_peltier_pwm()` / `set_mister()` |
| NEMA17 stepper sweep | **Simulated** in `vision_pipeline.home_and_scan_positions()` |
| Camera capture | **Simulated** in `vision_pipeline.capture_frame()` |
| YOLO detection + HSI/LBP + good/bad condition classification | **Simulated** in `vision_pipeline.detect_and_classify()` |
| SMS alert to owner on bad detection | **Simulated** (printed to console + logged) in `notifier.send_sms()` |

The dashboard can show a local browser camera preview after camera permission is
granted. That preview is not uploaded or analyzed. The latest-scan location map
and bounding boxes come from the simulated vision pipeline until a validated
camera-connected object detector is installed. The six-cucumber cartoon shelf
map draws one cartoon per cucumber reported by the latest scan, scales the
shelf contents to fit without horizontal scrolling, boxes only bad samples,
and polls for new scans every five seconds. Automatic cooler and
mister control, climate alerts, and the daily adjustment log are also simulation-only
until real sensors and actuators are connected. Temperature below range and
humidity above range are reported, but this prototype has no heater or
dehumidifier to correct them.

The Owner Alerts section sends to the number configured in the local `.env`
file as `CUKEGUARD_OWNER_PHONE`. The `.env` file is ignored by Git and should
not be committed. This recipient applies to the active batch and future
batches. Climate notifications are sent when conditions move out of range and
when they return to range; repeated readings during the same excursion are
deduplicated. Configure an SMS provider before expecting delivery.

### Hardware integration roadmap

See [HARDWARE_INTEGRATION.md](./HARDWARE_INTEGRATION.md) for the planned
components, indexed-camera scan design, existing software hooks, safe bring-up
sequence, and outstanding details. The roadmap is documented here rather than
displayed in the dashboard. Identify the exact Raspberry Pi Stepper Motor
HAT/driver before implementing motor control; GPIO assignments and electrical
ratings are intentionally not guessed.

## How the alert flow works

1. Every scan runs the vision pipeline, which labels each detected cucumber
   as `good` or `bad` based on its condition.
2. If any sample comes back `bad`, `notifier.notify_bad_condition()` fires
   an SMS to `StorageBatch.owner_phone` and logs it to the `alert_log` table.
3. The touchpad's **Owner Alerts** panel and the red status banner both read
   from `GET /api/alerts/history`.
4. Alerts are addressed to the fixed owner number in the local
   `CUKEGUARD_OWNER_PHONE` setting.

### Enable test SMS with iProg

In PowerShell, set the iProg token in the same terminal used to start Uvicorn:

```powershell
$env:CUKEGUARD_SMS_PROVIDER = "iprog"
$env:IPROG_API_TOKEN = "your-iProg-api-token"
# Optional: choose 0, 1, or 2; omit to use iProg's default.
$env:IPROG_SMS_PROVIDER = "0"
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

Never commit or paste the token into chat. Set the recipient in the local
`.env` file. A successful iProg API
response is recorded as `submitted`: iProg has queued the message, but that
does not confirm carrier delivery. Verify account credits and test with a
number you control. The existing Semaphore setup is also supported.

### Enable real SMS with Semaphore

In PowerShell, set the provider credentials before starting Uvicorn:

```powershell
$env:CUKEGUARD_SMS_PROVIDER = "semaphore"
$env:SEMAPHORE_API_KEY = "your-semaphore-api-key"
$env:SEMAPHORE_SENDER_NAME = "CukeGuard" # optional; must be approved by Semaphore
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

Never commit the API key. Set the recipient in the local `.env` file. The
dashboard refreshes the alert list every five seconds; scans submit an SMS as
soon as a bad sample is detected. Scheduled scans still run every 15 minutes.
An alert status of `submitted` means Semaphore accepted the request, not that
the carrier confirmed delivery. Without these settings, status is `simulated`
and no text is sent.

The simulated functions document the current adapter boundaries. Real motor,
camera, sensor, and actuator integration still needs hardware-specific drivers,
configuration, validation, and fault handling as described in the integration
guide.

## Project layout

```
CukeGuard/
├── backend/
│   ├── app.py              # FastAPI app, routes, background loops
│   ├── database.py         # SQLAlchemy models (matches your ERD)
│   ├── iot_controller.py   # climate control loop (sim now, GPIO later)
│   └── vision_pipeline.py  # scan + classification (sim now, YOLO later)
├── frontend/
│   ├── templates/index.html
│   └── static/{style.css, app.js}
└── requirements.txt
```

## API quick reference

- `GET  /api/batch/active` — current storage batch
- `POST /api/batch` — start a new batch (ends the current one); the dashboard's **New Batch** button creates one without requiring data entry
- `PATCH /api/batch/active/owner-phone` — update the active batch's SMS recipient
- `GET  /api/sensors/latest` — most recent temp/humidity/actuator reading
- `GET  /api/sensors/history?hours=24` — climate trend data
- `GET  /api/automation/history` — today's automatic climate corrections (UTC)
- `POST /api/scan/manual` — trigger a scan pass immediately
- `GET  /api/scan/latest` — latest scan's samples, good/bad counts, and estimated shelf life
- `GET  /api/scan/history?limit=20` — recent scan sessions with detected/good/bad totals

Interactive API docs (auto-generated by FastAPI) are at `/docs`.

## Tuning

- `iot_controller.py`: control-loop tolerance around target temp is
  ±1.25°C — adjust to match your actual PID behavior.
- `app.py`: `IOT_POLL_SECONDS` (sensor poll rate, default 5s) and
   `CUKEGUARD_AUTO_SCAN_MINUTES` (scheduled scan interval, default 15 min) — tune to
  your real hardware's timing before the defense demo.
