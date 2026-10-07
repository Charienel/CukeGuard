"""
app.py — CukeGuard backend entry point.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Serves:
  - REST API under /api/*
  - The frontend dashboard (static files) at /
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import desc
from dotenv import load_dotenv

from .database import (
    init_db, get_db, SessionLocal,
    StorageBatch, SensorReading, ScanSession, CucumberSample, AlertLog,
)
from . import iot_controller, vision_pipeline, notifier

load_dotenv()

app = FastAPI(title="CukeGuard API")
logger = logging.getLogger(__name__)
OWNER_PHONE = os.getenv("CUKEGUARD_OWNER_PHONE", "").strip()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

IOT_POLL_SECONDS = 5
AUTO_SCAN_MINUTES = max(1, int(os.getenv("CUKEGUARD_AUTO_SCAN_MINUTES", "15")))
AUTO_SCAN_SECONDS = 60 * AUTO_SCAN_MINUTES
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
HARDWARE_GUIDE = Path(__file__).resolve().parent.parent / "HARDWARE_INTEGRATION.md"
_next_auto_scan_at: Optional[datetime] = None


def _schedule_next_auto_scan():
    global _next_auto_scan_at
    _next_auto_scan_at = datetime.now(timezone.utc) + timedelta(
        seconds=AUTO_SCAN_SECONDS
    )


def _climate_alert_details(reading, previous_conditions=()):
    temp_min, temp_max = iot_controller.TARGET_TEMP_RANGE
    humidity_min, humidity_max = iot_controller.TARGET_HUMIDITY_RANGE
    previous = set(previous_conditions)
    active = set()
    messages = []
    if reading.temperature_c > temp_max:
        active.add("temperature_high")
    elif "temperature_high" in previous and reading.temperature_c > temp_max - 0.3:
        active.add("temperature_high")
    if reading.temperature_c < temp_min:
        active.add("temperature_low")
    elif "temperature_low" in previous and reading.temperature_c < temp_min + 0.3:
        active.add("temperature_low")
    if reading.humidity_pct < humidity_min:
        active.add("humidity_low")
    elif "humidity_low" in previous and reading.humidity_pct < humidity_min + 1.0:
        active.add("humidity_low")
    if reading.humidity_pct > humidity_max:
        active.add("humidity_high")
    elif "humidity_high" in previous and reading.humidity_pct > humidity_max - 1.0:
        active.add("humidity_high")

    if "temperature_high" in active:
        state = "high" if reading.temperature_c > temp_max else "recovering"
        messages.append(
            f"Temperature is {state} at {reading.temperature_c:.1f}°C "
            f"(target {temp_min:.1f}–{temp_max:.1f}°C); "
            + (
                f"cooler activated at {reading.peltier_pwm_pct:.0f}%."
                if reading.peltier_pwm_pct > 0
                else "cooler is on standby."
            )
        )
    if "temperature_low" in active:
        state = "low" if reading.temperature_c < temp_min else "recovering"
        messages.append(
            f"Temperature is {state} at {reading.temperature_c:.1f}°C "
            f"(target {temp_min:.1f}–{temp_max:.1f}°C); "
            "heating hardware is not configured."
        )
    if "humidity_low" in active:
        state = "low" if reading.humidity_pct < humidity_min else "recovering"
        messages.append(
            f"Humidity is {state} at {reading.humidity_pct:.1f}% RH "
            f"(target {humidity_min:.1f}–{humidity_max:.1f}%); "
            + (
                "humidifier boosted (mister activated)."
                if reading.mister_active
                else "humidifier is on standby."
            )
        )
    if "humidity_high" in active:
        state = "high" if reading.humidity_pct > humidity_max else "recovering"
        messages.append(
            f"Humidity is {state} at {reading.humidity_pct:.1f}% RH "
            f"(target {humidity_min:.1f}–{humidity_max:.1f}%); "
            "dehumidification hardware is not configured."
        )
    order = ("temperature_high", "temperature_low", "humidity_low", "humidity_high")
    return tuple(condition for condition in order if condition in active), " ".join(messages)


# ---------- Pydantic response/request shapes ----------

class BatchCreate(BaseModel):
    batch_code: Optional[str] = None
    cucumber_variety: Optional[str] = None
    initial_quantity: Optional[int] = Field(default=None, ge=1)
    target_temp_c: float = 11.0
    target_humidity_pct: float = 95.0
    owner_phone: str = OWNER_PHONE
    notes: Optional[str] = None

    @field_validator("owner_phone")
    @classmethod
    def _clean_owner_phone(cls, value: str) -> str:
        normalized = normalize_owner_phone(value)
        if normalized != OWNER_PHONE:
            raise ValueError("The owner phone number is permanently configured")
        return normalized


def normalize_owner_phone(value: str) -> str:
    compact = re.sub(r"[\s().-]", "", value.strip())
    if re.fullmatch(r"09\d{9}", compact):
        compact = "+63" + compact[1:]
    elif re.fullmatch(r"639\d{9}", compact):
        compact = "+" + compact
    if not re.fullmatch(r"\+?\d{7,15}", compact):
        raise ValueError("Enter a phone number with 7–15 digits, optionally starting with +")
    return compact


class OwnerPhoneUpdate(BaseModel):
    owner_phone: str

    @field_validator("owner_phone")
    @classmethod
    def _clean_owner_phone(cls, value: str) -> str:
        normalized = normalize_owner_phone(value)
        if normalized != OWNER_PHONE:
            raise ValueError("The owner phone number is permanently configured")
        return normalized


class SensorReadingOut(BaseModel):
    timestamp: datetime
    temperature_c: float
    humidity_pct: float
    peltier_pwm_pct: float
    mister_active: int
    correction_note: Optional[str] = None

    class Config:
        from_attributes = True


# ---------- Startup: init DB + ensure an active batch + background loops ----------

@app.on_event("startup")
async def startup():
    if not OWNER_PHONE:
        raise RuntimeError("Set CUKEGUARD_OWNER_PHONE in the local .env file before starting CukeGuard")
    init_db()
    db = SessionLocal()
    active = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if not active:
        active = StorageBatch(
            owner_phone=OWNER_PHONE,
        )
        db.add(active)
        db.flush()
        active.batch_code = f"BATCH-{active.id:04d}"
    elif active.owner_phone != OWNER_PHONE:
        active.owner_phone = OWNER_PHONE
    db.commit()
    db.close()

    _schedule_next_auto_scan()
    asyncio.create_task(iot_loop())
    asyncio.create_task(scheduled_scan_loop())


async def iot_loop():
    while True:
        db = SessionLocal()
        try:
            batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
            if batch:
                reading = iot_controller.control_tick(batch, db)
                previous_conditions = notifier.active_climate_conditions(batch.id)
                conditions, message = _climate_alert_details(
                    reading, previous_conditions
                )
                notifier.notify_climate_change(
                    db, batch, reading, conditions, message
                )
        except Exception:
            logger.exception("Climate monitoring iteration failed")
        finally:
            db.close()
        await asyncio.sleep(IOT_POLL_SECONDS)


async def scheduled_scan_loop():
    while True:
        if _next_auto_scan_at is None:
            _schedule_next_auto_scan()
        seconds_until_scan = (
            _next_auto_scan_at - datetime.now(timezone.utc)
        ).total_seconds()
        if seconds_until_scan > 0:
            await asyncio.sleep(min(seconds_until_scan, 1))
            continue

        db = SessionLocal()
        try:
            batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
            if batch:
                _execute_scan(batch.id, db, trigger_type="scheduled")
        except Exception:
            logger.exception("Scheduled scan failed; the next scan remains scheduled")
        finally:
            db.close()
            _schedule_next_auto_scan()


def _execute_scan(batch_id: int, db: Session, trigger_type: str) -> ScanSession:
    session_row = ScanSession(batch_id=batch_id, trigger_type=trigger_type)
    db.add(session_row)
    db.commit()
    db.refresh(session_row)

    batch = db.query(StorageBatch).filter(StorageBatch.id == batch_id).first()
    expected_count = batch.initial_quantity if batch else None
    samples, counts = vision_pipeline.run_scan(batch_id, expected_count)
    for s in samples:
        db.add(CucumberSample(batch_id=batch_id, scan_id=session_row.id, **s))

    db.commit()
    db.refresh(session_row)

    bad_cucumber_numbers = [
        sample["cucumber_number"]
        for sample in samples
        if sample["condition"] == "bad"
    ]
    if bad_cucumber_numbers:
        notifier.notify_bad_condition(
            db, batch, session_row.id, bad_cucumber_numbers
        )

    return session_row


# ---------- Batch endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/system/status")
def system_status():
    return {
        "mode": iot_controller.DATA_MODE,
        "auto_scan_interval_seconds": AUTO_SCAN_SECONDS,
        "next_auto_scan_at": _next_auto_scan_at,
        "components": {
            "sensors": "simulated",
            "camera": "simulated",
            "stepper": "simulated",
            "limit_switches": "not_configured",
            "actuators": "simulated",
            "sms": notifier.sms_mode(),
        },
        "hardware_integration": [
            {
                "name": "Pi Camera + MobileNetV2",
                "detail": "Capture one indexed cucumber crop and classify good/bad with the bundled Keras model.",
                "status": "simulated",
            },
            {
                "name": "NEMA17 + Raspberry Pi Stepper HAT",
                "detail": "Home and move one calibrated position at a time. Confirm exact HAT driver/API and motor current.",
                "status": "simulated",
            },
            {
                "name": "Two limit switches",
                "detail": "Establish home and travel bounds after verifying wiring and switch polarity.",
                "status": "not_configured",
            },
            {
                "name": "SHT31-D + DS18B20",
                "detail": "Read humidity/air temperature over I2C and probe temperature over 1-Wire.",
                "status": "simulated",
            },
            {
                "name": "TCA9548A multiplexer",
                "detail": "Use only if needed; configure the verified bus address and channel.",
                "status": "not_configured",
            },
            {
                "name": "TEC + heatsink fans",
                "detail": "Verify thermal assembly, rated supply, and MOSFET/load-driver ratings.",
                "status": "simulated",
            },
            {
                "name": "24V mist maker + relay",
                "detail": "Verify isolation and moisture-safe placement away from exposed electronics.",
                "status": "simulated",
            },
            {
                "name": "Power + waterproof lighting",
                "detail": "Verify load voltage/current, power-supply sizing, fusing, and cable routing.",
                "status": "needs_verification",
            },
        ],
        "climate_ranges": {
            "temperature_c": {
                "min": iot_controller.TARGET_TEMP_RANGE[0],
                "max": iot_controller.TARGET_TEMP_RANGE[1],
            },
            "humidity_pct": {
                "min": iot_controller.TARGET_HUMIDITY_RANGE[0],
                "max": iot_controller.TARGET_HUMIDITY_RANGE[1],
            },
        },
        "control_capabilities": {
            "cooling": True,
            "heating": False,
            "misting": True,
            "dehumidification": False,
        },
        "automation": {
            "cooler": "simulated automatic control",
            "humidifier": "simulated automatic control",
        },
    }


@app.get("/api/batch/active")
def get_active_batch(db: Session = Depends(get_db)):
    batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if not batch:
        raise HTTPException(404, "No active batch")
    return {
        "id": batch.id,
        "batch_code": batch.batch_code,
        "started_at": batch.started_at,
        "target_temp_c": batch.target_temp_c,
        "target_humidity_pct": batch.target_humidity_pct,
        "cucumber_variety": batch.cucumber_variety,
        "initial_quantity": batch.initial_quantity,
        "owner_phone": batch.owner_phone,
        "notes": batch.notes,
    }


@app.post("/api/batch")
def create_batch(payload: BatchCreate, db: Session = Depends(get_db)):
    current = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if current:
        current.ended_at = datetime.utcnow()
    batch = StorageBatch(
        batch_code=payload.batch_code,
        cucumber_variety=payload.cucumber_variety,
        initial_quantity=payload.initial_quantity,
        target_temp_c=payload.target_temp_c,
        target_humidity_pct=payload.target_humidity_pct,
        owner_phone=OWNER_PHONE,
        notes=payload.notes,
    )
    db.add(batch)
    db.flush()
    if not batch.batch_code:
        batch.batch_code = f"BATCH-{batch.id:04d}"
    db.commit()
    db.refresh(batch)
    return {
        "id": batch.id,
        "batch_code": batch.batch_code,
        "initial_quantity": batch.initial_quantity,
    }


@app.patch("/api/batch/active/owner-phone")
def update_active_owner_phone(payload: OwnerPhoneUpdate, db: Session = Depends(get_db)):
    batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if not batch:
        raise HTTPException(404, "No active batch")
    batch.owner_phone = payload.owner_phone
    db.commit()
    return {"owner_phone": batch.owner_phone}


# ---------- Sensor endpoints ----------

@app.get("/api/sensors/latest")
def latest_reading(db: Session = Depends(get_db)):
    reading = db.query(SensorReading).order_by(desc(SensorReading.timestamp)).first()
    if not reading:
        raise HTTPException(404, "No sensor readings yet")
    temp_min, temp_max = iot_controller.TARGET_TEMP_RANGE
    humidity_min, humidity_max = iot_controller.TARGET_HUMIDITY_RANGE
    outside_range = (
        reading.temperature_c < temp_min
        or reading.temperature_c > temp_max
        or reading.humidity_pct < humidity_min
        or reading.humidity_pct > humidity_max
    )
    is_correcting = (
        (reading.temperature_c > temp_max and reading.peltier_pwm_pct > 0)
        or (reading.humidity_pct < humidity_min and bool(reading.mister_active))
    )
    requires_hardware = (
        reading.temperature_c < temp_min or reading.humidity_pct > humidity_max
    )
    if not outside_range:
        control_status = "stable"
    elif requires_hardware and is_correcting:
        control_status = "partial"
    elif requires_hardware:
        control_status = "needs_hardware"
    else:
        control_status = "correcting"

    return {
        "timestamp": reading.timestamp,
        "temperature_c": reading.temperature_c,
        "humidity_pct": reading.humidity_pct,
        "peltier_pwm_pct": reading.peltier_pwm_pct,
        "mister_active": bool(reading.mister_active),
        "correction_note": reading.correction_note,
        "is_correcting": is_correcting,
        "requires_hardware": requires_hardware,
        "control_status": control_status,
        "cooler_active": reading.peltier_pwm_pct > 0,
        "humidifier_active": bool(reading.mister_active),
    }


@app.get("/api/sensors/history")
def sensor_history(hours: int = 24, db: Session = Depends(get_db)):
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (
        db.query(SensorReading)
        .filter(SensorReading.timestamp >= since)
        .order_by(SensorReading.timestamp)
        .all()
    )
    return [
        {
            "timestamp": r.timestamp.isoformat(),
            "temperature_c": r.temperature_c,
            "humidity_pct": r.humidity_pct,
            "peltier_pwm_pct": r.peltier_pwm_pct,
            "mister_active": r.mister_active,
        }
        for r in rows
    ]


@app.get("/api/automation/history")
def automation_history(limit: int = 20, db: Session = Depends(get_db)):
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(SensorReading)
        .filter(
            SensorReading.timestamp >= today,
            SensorReading.correction_note.isnot(None),
        )
        .order_by(desc(SensorReading.timestamp))
        .limit(min(max(limit, 1), 100))
        .all()
    )
    count = (
        db.query(SensorReading)
        .filter(
            SensorReading.timestamp >= today,
            SensorReading.correction_note.isnot(None),
        )
        .count()
    )
    return {
        "date": today.date().isoformat(),
        "count": count,
        "events": [
            {
                "timestamp": row.timestamp.isoformat(),
                "temperature_c": row.temperature_c,
                "humidity_pct": row.humidity_pct,
                "peltier_pwm_pct": row.peltier_pwm_pct,
                "mister_active": bool(row.mister_active),
                "note": row.correction_note,
            }
            for row in rows
        ],
    }


# ---------- Scan / vision endpoints ----------

@app.post("/api/scan/manual")
def trigger_manual_scan(db: Session = Depends(get_db)):
    batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if not batch:
        raise HTTPException(404, "No active batch")
    session_row = _execute_scan(batch.id, db, trigger_type="manual")
    _schedule_next_auto_scan()
    return {"scan_session_id": session_row.id}


@app.get("/api/scan/latest")
def latest_scan(db: Session = Depends(get_db)):
    active_batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    scan_query = db.query(ScanSession)
    if active_batch:
        scan_query = scan_query.filter(ScanSession.batch_id == active_batch.id)
    session_row = scan_query.order_by(desc(ScanSession.started_at)).first()
    if not session_row:
        raise HTTPException(404, "No scans yet")
    samples = (
        db.query(CucumberSample)
        .filter(CucumberSample.scan_id == session_row.id)
        .order_by(CucumberSample.cucumber_number)
        .all()
    )
    counts = {"good": 0, "bad": 0, "needs_inspection": 0}
    for sample in samples:
        if sample.condition in counts:
            counts[sample.condition] += 1

    return {
        "scan_session_id": session_row.id,
        "batch_id": session_row.batch_id,
        "started_at": session_row.started_at,
        "trigger_type": session_row.trigger_type,
        "counts": counts,
        "samples": [
            {
                "sample_id": s.id,
                "batch_id": s.batch_id,
                "scan_id": s.scan_id,
                "cucumber_number": s.cucumber_number,
                "track_position_mm": s.track_position_mm,
                "bbox": [s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h],
                "yolo_confidence": s.yolo_confidence,
                "hsi_hue_mean": s.hsi_hue_mean,
                "lbp_texture_score": s.lbp_texture_score,
                "condition": s.condition,
                "est_shelf_life_days": s.est_shelf_life_days,
            }
            for s in samples
        ],
    }


@app.get("/api/scan/history")
def scan_history(limit: int = 20, db: Session = Depends(get_db)):
    rows = (
        db.query(ScanSession)
        .order_by(desc(ScanSession.started_at))
        .limit(limit)
        .all()
    )
    sample_counts = {
        row.id: {"good": 0, "bad": 0, "needs_inspection": 0}
        for row in rows
    }
    if sample_counts:
        samples = (
            db.query(CucumberSample.scan_id, CucumberSample.condition)
            .filter(CucumberSample.scan_id.in_(sample_counts))
            .all()
        )
        for scan_id, condition in samples:
            if condition in sample_counts[scan_id]:
                sample_counts[scan_id][condition] += 1

    return [
        {
            "id": r.id,
            "batch_id": r.batch_id,
            "batch_code": r.batch.batch_code,
            "started_at": r.started_at.isoformat(),
            "trigger_type": r.trigger_type,
            "detected_count": sum(sample_counts[r.id].values()),
            "good_count": sample_counts[r.id]["good"],
            "bad_count": sample_counts[r.id]["bad"],
            "needs_inspection_count": sample_counts[r.id]["needs_inspection"],
        }
        for r in rows
    ]


# ---------- Alert endpoints ----------

@app.get("/api/alerts/history")
def alert_history(limit: int = 20, db: Session = Depends(get_db)):
    rows = db.query(AlertLog).order_by(desc(AlertLog.sent_at)).limit(limit).all()
    return [
        {
            "id": r.id,
            "sent_at": r.sent_at.isoformat(),
            "phone_number": r.phone_number,
            "message": r.message,
            "bad_count": r.bad_count,
            "delivery_status": r.delivery_status,
            "alert_type": r.alert_type,
        }
        for r in rows
    ]


# ---------- Serve frontend ----------

app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.get("/hardware-integration")
def hardware_integration_guide():
    return FileResponse(HARDWARE_GUIDE, media_type="text/markdown")


@app.get("/")
def serve_dashboard():
    return FileResponse(FRONTEND_DIR / "templates" / "index.html")
