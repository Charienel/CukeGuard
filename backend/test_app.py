import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from urllib.parse import parse_qs

os.environ["CUKEGUARD_OWNER_PHONE"] = "+639111111111"
os.environ["CUKEGUARD_DATABASE_URL"] = "sqlite://"

from backend import iot_controller, notifier, vision_pipeline
from backend.app import (
    AUTO_SCAN_SECONDS,
    OWNER_PHONE,
    OwnerPhoneUpdate,
    _climate_alert_details,
    app,
)
from backend.database import Base, CucumberSample, SessionLocal, engine


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
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
    assert data["auto_scan_interval_seconds"] == AUTO_SCAN_SECONDS == 900
    next_scan = datetime.fromisoformat(data["next_auto_scan_at"])
    assert next_scan.tzinfo == timezone.utc
    assert 0 < (next_scan - datetime.now(timezone.utc)).total_seconds() <= AUTO_SCAN_SECONDS
    assert data["climate_ranges"]["temperature_c"] == {"min": 10.0, "max": 12.5}
    assert data["climate_ranges"]["humidity_pct"] == {"min": 90.0, "max": 95.0}
    assert data["components"]["stepper"] == "simulated"
    assert data["components"]["limit_switches"] == "not_configured"
    assert any(
        "exact HAT driver/API" in component["detail"]
        for component in data["hardware_integration"]
        if component["name"] == "NEMA17 + Raspberry Pi Stepper HAT"
    )


def test_hardware_integration_guide_is_served(client):
    response = client.get("/hardware-integration")

    assert response.status_code == 200
    assert "Raspberry Pi Stepper Motor HAT" in response.text
    assert "Never directly" not in response.text
    assert "never directly to Raspberry Pi GPIO" in response.text


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


def test_climate_alert_describes_active_cooler_and_humidifier():
    reading = SimpleNamespace(
        temperature_c=14.0,
        humidity_pct=85.0,
        peltier_pwm_pct=60.0,
        mister_active=1,
    )

    conditions, message = _climate_alert_details(reading)

    assert conditions == ("temperature_high", "humidity_low")
    assert "cooler activated at 60%" in message
    assert "humidifier boosted (mister activated)" in message


def test_climate_alert_waits_for_hysteresis_before_recovery():
    recovering = SimpleNamespace(
        temperature_c=12.4,
        humidity_pct=90.5,
        peltier_pwm_pct=0.0,
        mister_active=0,
    )
    recovered = SimpleNamespace(
        temperature_c=12.1,
        humidity_pct=91.1,
        peltier_pwm_pct=0.0,
        mister_active=0,
    )

    active_conditions, message = _climate_alert_details(
        recovering, ("temperature_high", "humidity_low")
    )
    clear_conditions, _ = _climate_alert_details(
        recovered, active_conditions
    )

    assert active_conditions == ("temperature_high", "humidity_low")
    assert "recovering" in message
    assert clear_conditions == ()


def test_climate_notifications_are_deduplicated_until_recovery(monkeypatch):
    class FakeDB:
        def __init__(self):
            self.alerts = []

        def add(self, alert):
            self.alerts.append(alert)

        def commit(self):
            pass

        def refresh(self, alert):
            alert.id = len(self.alerts)

    db = FakeDB()
    batch = SimpleNamespace(id=99001, batch_code="TEST-CLIMATE", owner_phone="+639123456789")
    reading = SimpleNamespace(timestamp=datetime.now(timezone.utc))
    sent_messages = []
    monkeypatch.setattr(notifier, "_active_climate_conditions", {})
    monkeypatch.setattr(
        notifier,
        "send_sms",
        lambda phone, message: sent_messages.append(message) or "simulated",
    )

    first = notifier.notify_climate_change(
        db, batch, reading, ("temperature_high",), "cooler activated"
    )
    duplicate = notifier.notify_climate_change(
        db, batch, reading, ("temperature_high",), "cooler activated"
    )
    recovered = notifier.notify_climate_change(db, batch, reading, (), "")

    assert first is db.alerts[0]
    assert first.alert_type == "climate"
    assert duplicate is None
    assert recovered is db.alerts[1]
    assert "returned to the target" in recovered.message
    assert len(sent_messages) == 2


def test_simulated_readings_remain_inside_storage_ranges():
    readings = [iot_controller.read_temperature_humidity() for _ in range(50)]
    assert all(10.0 <= temp <= 12.5 for temp, _ in readings)
    assert all(90.0 <= humidity <= 95.0 for _, humidity in readings)


