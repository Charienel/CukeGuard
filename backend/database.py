"""
database.py — SQLAlchemy models for CukeGuard, mirroring the ERD:
STORAGE_BATCH -> SCAN_SESSION -> CUCUMBER_SAMPLE
STORAGE_BATCH -> SENSOR_READING
"""
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, ForeignKey
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = "sqlite:///./cukeguard.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class StorageBatch(Base):
    """One batch of cucumbers loaded into the chamber for a storage run."""
    __tablename__ = "storage_batch"

    id = Column(Integer, primary_key=True, index=True)
    batch_code = Column(String, unique=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    target_temp_c = Column(Float, default=11.0)      # midpoint of 10-12.5C target
    target_humidity_pct = Column(Float, default=95.0)
    owner_phone = Column(String, nullable=True)       # SMS destination for bad-condition alerts
    notes = Column(String, nullable=True)

    sensor_readings = relationship("SensorReading", back_populates="batch")
    scan_sessions = relationship("ScanSession", back_populates="batch")
    alerts = relationship("AlertLog", back_populates="batch")


class SensorReading(Base):
    """Periodic climate reading from SHT31-D (temp/humidity) + DS18B20 (temp)."""
    __tablename__ = "sensor_reading"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("storage_batch.id"))
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    temperature_c = Column(Float)
    humidity_pct = Column(Float)
    peltier_pwm_pct = Column(Float)        # 0-100, cooling effort applied
    mister_active = Column(Integer)        # 0/1
    correction_note = Column(String, nullable=True)  # e.g. "humidity low -> mister on"

    batch = relationship("StorageBatch", back_populates="sensor_readings")


class ScanSession(Base):
    """One pass of the stepper-driven camera carriage across the V-slot track."""
    __tablename__ = "scan_session"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("storage_batch.id"))
    started_at = Column(DateTime, default=datetime.utcnow)
    trigger_type = Column(String, default="scheduled")  # "scheduled" | "manual"
    ripe_count = Column(Integer, default=0)
    near_ripe_count = Column(Integer, default=0)
    unripe_count = Column(Integer, default=0)
    spoiled_count = Column(Integer, default=0)

    batch = relationship("StorageBatch", back_populates="scan_sessions")
    samples = relationship("CucumberSample", back_populates="scan_session")


class CucumberSample(Base):
    """A single detected/classified cucumber within a scan session."""
    __tablename__ = "cucumber_sample"

    id = Column(Integer, primary_key=True, index=True)
    scan_session_id = Column(Integer, ForeignKey("scan_session.id"))
    track_position_mm = Column(Float)      # position along V-slot track at capture
    bbox_x = Column(Float)
    bbox_y = Column(Float)
    bbox_w = Column(Float)
    bbox_h = Column(Float)
    yolo_confidence = Column(Float)
    hsi_hue_mean = Column(Float)
    lbp_texture_score = Column(Float)
    ripeness_class = Column(String)        # "ripe" | "near_ripe" | "unripe" | "spoiled"
    condition = Column(String)             # "good" | "bad" -- the simple owner-facing gate
    est_shelf_life_days = Column(Float)

    scan_session = relationship("ScanSession", back_populates="samples")


class AlertLog(Base):
    """Record of every SMS sent to the owner when a bad cucumber is detected."""
    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("storage_batch.id"))
    scan_session_id = Column(Integer, ForeignKey("scan_session.id"), nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    phone_number = Column(String)
    message = Column(String)
    bad_count = Column(Integer, default=0)
    delivery_status = Column(String, default="sent")  # "sent" | "failed" (simulated)

    batch = relationship("StorageBatch", back_populates="alerts")


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
