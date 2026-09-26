"""
app.py — CukeGuard backend entry point.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Serves:
  - REST API under /api/*
  - The frontend dashboard (static files) at /
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc

from .database import (
    init_db, get_db, SessionLocal,
    StorageBatch, SensorReading, ScanSession, CucumberSample, AlertLog,
)
from . import iot_controller, vision_pipeline, notifier

app = FastAPI(title="CukeGuard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

IOT_POLL_SECONDS = 5
AUTO_SCAN_SECONDS = 60 * 15  # scheduled scan every 15 min (tune as needed)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# ---------- Pydantic response/request shapes ----------

class BatchCreate(BaseModel):
    batch_code: str
    target_temp_c: float = 11.0
    target_humidity_pct: float = 95.0
    owner_phone: Optional[str] = None
    notes: Optional[str] = None


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
    init_db()
    db = SessionLocal()
    active = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if not active:
        active = StorageBatch(
            batch_code=f"BATCH-{datetime.utcnow():%Y%m%d-%H%M}",
            owner_phone="+639000000000",  # placeholder -- set the real number via POST /api/batch
        )
        db.add(active)
        db.commit()
    db.close()

    asyncio.create_task(iot_loop())
    asyncio.create_task(scheduled_scan_loop())


async def iot_loop():
    while True:
        db = SessionLocal()
        try:
            batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
            if batch:
                iot_controller.control_tick(batch, db)
        finally:
            db.close()
        await asyncio.sleep(IOT_POLL_SECONDS)


async def scheduled_scan_loop():
    while True:
        await asyncio.sleep(AUTO_SCAN_SECONDS)
        db = SessionLocal()
        try:
            batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
            if batch:
                _execute_scan(batch.id, db, trigger_type="scheduled")
        finally:
            db.close()


def _execute_scan(batch_id: int, db: Session, trigger_type: str) -> ScanSession:
    session_row = ScanSession(batch_id=batch_id, trigger_type=trigger_type)
    db.add(session_row)
    db.commit()
    db.refresh(session_row)

    samples, counts, bad_count = vision_pipeline.run_scan(batch_id)
    for s in samples:
        db.add(CucumberSample(scan_session_id=session_row.id, **s))

    session_row.ripe_count = counts["ripe"]
    session_row.near_ripe_count = counts["near_ripe"]
    session_row.unripe_count = counts["unripe"]
    session_row.spoiled_count = counts["spoiled"]
    db.commit()
    db.refresh(session_row)

    if bad_count > 0:
        batch = db.query(StorageBatch).get(batch_id)
        notifier.notify_bad_condition(db, batch, session_row.id, bad_count)

    return session_row


# ---------- Batch endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/system/status")
def system_status():
    return {
        "mode": iot_controller.DATA_MODE,
        "components": {
            "sensors": "simulated",
            "camera": "simulated",
            "actuators": "simulated",
            "sms": "simulated",
        },
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
        "owner_phone": batch.owner_phone,
        "notes": batch.notes,
    }


@app.post("/api/batch")
def create_batch(payload: BatchCreate, db: Session = Depends(get_db)):
    current = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if current:
        current.ended_at = datetime.utcnow()
    batch = StorageBatch(**payload.dict())
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return {"id": batch.id, "batch_code": batch.batch_code}


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


# ---------- Scan / vision endpoints ----------

@app.post("/api/scan/manual")
def trigger_manual_scan(db: Session = Depends(get_db)):
    batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    if not batch:
        raise HTTPException(404, "No active batch")
    session_row = _execute_scan(batch.id, db, trigger_type="manual")
    return {"scan_session_id": session_row.id}


@app.get("/api/scan/latest")
def latest_scan(db: Session = Depends(get_db)):
    session_row = db.query(ScanSession).order_by(desc(ScanSession.started_at)).first()
    if not session_row:
        raise HTTPException(404, "No scans yet")
    samples = (
        db.query(CucumberSample)
        .filter(CucumberSample.scan_session_id == session_row.id)
        .all()
    )
    return {
        "scan_session_id": session_row.id,
        "started_at": session_row.started_at,
        "trigger_type": session_row.trigger_type,
        "counts": {
            "ripe": session_row.ripe_count,
            "near_ripe": session_row.near_ripe_count,
            "unripe": session_row.unripe_count,
            "spoiled": session_row.spoiled_count,
        },
        "samples": [
            {
                "track_position_mm": s.track_position_mm,
                "bbox": [s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h],
                "yolo_confidence": s.yolo_confidence,
                "hsi_hue_mean": s.hsi_hue_mean,
                "lbp_texture_score": s.lbp_texture_score,
                "ripeness_class": s.ripeness_class,
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
    sample_counts = {row.id: {"good": 0, "bad": 0} for row in rows}
    if sample_counts:
        samples = (
            db.query(CucumberSample.scan_session_id, CucumberSample.condition)
            .filter(CucumberSample.scan_session_id.in_(sample_counts))
            .all()
        )
        for scan_session_id, condition in samples:
            if condition in ("good", "bad"):
                sample_counts[scan_session_id][condition] += 1

    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat(),
            "trigger_type": r.trigger_type,
            "ripe_count": r.ripe_count,
            "near_ripe_count": r.near_ripe_count,
            "unripe_count": r.unripe_count,
            "spoiled_count": r.spoiled_count,
            "detected_count": sum(sample_counts[r.id].values()),
            "good_count": sample_counts[r.id]["good"],
            "bad_count": sample_counts[r.id]["bad"],
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
        }
        for r in rows
    ]


# ---------- Serve frontend ----------

app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.get("/")
def serve_dashboard():
    return FileResponse(FRONTEND_DIR / "templates" / "index.html")
