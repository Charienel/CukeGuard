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

### Cucumber detection and condition models

`models/yolov8n_cucumber.pt` is a ready-made YOLOv8n detector with four
cucumber growth-stage classes. It finds cucumber boxes; each detected crop is
then passed to `models/cucumber_condition.keras` for Good/Bad classification.
The YOLOv8n baseline weights by Xu Liu and Wei Sun are from
[CAS-YOLO](https://github.com/Liu67-xu/CAS-YOLO) and are released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The file is renamed
from `YOLOv8n_baseline_best.pt` without changing the weights. Its SHA-256 is
`2818f190db83411d7d1e7cfdeaf749094df1490f95296464e876695f33911386`.
The detector was validated on other camera data, so test it with the installed
camera and lighting before relying on alerts.

The Keras candidate expects one RGB cucumber crop resized to 128×128 and
returns two softmax class scores. The bundled mapping was verified on 311
provided crops: `good` as the positive class scored 271/311 (87.1%), with
100/105 Good and 171/206 Bad correctly classified. The mapping is therefore
set to `good`; verify it again with independent, camera-captured samples
before relying on alerts. Install the optional runtime and place labeled images
in `good/` and `bad/`:

```powershell
python -m pip install -r requirements-model.txt
python verify_model_mapping.py --good-dir .\verification_samples\good --bad-dir .\verification_samples\bad
```

Compare the selected model against the old classifier on the same images with
`--model .\models\cucumber_condition.keras --compare-model .\models\cucumber_good_bad_classifier.keras`.
The verifier handles each model's own input dimensions and sigmoid/softmax
output, and reports overall accuracy plus Good and Bad recall.

Set `CUKEGUARD_MODEL_POSITIVE_LABEL` to the verified mapping (`good` for the
bundled model) in the server environment if overriding the default. Install
YOLO and Keras dependencies with `pip install -r requirements-model.txt`.
The scan samples frames every
5 mm from Home/MIN (the right endpoint) toward Far/MAX, keeps the strongest
detection in each of four 63.5 mm sections along the approximately 254 mm rail,
then returns to Home/MIN. The software travel bound is 254 mm; the Far/MAX
switch must be reached within that distance.
It reports an explicit error instead of fabricating Bad samples if it does not
find all four cucumbers. Keras classifies the detected cucumber crop; HSI/LBP
measurements are stored as supporting features.
If the Far/MAX switch is not reached, the scan stops and leaves the carriage
where it is; it does not command a return to Home/MIN.

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
| Peltier / heatsink | Always powered (user-confirmed); not monitored or switched by software |
| Mist board control | GPIO21 (physical pin 40), GND physical pin 39; toggle button receives one 0.5-second press when desired state changes |
| NEMA17 + DRV8825 | GPIO17 STEP, GPIO27 DIR, GPIO22 ENABLE; calibrate and verify homing/travel before scans |
| Camera capture | USB/UVC `/dev/video0` adapter; verify framing and focus on the Pi |
| Condition classification | YOLO detects cucumber crops; Keras classifies each crop as Good/Bad, with HSI/LBP stored as supporting features |
| SMS alert to owner | SIM800L AT-command integration on `/dev/serial0`; verify modem, SIM, signal, and SMS service |

The scan uses a USB/UVC webcam on the Pi and four cucumber sections spaced
63.5 mm apart along the approximately 254 mm carriage travel. The dashboard
shows the latest scan map, not a live camera preview; the USB webcam remains
active for real scans.
Live climate readings require the Pi sensors and
are marked stale when no recent reading is received. The Peltier is always
powered but has no feedback and cannot be switched off by software. If
temperature drops below 10°C, switch the cooler off at its power source. The
system has no heater or dehumidifier; do not leave it unattended unless an
independent thermostat or safety cutoff prevents overcooling.

Because the mist board provides no state feedback, inspect its actual state
before starting the controller and set `CUKEGUARD_MISTER_INITIAL_STATE=on` or
`off` in the ignored local `.env` to match. Leave it unset until verified;
repeat the check if the mist board is powered off or toggled independently.

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
assignments and step-by-step Raspberry Pi setup. Peltier/fan software control
remains blocked because no control output is assigned; the Peltier stays powered
independently of CukeGuard.

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
│   ├── iot_controller.py   # climate control loop
│   └── vision_pipeline.py  # GPIO sweep, YOLO detection and Keras classification
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
