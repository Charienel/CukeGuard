# CukeGuard — Software Prototype

A working FastAPI backend + browser dashboard for the CukeGuard system,
built to run **right now on a laptop** with simulated sensors/camera, and
structured so the real Raspberry Pi hardware drops in later with no
restructuring.

## Run it

```bash
python -m pip install -r requirements.txt
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open `http://localhost:8000` (or `http://<pi-ip>:8000` from another
device on the same network — this is what makes it work for both the local
touchscreen in kiosk mode and remote browser access).

To export the complete database history to a filterable Excel workbook, run
`python export_database.py`. The timestamped workbook is written to `exports/`.
It is a snapshot; rerun the script to include newer records.

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

## How the alert flow works

1. Every scan runs the vision pipeline, which labels each detected cucumber
   as `good` or `bad` based on its condition.
2. If any sample comes back `bad`, `notifier.notify_bad_condition()` fires
   an SMS to `StorageBatch.owner_phone` and logs it to the `alert_log` table.
3. The touchpad's **Owner Alerts** panel and the red status banner both read
   from `GET /api/alerts/history`.
4. Set the real owner number with `POST /api/batch` (`owner_phone` field) —
   a placeholder (`+639000000000`) is used until you do.

### Enable test SMS with iProg

In PowerShell, set the iProg token in the same terminal used to start Uvicorn:

```powershell
$env:CUKEGUARD_SMS_PROVIDER = "iprog"
$env:IPROG_API_TOKEN = "your-iProg-api-token"
# Optional: choose 0, 1, or 2; omit to use iProg's default.
$env:IPROG_SMS_PROVIDER = "0"
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

Never commit or paste the token into chat. Set the active batch's `owner_phone`
to your real recipient number in international format. A successful iProg API
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

Never commit the API key. Set a real recipient number when creating the active
batch with `POST /api/batch` (`owner_phone`, in international format). The
dashboard refreshes the alert list every five seconds; scans submit an SMS as
soon as a bad sample is detected. Scheduled scans still run every 15 minutes.
An alert status of `submitted` means Semaphore accepted the request, not that
the carrier confirmed delivery. Without these settings, status is `simulated`
and no text is sent.

Every simulated function has a docstring saying exactly what real call to
swap in (`RPi.GPIO`/`gpiozero`, `adafruit_sht31d`, `picamera2`,
`ultralytics` YOLO, your OpenCV HSI/LBP code). Nothing else in the app —
the DB schema, the API contract, the dashboard — needs to change when you
do that swap, so you can develop/demo the software today and wire up the
rig separately.

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
- `GET  /api/sensors/latest` — most recent temp/humidity/actuator reading
- `GET  /api/sensors/history?hours=24` — climate trend data
- `POST /api/scan/manual` — trigger a scan pass immediately
- `GET  /api/scan/latest` — latest scan's samples, good/bad counts, and estimated shelf life
- `GET  /api/scan/history?limit=20` — recent scan sessions with detected/good/bad totals

Interactive API docs (auto-generated by FastAPI) are at `/docs`.

## Tuning

- `iot_controller.py`: control-loop tolerance around target temp is
  ±1.25°C — adjust to match your actual PID behavior.
- `app.py`: `IOT_POLL_SECONDS` (sensor poll rate, default 5s) and
  `AUTO_SCAN_SECONDS` (scheduled scan interval, default 15 min) — tune to
  your real hardware's timing before the defense demo.
