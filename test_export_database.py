from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, CucumberSample, ScanSession, SensorReading, StorageBatch
from export_database import (
    ALERT_FILLS,
    ExportFilters,
    load_rows,
    parse_end_date,
    style_data_sheet,
)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_export_filters_rows_and_associates_sensor_reading_with_prior_scan(session_factory):
    db = session_factory()
    batch_a = StorageBatch(
        batch_code="BATCH-A",
        started_at=datetime(2026, 1, 1),
    )
    batch_b = StorageBatch(
        batch_code="BATCH-B",
        started_at=datetime(2026, 1, 1),
        ended_at=datetime(2026, 1, 5),
    )
    db.add_all([batch_a, batch_b])
    db.flush()

    scan_a = ScanSession(
        batch_id=batch_a.id,
        started_at=datetime(2026, 1, 2, 12),
        trigger_type="manual",
    )
    scan_b = ScanSession(
        batch_id=batch_b.id,
        started_at=datetime(2026, 1, 2, 12),
        trigger_type="manual",
    )
    latest_scan_a = ScanSession(
        batch_id=batch_a.id,
        started_at=datetime(2026, 1, 3, 9),
        trigger_type="scheduled",
    )
    db.add_all([scan_a, scan_b, latest_scan_a])
    db.flush()
    db.add_all([
        CucumberSample(
            batch_id=batch_a.id, scan_id=scan_a.id, cucumber_number=1,
            condition="good", yolo_confidence=0.99, hsi_hue_mean=10.0,
            lbp_texture_score=0.1, est_shelf_life_days=17.0,
        ),
        CucumberSample(
            batch_id=batch_a.id, scan_id=latest_scan_a.id, cucumber_number=1,
            condition="bad", yolo_confidence=0.8, hsi_hue_mean=75.0,
            lbp_texture_score=0.4, est_shelf_life_days=2.0,
        ),
        CucumberSample(
            batch_id=batch_a.id, scan_id=latest_scan_a.id, cucumber_number=2,
            condition="good", yolo_confidence=0.9, hsi_hue_mean=45.0,
            lbp_texture_score=0.2, est_shelf_life_days=14.0,
        ),
        CucumberSample(batch_id=batch_b.id, scan_id=scan_b.id, cucumber_number=1, condition="bad"),
    ])
    db.add_all([
        SensorReading(
            batch_id=batch_a.id,
            timestamp=datetime(2026, 1, 3, 8),
            temperature_c=11.0,
            humidity_pct=92.0,
            peltier_pwm_pct=0,
            mister_active=0,
        ),
        SensorReading(
            batch_id=batch_a.id,
            timestamp=datetime(2026, 1, 3, 10),
            temperature_c=13.0,
            humidity_pct=92.0,
            peltier_pwm_pct=20,
            mister_active=0,
        ),
        SensorReading(
            batch_id=batch_a.id,
            timestamp=datetime(2026, 1, 4, 10),
            temperature_c=11.0,
            humidity_pct=92.0,
            peltier_pwm_pct=0,
            mister_active=0,
        ),
        SensorReading(
            batch_id=batch_b.id,
            timestamp=datetime(2026, 1, 3, 10),
            temperature_c=13.0,
            humidity_pct=92.0,
            peltier_pwm_pct=20,
            mister_active=0,
        ),
    ])
    db.commit()
    db.close()

    filters = ExportFilters(
        start_date=datetime(2026, 1, 3),
        end_date=parse_end_date("2026-01-03"),
        batch_ids=(batch_a.id, batch_b.id),
        exclude_batch_ids=(batch_b.id,),
        exclude_alert_statuses=("Normal",),
    )
    rows, _ = load_rows(filters, session_factory=session_factory)

    assert len(rows["readings"]) == 1
    reading = rows["readings"][0]
    assert reading[:2] == [datetime(2026, 1, 1), datetime(2026, 1, 3)]
    assert reading[3:7] == [batch_a.id, "BATCH-A", datetime(2026, 1, 3, 10), latest_scan_a.id]
    assert reading[11] == "Threshold Breach: temperature"
    batch_row = rows["batches"][0]
    assert batch_row[2] == batch_a.id
    assert batch_row[11:] == [
        latest_scan_a.id,
        datetime(2026, 1, 3, 9),
        0.85,
        60.0,
        0.3,
        "1 good, 1 bad",
        8.0,
    ]
    assert [sample[13:18] for sample in rows["samples"]] == [
        [0.8, 75.0, 0.4, "bad", 2.0],
        [0.9, 45.0, 0.2, "good", 14.0],
    ]

    active_rows, _ = load_rows(
        ExportFilters(active_only=True), session_factory=session_factory
    )
    assert [batch[2] for batch in active_rows["batches"]] == [batch_a.id]
    assert {reading[3] for reading in active_rows["readings"]} == {batch_a.id}
    assert {scan[3] for scan in active_rows["scans"]} == {batch_a.id}
    assert {sample[3] for sample in active_rows["samples"]} == {batch_a.id}


def test_export_highlights_sensor_alert_statuses():
    from openpyxl import Workbook

    sheet = Workbook().active
    style_data_sheet(
        sheet,
        ["Alert Status"],
        [["Normal"], ["Warning"], ["Threshold Breach: temperature"]],
    )

    assert sheet["A2"].fill.fgColor.rgb.endswith(ALERT_FILLS["Normal"].fgColor.rgb[-6:])
    assert sheet["A3"].fill.fgColor.rgb.endswith(ALERT_FILLS["Warning"].fgColor.rgb[-6:])
    assert sheet["A4"].fill.fgColor.rgb.endswith(ALERT_FILLS["Threshold Breach"].fgColor.rgb[-6:])


def test_batch_average_confidence_is_formatted_as_percentage():
    from openpyxl import Workbook

    sheet = Workbook().active
    style_data_sheet(
        sheet,
        ["YOLO Confidence (Latest Scan Avg)"],
        [[0.83]],
    )

    assert sheet["A2"].number_format == "0.0%"
