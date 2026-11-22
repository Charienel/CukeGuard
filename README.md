# CukeGuard — Software Prototype

A FastAPI backend and dashboard configured for the CukeGuard Raspberry Pi
hardware. Hardware-specific dependencies and live I/O require a Raspberry Pi;
the Windows development environment cannot read the sensors or drive actuators.

## Run it

Follow [HARDWARE_INTEGRATION.md](./HARDWARE_INTEGRATION.md) for the Raspberry Pi
setup, wiring checks, calibration, and service startup sequence. The dashboard
is available at `http://<raspberry-pi-ip>:8000` after the service starts.

Automatic camera scans default to every 3 hours. A manual scan restarts
that countdown. A newly detected temperature or humidity excursion triggers
an immediate scan without changing the scheduled countdown. This is a fresh
camera scan, not a look-back through recorded video. To change the interval, set
`CUKEGUARD_AUTO_SCAN_MINUTES` before starting the server.

### Optional cucumber model

The selected candidate is `models/cucumber_condition.keras`. It expects one
RGB cucumber crop resized to 128×128 and returns two softmax class scores.
The verification set measured 87.14% accuracy, but the model labeled 35 of
206 Bad samples as Good. HSI/LBP supplies the final Good/Bad condition in
model mode; Keras confidence is retained as supporting information. These
verification images may overlap training data; validate on independent,
camera-captured samples before relying on alerts. To compare
models, install the optional runtime and
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
`good` or `bad` in the server environment. Without this setting, the webcam
scan still computes its Good/Bad result using HSI/LBP. In model mode, HSI/LBP determines the final
Good/Bad condition, including when its result differs from Keras. Existing
records previously marked for inspection are converted using their stored
HSI/LBP measurements (missing or failing measurements become Bad). The USB
webcam adapter provides RGB frames to `capture_frame()`; each fixed cucumber
position produces one sample. The classifier does not
produce shelf-life estimates or object bounding boxes, so those remain
unavailable without separate validated calculations.

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

## Hardware integration status

| Piece | Status |
|---|---|
| FastAPI app, routing, background loops | Real |
| SQLite DB + SQLAlchemy models (matches your ERD) | Real |
| Dashboard (live metric cards, 24h chart, scan table, manual scan button) | Real |
| SHT41 x3 / DS18B20 readings | Hardware adapter implemented; verify wiring and calibrate on the Pi |
| Peltier / heatsink control | Not configured; the wiring PDF does not map relay loads or give a Peltier control pin |
| Mist board control | GPIO21 0.5-second button press; validate behavior and pulse timing on the rig |
| NEMA17 + DRV8825 | GPIO17 STEP, GPIO27 DIR, GPIO22 ENABLE; calibrate and verify homing/travel before scans |
| Camera capture | USB/UVC `/dev/video0` adapter; verify framing and focus on the Pi |
| Condition classification | HSI/LBP from each captured frame; optional Keras model needs verified class mapping |
| SMS alert to owner | SIM800L AT-command integration on `/dev/serial0`; verify modem, SIM, signal, and SMS service |

The scan uses a USB/UVC webcam on the Pi and four fixed cucumber positions
spaced 63.5 mm apart. A separate browser camera preview is optional and is not
the camera used for scanning. Live climate readings require the Pi sensors and
are marked stale when no recent reading is received. The cooler cannot operate
until its Peltier/fan control wiring is identified; there is no heater or
dehumidifier installed.

The Owner Alerts section sends to the number configured in the local `.env`
file as `CUKEGUARD_OWNER_PHONE`. The `.env` file is ignored by Git and should
not be committed. This recipient applies to the active batch and future
batches. Climate notifications are sent when conditions move out of range and
when they return to range; repeated readings during the same excursion are
deduplicated. The configured SMS provider is a SIM800L modem on `/dev/serial0`;
enable the Raspberry Pi UART, install `pyserial`, insert an active SIM with SMS
service, and connect an antenna before expecting delivery. Power the modem
through its EVB 5VIN input using a supply that meets the board's current needs;
do not power it from a Pi GPIO pin. Verify the EVB UART logic levels are safe
for the Pi's 3.3 V UART.

### Hardware integration guide

See [HARDWARE_INTEGRATION.md](./HARDWARE_INTEGRATION.md) for the verified GPIO
assignments and step-by-step Raspberry Pi setup. Peltier/fan output remains
blocked until its wiring and relay/load mapping are confirmed.

## How the alert flow works

1. Every scan runs the vision pipeline, which labels each detected cucumber
   as `good` or `bad` based on its condition.
2. If any sample comes back `bad`, `notifier.notify_bad_condition()` fires
   an SMS to `StorageBatch.owner_phone` and logs it to the `alert_log` table.
3. The touchpad's **Owner Alerts** panel and the red status banner both read
   from `GET /api/alerts/history`.
4. Alerts are addressed to the fixed owner number in the local
   `CUKEGUARD_OWNER_PHONE` setting.

### Optional cloud SMS: iProg

The default provider is the SIM800L hardware modem on the Raspberry Pi. If
using iProg instead, set its token locally before starting Uvicorn:

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

### Optional cloud SMS: Semaphore

If using Semaphore instead of the default SIM800L, set its credentials before
starting Uvicorn:

```powershell
$env:CUKEGUARD_SMS_PROVIDER = "semaphore"
$env:SEMAPHORE_API_KEY = "your-semaphore-api-key"
$env:SEMAPHORE_SENDER_NAME = "CukeGuard" # optional; must be approved by Semaphore
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

Never commit the API key. Set the recipient in the local `.env` file. The
dashboard refreshes the alert list every five seconds; scans submit an SMS as
soon as a bad sample is detected. Scheduled scans run every 3 hours by default,
and a newly active temperature or humidity alert triggers an immediate scan.
An alert status of `submitted` means Semaphore accepted the request, not that
the carrier confirmed delivery.

If no SMS provider is configured, sending is reported as failed rather than
silently treated as delivered. For the SIM800L UART wiring and Raspberry Pi
setup, follow the hardware integration guide.

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
   `CUKEGUARD_AUTO_SCAN_MINUTES` (scheduled scan interval, default 180 min) — tune to
  your real hardware's timing before the defense demo.
