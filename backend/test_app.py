import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from backend import iot_controller, vision_pipeline
from backend.app import app
from backend.database import CucumberSample, SessionLocal, engine


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_system_status_reports_demo_mode_and_target_bands(client):
    response = client.get("/api/system/status")
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "simulation"
    assert data["climate_ranges"]["temperature_c"] == {"min": 10.0, "max": 12.5}
    assert data["climate_ranges"]["humidity_pct"] == {"min": 90.0, "max": 95.0}


def test_controller_reports_missing_heating_and_dehumidification():
    pwm, mister, notes = iot_controller.plan_corrections(9.0, 97.0)
    assert pwm == 0
    assert mister is False
    assert any("heating hardware required" in note for note in notes)
    assert any("dehumidification hardware required" in note for note in notes)


def test_controller_corrects_high_temperature_and_low_humidity():
    pwm, mister, notes = iot_controller.plan_corrections(14.0, 85.0)
    assert pwm > 0
    assert mister is True
    assert any("cooling" in note for note in notes)
    assert any("mister on" in note for note in notes)


def test_simulated_readings_remain_inside_storage_ranges():
    readings = [iot_controller.read_temperature_humidity() for _ in range(50)]
    assert all(10.0 <= temp <= 12.5 for temp, _ in readings)
    assert all(90.0 <= humidity <= 95.0 for _, humidity in readings)


def test_scan_numbers_cucumbers_right_to_left(monkeypatch):
    def fake_detect(frame):
        positions = {600: [100, 500], 0: [300]}
        return [
            {"bbox_x": x_position, "condition": "good"}
            for x_position in positions.get(frame["position_mm"], [])
        ]

    monkeypatch.setattr(vision_pipeline, "detect_and_classify", fake_detect)

    samples, counts = vision_pipeline.run_scan(batch_id=1)

    assert counts == {"good": 3, "bad": 0}
    assert [sample["cucumber_number"] for sample in samples] == [1, 2, 3]
    assert [(sample["track_position_mm"], sample["bbox_x"]) for sample in samples] == [
        (600, 500), (600, 100), (0, 300)
    ]


def test_scan_uses_batch_expected_quantity(monkeypatch):
    def fake_detect(frame, object_count=None):
        return [
            {"bbox_x": 100.0, "condition": "good"}
            for _ in range(object_count or 0)
        ]

    monkeypatch.setattr(vision_pipeline, "detect_and_classify", fake_detect)

    samples, counts = vision_pipeline.run_scan(batch_id=1, expected_count=3)

    assert len(samples) == 3
    assert counts == {"good": 3, "bad": 0}


def test_active_batch_is_available_after_startup(client):
    response = client.get("/api/batch/active")
    assert response.status_code == 200
    data = response.json()
    assert "batch_code" in data
    assert "started_at" in data


def test_manual_scan_creates_scan_session(client, monkeypatch):
    active_before = client.get("/api/batch/active").json()
    batch_response = client.post(
        "/api/batch",
        json={},
    )
    assert batch_response.status_code == 200
    new_batch = batch_response.json()
    assert new_batch["id"] > active_before["id"]
    assert new_batch["batch_code"] == f"BATCH-{new_batch['id']:04d}"
    assert new_batch["initial_quantity"] is None
    expected_owner_phone = active_before["owner_phone"] or "+639000000000"
    assert client.get("/api/batch/active").json()["owner_phone"] == expected_owner_phone
    assert client.get("/api/scan/latest").status_code == 404

    scan_call = {}

    def fixed_scan(batch_id, expected_count=None):
        scan_call.update(batch_id=batch_id, expected_count=expected_count)
        return [
            {
                "bbox_x": 500.0,
                "bbox_y": 100.0,
                "bbox_w": 80.0,
                "bbox_h": 60.0,
                "yolo_confidence": 0.9,
                "hsi_hue_mean": 55.0,
                "lbp_texture_score": 0.3,
                "condition": "bad",
                "est_shelf_life_days": 1.0,
                "track_position_mm": 600,
                "cucumber_number": 1,
            },
            {
                "bbox_x": 100.0,
                "bbox_y": 120.0,
                "bbox_w": 85.0,
                "bbox_h": 62.0,
                "yolo_confidence": 0.95,
                "hsi_hue_mean": 60.0,
                "lbp_texture_score": 0.2,
                "condition": "good",
                "est_shelf_life_days": 14.0,
                "track_position_mm": 0,
                "cucumber_number": 2,
            },
        ], {"good": 1, "bad": 1}

    monkeypatch.setattr(vision_pipeline, "run_scan", fixed_scan)
    response = client.post(
        "/api/scan/manual",
        json={},
    )
    assert response.status_code == 200
    data = response.json()
    assert "scan_session_id" in data
    assert scan_call == {"batch_id": new_batch["id"], "expected_count": None}

    history_response = client.get("/api/scan/history?limit=1")
    assert history_response.status_code == 200
    latest = history_response.json()[0]
    assert latest["detected_count"] == latest["good_count"] + latest["bad_count"]
    assert not any(key.endswith("_count") for key in latest if key not in {
        "detected_count", "good_count", "bad_count"
    })

    scan_response = client.get("/api/scan/latest")
    assert scan_response.status_code == 200
    scan = scan_response.json()
    assert set(scan["counts"]) == {"good", "bad"}
    assert scan["batch_id"] == new_batch["id"]
    assert all(sample["condition"] in {"good", "bad"} for sample in scan["samples"])
    assert all("ripeness_class" not in sample for sample in scan["samples"])
    assert all(sample["batch_id"] == scan["batch_id"] for sample in scan["samples"])
    assert all(sample["scan_id"] == scan["scan_session_id"] for sample in scan["samples"])
    assert all(isinstance(sample["est_shelf_life_days"], float) for sample in scan["samples"])
    assert [sample["cucumber_number"] for sample in scan["samples"]] == [1, 2]

    alert_response = client.get("/api/alerts/history?limit=20")
    assert alert_response.status_code == 200
    alert_messages = [alert["message"] for alert in alert_response.json()]
    alert_message = next(
        message for message in alert_messages
        if f"Scan S-{scan['scan_session_id']:04d}:" in message
    )
    assert f"Batch B-{scan['batch_id']:04d}" in alert_message
    assert "bad cucumber(s) #1" in alert_message

    scan_session_columns = {
        column["name"] for column in inspect(engine).get_columns("scan_session")
    }
    cucumber_sample_columns = {
        column["name"] for column in inspect(engine).get_columns("cucumber_sample")
    }
    assert not scan_session_columns.intersection({
        "ripe_count", "near_ripe_count", "unripe_count", "spoiled_count"
    })
    assert {"batch_id", "scan_id", "cucumber_number", "est_shelf_life_days"}.issubset(cucumber_sample_columns)
    assert not cucumber_sample_columns.intersection({"ripeness_class", "scan_session_id"})

    db = SessionLocal()
    try:
        assert db.query(CucumberSample).filter(CucumberSample.batch_id.is_(None)).count() == 0
    finally:
        db.close()
