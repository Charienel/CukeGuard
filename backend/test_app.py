import pytest
from fastapi.testclient import TestClient

from backend import iot_controller
from backend.app import app


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


def test_active_batch_is_available_after_startup(client):
    response = client.get("/api/batch/active")
    assert response.status_code == 200
    data = response.json()
    assert "batch_code" in data
    assert "started_at" in data


def test_manual_scan_creates_scan_session(client):
    response = client.post(
        "/api/scan/manual",
        json={},
    )
    assert response.status_code == 200
    data = response.json()
    assert "scan_session_id" in data

    history_response = client.get("/api/scan/history?limit=1")
    assert history_response.status_code == 200
    latest = history_response.json()[0]
    assert latest["detected_count"] == latest["good_count"] + latest["bad_count"]
