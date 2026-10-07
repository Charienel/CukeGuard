"""
database.py — SQLAlchemy models for CukeGuard, mirroring the ERD:
STORAGE_BATCH -> SCAN_SESSION -> CUCUMBER_SAMPLE
STORAGE_BATCH -> SENSOR_READING
"""
from datetime import datetime
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, ForeignKey, inspect
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.getenv("CUKEGUARD_DATABASE_URL", "sqlite:///./cukeguard.db")
engine_options = {"connect_args": {"check_same_thread": False}}
if DATABASE_URL in {"sqlite://", "sqlite:///:memory:"}:
    engine_options["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class StorageBatch(Base):
    """One batch of cucumbers loaded into the chamber for a storage run."""
    __tablename__ = "storage_batch"

    id = Column(Integer, primary_key=True, index=True)
    batch_code = Column(String, unique=True, index=True)
    cucumber_variety = Column(String, nullable=True)
    initial_quantity = Column(Integer, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    target_temp_c = Column(Float, default=11.0)      # midpoint of 10-12.5C target
    target_humidity_pct = Column(Float, default=95.0)
    owner_phone = Column(String, nullable=True)       # SMS destination for bad-condition alerts
    notes = Column(String, nullable=True)

    sensor_readings = relationship("SensorReading", back_populates="batch")
    scan_sessions = relationship("ScanSession", back_populates="batch")
    samples = relationship("CucumberSample", back_populates="batch")
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

    batch = relationship("StorageBatch", back_populates="scan_sessions")
    samples = relationship("CucumberSample", back_populates="scan_session")


class CucumberSample(Base):
    """A single detected/classified cucumber within a scan session."""
    __tablename__ = "cucumber_sample"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("storage_batch.id"), index=True)
    scan_id = Column(Integer, ForeignKey("scan_session.id"), index=True)
    cucumber_number = Column(Integer, nullable=False)
    track_position_mm = Column(Float)      # position along V-slot track at capture
    bbox_x = Column(Float)
    bbox_y = Column(Float)
    bbox_w = Column(Float)
    bbox_h = Column(Float)
    yolo_confidence = Column(Float)
    hsi_hue_mean = Column(Float)
    lbp_texture_score = Column(Float)
    condition = Column(String)             # "good" | "bad" | "needs_inspection"
    est_shelf_life_days = Column(Float)

    batch = relationship("StorageBatch", back_populates="samples")
    scan_session = relationship("ScanSession", back_populates="samples")


class AlertLog(Base):
    """Record owner notifications for cucumber condition and climate events."""
    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("storage_batch.id"))
    scan_session_id = Column(Integer, ForeignKey("scan_session.id"), nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    phone_number = Column(String)
    message = Column(String)
    bad_count = Column(Integer, default=0)
    delivery_status = Column(String, default="simulated")  # "submitted" | "simulated" | "failed"
    alert_type = Column(String, nullable=False, default="condition")

    batch = relationship("StorageBatch", back_populates="alerts")


def init_db():
    Base.metadata.create_all(bind=engine)
    legacy_columns = {
        "scan_session": (
            "ripe_count",
            "near_ripe_count",
            "unripe_count",
            "spoiled_count",
        ),
        "cucumber_sample": ("ripeness_class",),
    }
    with engine.begin() as connection:
        inspector = inspect(connection)

        batch_columns = {
            column["name"] for column in inspector.get_columns("storage_batch")
        }
        if "cucumber_variety" not in batch_columns:
            connection.exec_driver_sql(
                'ALTER TABLE "storage_batch" ADD COLUMN "cucumber_variety" VARCHAR'
            )
        if "initial_quantity" not in batch_columns:
            connection.exec_driver_sql(
                'ALTER TABLE "storage_batch" ADD COLUMN "initial_quantity" INTEGER'
            )

        sample_columns = {
            column["name"]
            for column in inspector.get_columns("cucumber_sample")
        }
        if "scan_session_id" in sample_columns and "scan_id" not in sample_columns:
            connection.exec_driver_sql(
                'ALTER TABLE "cucumber_sample" '
                'RENAME COLUMN "scan_session_id" TO "scan_id"'
            )
            sample_columns.remove("scan_session_id")
            sample_columns.add("scan_id")
        if "batch_id" not in sample_columns:
            connection.exec_driver_sql(
                'ALTER TABLE "cucumber_sample" '
                'ADD COLUMN "batch_id" INTEGER REFERENCES storage_batch(id)'
            )
            sample_columns.add("batch_id")
        if "est_shelf_life_days" not in sample_columns:
            connection.exec_driver_sql(
                'ALTER TABLE "cucumber_sample" ADD COLUMN "est_shelf_life_days" FLOAT'
            )
        if "cucumber_number" not in sample_columns:
            connection.exec_driver_sql(
                'ALTER TABLE "cucumber_sample" '
                'ADD COLUMN "cucumber_number" INTEGER NOT NULL DEFAULT 0'
            )
            old_samples = connection.exec_driver_sql(
                "SELECT id, scan_id FROM cucumber_sample "
                "ORDER BY scan_id, track_position_mm DESC, bbox_x DESC, id"
            ).fetchall()
            number_by_scan = {}
            for sample_id, scan_id in old_samples:
                number_by_scan[scan_id] = number_by_scan.get(scan_id, 0) + 1
                connection.exec_driver_sql(
                    "UPDATE cucumber_sample SET cucumber_number = ? WHERE id = ?",
                    (number_by_scan[scan_id], sample_id),
                )
        connection.exec_driver_sql(
            "UPDATE cucumber_sample "
            "SET batch_id = (SELECT batch_id FROM scan_session "
            "WHERE scan_session.id = cucumber_sample.scan_id) "
            "WHERE batch_id IS NULL"
        )

        for table_name, columns in legacy_columns.items():
            existing_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            for column_name in columns:
                if column_name in existing_columns:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'
                    )
                    existing_columns.remove(column_name)

        connection.exec_driver_sql(
            "UPDATE alert_log SET delivery_status = 'simulated' "
            "WHERE delivery_status = 'sent'"
        )
        alert_columns = {
            column["name"] for column in inspector.get_columns("alert_log")
        }
        if "alert_type" not in alert_columns:
            connection.exec_driver_sql(
                'ALTER TABLE "alert_log" ADD COLUMN "alert_type" '
                "VARCHAR NOT NULL DEFAULT 'condition'"
            )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