def test_sms_is_explicitly_simulated_without_provider(monkeypatch):
    monkeypatch.delenv("CUKEGUARD_SMS_PROVIDER", raising=False)
    monkeypatch.delenv("SEMAPHORE_API_KEY", raising=False)

    assert notifier.sms_mode() == "simulated"
    assert notifier.send_sms("+639000000000", "test") == "simulated"


def test_sms_submits_to_semaphore_when_configured(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return b'[{"status":"Queued"}]'

    request_data = {}

    def fake_urlopen(request, timeout):
        request_data.update(
            form=parse_qs(request.data.decode("utf-8")),
            timeout=timeout,
        )
        return FakeResponse()

    monkeypatch.setenv("CUKEGUARD_SMS_PROVIDER", "semaphore")
    monkeypatch.setenv("SEMAPHORE_API_KEY", "test-key")
    monkeypatch.setenv("SEMAPHORE_SENDER_NAME", "CukeGuard")
    monkeypatch.setattr(notifier, "urlopen", fake_urlopen)

    assert notifier.sms_mode() == "semaphore"
    assert notifier.send_sms("+639123456789", "bad cucumber") == "submitted"
    assert request_data == {
        "form": {
            "apikey": ["test-key"],
            "number": ["+639123456789"],
            "message": ["bad cucumber"],
            "sendername": ["CukeGuard"],
        },
        "timeout": 10,
    }


def test_sms_submits_to_iprog_when_configured(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return b'{"status":200,"message":"queued","message_id":"test-123"}'

    request_data = {}

    def fake_urlopen(request, timeout):
        request_data.update(
            url=request.full_url,
            body=request.data,
            content_type=request.get_header("Content-type"),
            timeout=timeout,
        )
        return FakeResponse()

    monkeypatch.setenv("CUKEGUARD_SMS_PROVIDER", "iprog")
    monkeypatch.setenv("IPROG_API_TOKEN", "test-token")
    monkeypatch.setenv("IPROG_SMS_PROVIDER", "1")
    monkeypatch.setattr(notifier, "urlopen", fake_urlopen)

    assert notifier.sms_mode() == "iprog"
    assert notifier.send_sms("+639123456789", "bad cucumber") == "submitted"
    assert "api_token=test-token" in request_data["url"]
    assert request_data["content_type"] == "application/json"
    assert request_data["timeout"] == 10
    assert json.loads(request_data["body"]) == {
        "api_token": "test-token",
        "phone_number": "639123456789",
        "message": "bad cucumber",
        "sms_provider": 1,
    }


def test_iprog_failure_exposes_redacted_provider_reason(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return b'{"status":401,"message":"Invalid API token test-token"}'

    monkeypatch.setenv("CUKEGUARD_SMS_PROVIDER", "iprog")
    monkeypatch.setenv("IPROG_API_TOKEN", "test-token")
    monkeypatch.setattr(notifier, "urlopen", lambda request, timeout: FakeResponse())

    status = notifier.send_sms("639123456789", "bad cucumber")

    assert status == "failed: Invalid API token [redacted]"
    assert "test-token" not in status


def test_iprog_selection_without_token_fails_instead_of_simulating(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_SMS_PROVIDER", "iprog")
    monkeypatch.delenv("IPROG_API_TOKEN", raising=False)

    assert notifier.sms_mode() == "iprog_unconfigured"
    assert notifier.send_sms("639123456789", "bad cucumber") == "failed"


def test_scan_numbers_cucumbers_right_to_left(monkeypatch):
    def fake_detect(frame):
        positions = {600: [100, 500], 0: [300]}
        return [
            {"bbox_x": x_position, "condition": "good"}
            for x_position in positions.get(frame["position_mm"], [])
        ]

    monkeypatch.setattr(vision_pipeline, "detect_and_classify", fake_detect)

    samples, counts = vision_pipeline.run_scan(batch_id=1)

    assert counts == {"good": 3, "bad": 0, "needs_inspection": 0}
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
    assert counts == {"good": 3, "bad": 0, "needs_inspection": 0}


def test_active_batch_is_available_after_startup(client):
    response = client.get("/api/batch/active")
    assert response.status_code == 200
    data = response.json()
    assert "batch_code" in data
    assert "started_at" in data


def test_automation_history_endpoint_returns_daily_log(client):
    response = client.get("/api/automation/history?limit=3")

    assert response.status_code == 200
    data = response.json()
    assert {"date", "count", "events"} == set(data)
    assert data["count"] >= len(data["events"])
    assert len(data["events"]) <= 3
    assert all("note" in event for event in data["events"])


def test_owner_phone_is_normalized_and_validated():
    assert OwnerPhoneUpdate(owner_phone=" 09111111111 ").owner_phone == OWNER_PHONE
    assert OwnerPhoneUpdate(owner_phone=" +63 911-111-1111 ").owner_phone == OWNER_PHONE

    with pytest.raises(ValueError, match="7–15 digits"):
        OwnerPhoneUpdate(owner_phone="not a phone")

    with pytest.raises(ValueError, match="permanently configured"):
        OwnerPhoneUpdate(owner_phone="+639123456789")


def test_owner_phone_is_fixed_for_active_and_new_batches(client):
    assert client.get("/api/batch/active").json()["owner_phone"] == OWNER_PHONE

    response = client.post("/api/batch", json={"owner_phone": "+639123456789"})
    assert response.status_code == 422

    response = client.post("/api/batch", json={})
    assert response.status_code == 200
    assert client.get("/api/batch/active").json()["owner_phone"] == OWNER_PHONE


def test_manual_scan_creates_scan_session(client, monkeypatch):
    active_before = client.get("/api/batch/active").json()
    batch_response = client.post(
        "/api/batch",
        json={"initial_quantity": 3},
    )
    assert batch_response.status_code == 200
    new_batch = batch_response.json()
    assert new_batch["id"] > active_before["id"]
    assert new_batch["batch_code"] == f"BATCH-{new_batch['id']:04d}"
    assert new_batch["initial_quantity"] == 3
    assert active_before["owner_phone"] == OWNER_PHONE
    assert client.get("/api/batch/active").json()["owner_phone"] == OWNER_PHONE
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
            {
                "bbox_x": 300.0,
                "bbox_y": 110.0,
                "bbox_w": 82.0,
                "bbox_h": 61.0,
                "yolo_confidence": 0.75,
                "hsi_hue_mean": 108.0,
                "lbp_texture_score": 12.0,
                "condition": "needs_inspection",
                "est_shelf_life_days": None,
                "track_position_mm": 300,
                "cucumber_number": 3,
            },
        ], {"good": 1, "bad": 1, "needs_inspection": 1}

    monkeypatch.setattr(vision_pipeline, "run_scan", fixed_scan)
    schedule_resets = []
    monkeypatch.setattr("backend.app._schedule_next_auto_scan", lambda: schedule_resets.append(True))
    response = client.post(
        "/api/scan/manual",
        json={},
    )
    assert response.status_code == 200
    assert schedule_resets == [True]
    data = response.json()
    assert "scan_session_id" in data
    assert scan_call == {"batch_id": new_batch["id"], "expected_count": 3}

    history_response = client.get("/api/scan/history?limit=1")
    assert history_response.status_code == 200
    latest = history_response.json()[0]
    assert latest["detected_count"] == (
        latest["good_count"] + latest["bad_count"] + latest["needs_inspection_count"]
    )
    assert not any(key.endswith("_count") for key in latest if key not in {
        "detected_count", "good_count", "bad_count", "needs_inspection_count"
    })

    scan_response = client.get("/api/scan/latest")
    assert scan_response.status_code == 200
    scan = scan_response.json()
    assert set(scan["counts"]) == {"good", "bad", "needs_inspection"}
    assert scan["batch_id"] == new_batch["id"]
    assert all(sample["condition"] in {"good", "bad", "needs_inspection"} for sample in scan["samples"])
    assert all("ripeness_class" not in sample for sample in scan["samples"])
    assert all(sample["batch_id"] == scan["batch_id"] for sample in scan["samples"])
    assert all(sample["scan_id"] == scan["scan_session_id"] for sample in scan["samples"])
    assert all(
        sample["est_shelf_life_days"] is None
        if sample["condition"] == "needs_inspection"
        else isinstance(sample["est_shelf_life_days"], float)
        for sample in scan["samples"]
    )
    assert scan["counts"] == {"good": 1, "bad": 1, "needs_inspection": 1}
    assert latest["needs_inspection_count"] == 1
    assert [sample["cucumber_number"] for sample in scan["samples"]] == [1, 2, 3]

    alert_response = client.get("/api/alerts/history?limit=20")
    assert alert_response.status_code == 200
    alert = next(
        row for row in alert_response.json()
        if f"Scan S-{scan['scan_session_id']:04d}:" in row["message"]
    )
    alert_message = alert["message"]
    assert alert["delivery_status"] == "simulated"
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
