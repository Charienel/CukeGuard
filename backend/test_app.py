import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from urllib.parse import parse_qs

from calibration import common, hw

os.environ["CUKEGUARD_OWNER_PHONE"] = "+639111111111"
os.environ["CUKEGUARD_DATABASE_URL"] = "sqlite://"

from backend import iot_controller, notifier, vision_pipeline
from backend.app import (
    AUTO_SCAN_SECONDS,
    OWNER_PHONE,
    OwnerPhoneUpdate,
    _climate_alert_details,
    _scan_on_climate_excursion,
    app,
)
from backend.database import (
    Base,
    CucumberSample,
    SensorReading,
    ScanSession,
    SessionLocal,
    StorageBatch,
    engine,
    init_db,
)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as test_client:
        yield test_client


def patch_scan_detections(monkeypatch, detect):
    monkeypatch.setattr(vision_pipeline, "detect_cucumber_crops", detect)
    monkeypatch.setattr(
        vision_pipeline,
        "classify_cucumber_crop",
        lambda detection: {
            key: value for key, value in detection.items() if key != "_crop"
        },
    )


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_stale_sensor_readings_are_flagged_and_cooler_is_unmonitored(client):
    db = SessionLocal()
    db.add(
        SensorReading(
            timestamp=datetime.utcnow() - timedelta(days=2),
            temperature_c=13.0,
            humidity_pct=92.0,
            peltier_pwm_pct=100.0,
            mister_active=0,
        )
    )
    db.commit()
    db.close()

    response = client.get("/api/sensors/latest")

    assert response.status_code == 200
    data = response.json()
    assert data["is_stale"] is True
    assert data["cooler_available"] is False
    assert data["cooler_active"] is False
    assert data["cooler_power_mode"] == "continuous_unverified"
    assert data["is_correcting"] is False
    assert data["control_status"] == "needs_hardware"


def test_system_status_reports_hardware_mode_and_target_bands(client):
    response = client.get("/api/system/status")
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "hardware"
    assert data["auto_scan_interval_seconds"] == AUTO_SCAN_SECONDS == 10800
    next_scan = datetime.fromisoformat(data["next_auto_scan_at"])
    assert next_scan.tzinfo == timezone.utc
    assert 0 < (next_scan - datetime.now(timezone.utc)).total_seconds() <= AUTO_SCAN_SECONDS
    assert data["climate_ranges"]["temperature_c"] == {"min": 10.0, "max": 12.5}
    assert data["climate_ranges"]["humidity_pct"] == {"min": 90.0, "max": 95.0}
    assert data["components"]["stepper"] == "configured_not_calibrated"
    assert data["components"]["limit_switches"] == "configured_not_confirmed"
    assert data["components"]["cabin_light"] == "configured_not_tested"
    assert any(
        component["name"] == "Cabin LED strip"
        and component["status"] == "configured_not_tested"
        for component in data["hardware_integration"]
    )
    assert any(
        "exact HAT driver/API" in component["detail"]
        for component in data["hardware_integration"]
        if component["name"] == "NEMA17 + Raspberry Pi Stepper HAT"
    )


def test_climate_excursion_triggers_immediate_scan_once(monkeypatch):
    batch = SimpleNamespace(id=42)
    db = object()
    scan_calls = []
    monkeypatch.setattr(
        "backend.app._execute_scan",
        lambda batch_id, session, trigger_type: scan_calls.append(
            (batch_id, session, trigger_type)
        ),
    )

    _scan_on_climate_excursion(batch, db, ("humidity_low",), ())
    _scan_on_climate_excursion(
        batch, db, ("humidity_low",), ("humidity_low",)
    )
    _scan_on_climate_excursion(
        batch,
        db,
        ("humidity_low", "temperature_high"),
        ("humidity_low",),
    )

    assert scan_calls == [
        (42, db, "climate_override"),
        (42, db, "climate_override"),
    ]


def test_hardware_integration_guide_is_served(client):
    response = client.get("/hardware-integration")

    assert response.status_code == 200
    assert "Raspberry Pi Stepper Motor HAT" in response.text
    assert "Never connect motor coils" in response.text
    assert "directly to Pi GPIO" in response.text


def test_controller_reports_manual_cooler_and_missing_dehumidification():
    pwm, mister, notes = iot_controller.plan_corrections(9.0, 97.0)
    assert pwm == 0
    assert mister is False
    assert any("switch off the continuously powered cooler manually" in note for note in notes)
    assert any("dehumidification hardware required" in note for note in notes)


def test_controller_only_requests_mister_and_reports_continuous_cooler():
    pwm, mister, notes = iot_controller.plan_corrections(14.0, 85.0)
    assert pwm == 0
    assert mister is True
    assert any("continuously powered" in note for note in notes)
    assert any("mister on" in note for note in notes)


def test_climate_alert_describes_continuous_cooler_and_humidifier():
    reading = SimpleNamespace(
        temperature_c=14.0,
        humidity_pct=85.0,
        peltier_pwm_pct=60.0,
        mister_active=1,
    )

    conditions, message = _climate_alert_details(reading)

    assert conditions == ("temperature_high", "humidity_low")
    assert "continuously powered" in message
    assert "mister's last commanded state is ON (not sensed)" in message


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


def test_toggle_mister_only_presses_when_humidity_target_state_changes(client, monkeypatch):
    readings = iter([(11.5, 89.0), (11.5, 88.0), (11.5, 90.0)])
    button_presses = []
    monkeypatch.setenv("CUKEGUARD_MISTER_INITIAL_STATE", "off")
    monkeypatch.setattr(iot_controller, "_mister_state_initialized", False)
    monkeypatch.setitem(iot_controller._actuator_state, "mister", None)
    monkeypatch.setattr(
        iot_controller,
        "read_temperature_humidity",
        lambda: next(readings),
    )
    monkeypatch.setattr(
        iot_controller,
        "press_mister_button",
        lambda: button_presses.append("press"),
    )

    db = SessionLocal()
    batch = db.query(StorageBatch).first()
    states = [
        iot_controller.control_tick(batch, db).mister_active
        for _ in range(3)
    ]
    db.close()

    assert states == [1, 1, 0]
    assert button_presses == ["press", "press"]


def test_unknown_mister_state_preserves_sensor_reading_without_toggling(client, monkeypatch):
    button_presses = []
    monkeypatch.delenv("CUKEGUARD_MISTER_INITIAL_STATE", raising=False)
    monkeypatch.setattr(iot_controller, "_mister_state_initialized", False)
    monkeypatch.setitem(iot_controller._actuator_state, "mister", None)
    monkeypatch.setattr(
        iot_controller,
        "read_temperature_humidity",
        lambda: (11.5, 85.0),
    )
    monkeypatch.setattr(
        iot_controller,
        "press_mister_button",
        lambda: button_presses.append("press"),
    )

    db = SessionLocal()
    batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    reading = iot_controller.control_tick(batch, db)
    db.close()
    response = client.get("/api/sensors/latest")

    assert reading.mister_active is None
    assert "automatic toggling is inhibited" in reading.correction_note
    assert button_presses == []
    assert response.json()["humidifier_active"] is None
    assert response.json()["control_status"] == "needs_hardware"


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
        lambda phone, message: sent_messages.append(message) or "submitted",
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


def test_hardware_reading_adapter_is_used(monkeypatch):
    monkeypatch.setattr(
        iot_controller,
        "read_temperature_humidity",
        lambda: (11.5, 92.5),
    )
    temp, humidity = iot_controller.read_temperature_humidity()
    assert 10.0 <= temp <= 12.5
    assert 90.0 <= humidity <= 95.0


def test_sms_is_explicitly_unconfigured_without_provider(monkeypatch):
    monkeypatch.delenv("CUKEGUARD_SMS_PROVIDER", raising=False)
    monkeypatch.delenv("SEMAPHORE_API_KEY", raising=False)

    assert notifier.sms_mode() == "unconfigured"
    assert notifier.send_sms("+639000000000", "test") == "failed: provider unconfigured"


def test_sms_submits_through_sim800l_uart(monkeypatch):
    class FakePort:
        def __init__(self):
            self.writes = []
            self.pending = bytearray()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def reset_input_buffer(self):
            pass

        def flush(self):
            pass

        def write(self, data):
            self.writes.append(data)
            if data == b"AT\r" or data == b"AT+CMGF=1\r":
                self.pending.extend(b"OK\r\n")
            elif data.startswith(b'AT+CMGS="'):
                self.pending.extend(b"> ")
            elif data.endswith(b"\x1a"):
                self.pending.extend(b"\r\n+CMGS: 12\r\nOK\r\n")

        def read(self, size=1):
            if not self.pending:
                return b""
            value = bytes(self.pending[:size])
            del self.pending[:size]
            return value

    fake_port = FakePort()
    monkeypatch.setenv("CUKEGUARD_SMS_PROVIDER", "sim800l")
    monkeypatch.setenv("CUKEGUARD_SIM800L_PORT", "/dev/serial0")
    monkeypatch.setitem(
        __import__("sys").modules,
        "serial",
        SimpleNamespace(Serial=lambda *args, **kwargs: fake_port),
    )

    assert notifier.sms_mode() == "sim800l"
    assert notifier.send_sms("+639709634243", "bad cucumber") == "submitted"
    assert b'AT+CMGS="+639709634243"\r' in fake_port.writes
    assert b"bad cucumber\x1a" in fake_port.writes


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
    monkeypatch.setattr(iot_controller, "set_cabin_light", lambda active: None)
    sample_positions = [31.75, 95.25, 158.75, 222.25, 254.0]
    monkeypatch.setattr(
        vision_pipeline,
        "home_and_scan_positions",
        lambda: sample_positions,
    )
    monkeypatch.setattr(
        vision_pipeline,
        "capture_frame",
        lambda position: {"position_mm": position, "image": object()},
    )
    def fake_detect(frame, object_count=None):
        positions = {31.75: [100], 95.25: [200], 158.75: [300], 222.25: [400]}
        samples = [
            {"bbox_x": x_position, "condition": "good"}
            for x_position in positions.get(frame["position_mm"], [])
        ]
        return samples

    patch_scan_detections(monkeypatch, fake_detect)

    samples, counts = vision_pipeline.run_scan(batch_id=1)

    assert counts == {"good": 4, "bad": 0}
    assert [sample["cucumber_number"] for sample in samples] == [1, 2, 3, 4]
    assert [(sample["track_position_mm"], sample["bbox_x"]) for sample in samples] == [
        (31.75, 100), (95.25, 200), (158.75, 300), (222.25, 400)
    ]


def test_hardware_scan_positions_are_bounded_to_254mm(monkeypatch):
    class FakeStepper:
        def __init__(self):
            self.step_calls = []

        def step(self, n, forward, delay_s, stop_if=None):
            self.step_calls.append((n, forward))
            return n

    stepper = FakeStepper()
    limits = SimpleNamespace(
        home=SimpleNamespace(is_pressed=True),
        far=SimpleNamespace(is_pressed=False),
    )
    monkeypatch.setattr(common, "load", lambda: {
        "carriage": {"steps_per_mm": 100, "step_delay_s": 0.001},
    })
    monkeypatch.setattr(hw, "Stepper", lambda: stepper)
    monkeypatch.setattr(hw, "Limits", lambda: limits)
    monkeypatch.setattr(
        vision_pipeline.home_and_scan_positions, "_stepper", None, raising=False
    )
    monkeypatch.setattr(
        vision_pipeline.home_and_scan_positions, "_limits", None, raising=False
    )

    positions = vision_pipeline.home_and_scan_positions()

    assert positions[0] == 0
    assert positions[-2:] == [250.0, 254.0]
    assert stepper.step_calls[1][0] == 25400


def test_scan_does_not_return_home_before_reaching_far_limit(monkeypatch):
    class FakeStepper:
        def __init__(self):
            self.step_calls = []
            self.released = False

        def step(self, n, forward, delay_s, stop_if=None):
            self.step_calls.append((n, forward))
            return n

        def release(self):
            self.released = True

    class FakeLimits:
        def __init__(self):
            self.home = SimpleNamespace(is_pressed=False)
            self.far = SimpleNamespace(is_pressed=False)
            self.closed = False

        def close(self):
            self.closed = True

    stepper = FakeStepper()
    limits = FakeLimits()
    fake_home_and_scan = lambda: [0.0]
    fake_home_and_scan._stepper = stepper
    fake_home_and_scan._limits = limits
    monkeypatch.setattr(vision_pipeline, "home_and_scan_positions", fake_home_and_scan)
    monkeypatch.setattr(
        vision_pipeline,
        "capture_frame",
        lambda position: {"position_mm": position, "image": object()},
    )
    monkeypatch.setattr(iot_controller, "set_cabin_light", lambda active: None)
    patch_scan_detections(monkeypatch, lambda frame, object_count=None: [])

    with pytest.raises(RuntimeError, match="did not reach the Far/MAX limit"):
        vision_pipeline.run_scan(batch_id=1)

    assert stepper.step_calls == []
    assert stepper.released is True
    assert limits.closed is True


def test_scan_always_uses_four_fixed_cucumbers(monkeypatch):
    monkeypatch.setattr(iot_controller, "set_cabin_light", lambda active: None)
    monkeypatch.setattr(
        vision_pipeline,
        "home_and_scan_positions",
        lambda: [0.0, 63.5, 127.0, 190.5],
    )
    monkeypatch.setattr(
        vision_pipeline,
        "capture_frame",
        lambda position: {"position_mm": position, "image": object()},
    )
    def fake_detect(frame, object_count=None):
        return [{"bbox_x": 100.0, "condition": "good"}]

    patch_scan_detections(monkeypatch, fake_detect)

    samples, counts = vision_pipeline.run_scan(batch_id=1, expected_count=3)

    assert len(samples) == 4
    assert counts == {"good": 4, "bad": 0}


def test_scan_keeps_best_detection_for_each_cucumber_slot(monkeypatch):
    monkeypatch.setattr(iot_controller, "set_cabin_light", lambda active: None)
    positions = [0.0, 5.0, 63.5, 68.5, 127.0, 132.0, 190.5, 195.5]
    classified_positions = []
    monkeypatch.setattr(vision_pipeline, "home_and_scan_positions", lambda: positions)
    monkeypatch.setattr(
        vision_pipeline,
        "capture_frame",
        lambda position: {"position_mm": position, "image": object()},
    )
    patch_scan_detections(
        monkeypatch,
        lambda frame, object_count=None: [{
            "bbox_x": frame["position_mm"],
            "yolo_confidence": 0.7 if frame["position_mm"] in {0.0, 63.5, 127.0, 190.5} else 0.95,
            "condition": "good",
        }],
    )
    monkeypatch.setattr(
        vision_pipeline,
        "classify_cucumber_crop",
        lambda detection: classified_positions.append(detection["track_position_mm"])
        or {
            key: value for key, value in detection.items() if key != "_crop"
        },
    )

    samples, counts = vision_pipeline.run_scan(batch_id=1)

    assert counts == {"good": 4, "bad": 0}
    assert [sample["cucumber_number"] for sample in samples] == [1, 2, 3, 4]
    assert [sample["track_position_mm"] for sample in samples] == [5, 68.5, 132, 195.5]
    assert classified_positions == [5, 68.5, 132, 195.5]


def test_scan_reports_missing_yolo_detections_without_fake_bad_rows(monkeypatch):
    light_states = []
    monkeypatch.setattr(iot_controller, "set_cabin_light", light_states.append)
    monkeypatch.setattr(
        vision_pipeline,
        "home_and_scan_positions",
        lambda: [0.0, 63.5, 127.0, 190.5],
    )
    monkeypatch.setattr(
        vision_pipeline,
        "capture_frame",
        lambda position: {"position_mm": position, "image": object()},
    )
    patch_scan_detections(
        monkeypatch,
        lambda frame, object_count=None: (
            [{"bbox_x": 1.0, "yolo_confidence": 0.9, "condition": "good"}]
            if frame["position_mm"] == 0.0 else []
        ),
    )

    with pytest.raises(RuntimeError, match="YOLO detected only 1 of 4 cucumbers"):
        vision_pipeline.run_scan(batch_id=1)

    assert light_states == [True, False]


def test_legacy_inspection_samples_migrate_to_good_or_bad(client):
    db = SessionLocal()
    batch = StorageBatch(batch_code="MIGRATION-TEST")
    db.add(batch)
    db.commit()
    scan = ScanSession(batch_id=batch.id)
    db.add(scan)
    db.commit()
    samples = [
        CucumberSample(
            batch_id=batch.id,
            scan_id=scan.id,
            cucumber_number=number,
            hsi_hue_mean=hue,
            lbp_texture_score=texture,
            condition="needs_inspection",
        )
        for number, hue, texture in (
            (1, 105.0, 10.0),
            (2, 65.0, 10.0),
            (3, None, None),
        )
    ]
    db.add_all(samples)
    db.commit()
    sample_ids = [sample.id for sample in samples]
    db.close()

    init_db()

    db = SessionLocal()
    migrated_conditions = [
        db.get(CucumberSample, sample_id).condition for sample_id in sample_ids
    ]
    db.close()
    assert migrated_conditions == ["good", "bad", "bad"]


def test_cabin_light_hardware_state_tracks_requested_state(monkeypatch):
    states = []
    monkeypatch.setattr(iot_controller, "set_cabin_light", lambda active: states.append(active))
    iot_controller.set_cabin_light(True)
    iot_controller.set_cabin_light(False)
    assert states == [True, False]


def test_scan_light_stays_on_during_movement_and_capture(monkeypatch):
    events = []
    monkeypatch.setattr(
        iot_controller, "set_cabin_light", lambda active: events.append(("light", active))
    )
    monkeypatch.setattr(
        vision_pipeline,
        "home_and_scan_positions",
        lambda: events.append(("move", None)) or [0.0, 63.5, 127.0, 190.5],
    )
    monkeypatch.setattr(
        vision_pipeline,
        "capture_frame",
        lambda position: events.append(("capture", position)) or {"position_mm": position},
    )
    patch_scan_detections(
        monkeypatch,
        lambda frame, object_count=None: [{
            "bbox_x": 1.0,
            "yolo_confidence": 0.9,
            "condition": "good",
        }],
    )

    vision_pipeline.run_scan(batch_id=1)

    assert events == [
        ("light", True),
        ("move", None),
        ("capture", 0.0),
        ("capture", 63.5),
        ("capture", 127.0),
        ("capture", 190.5),
        ("light", False),
    ]


def test_scan_light_turns_off_when_capture_fails(monkeypatch):
    light_states = []
    monkeypatch.setattr(iot_controller, "set_cabin_light", light_states.append)
    monkeypatch.setattr(vision_pipeline, "home_and_scan_positions", lambda: [100])

    def fail_capture(position):
        raise RuntimeError("camera unavailable")

    monkeypatch.setattr(vision_pipeline, "capture_frame", fail_capture)

    with pytest.raises(RuntimeError, match="camera unavailable"):
        vision_pipeline.run_scan(batch_id=1)

    assert light_states == [True, False]


def test_capture_frame_records_partial_travel_when_far_limit_trips(monkeypatch):
    class FakeStepper:
        def step(self, n, forward, delay_s, stop_if=None):
            assert n == 80
            assert forward is hw.HOME_IS_BACKWARD
            return 40

    class FakeCamera:
        def capture_array(self):
            return object()

    limits = SimpleNamespace(
        home=SimpleNamespace(is_pressed=False),
        far=SimpleNamespace(is_pressed=True),
    )
    monkeypatch.setattr(common, "load", lambda: {
        "carriage": {"steps_per_mm": 8, "step_delay_s": 0.001},
    })
    monkeypatch.setattr(hw, "open_camera", FakeCamera)
    monkeypatch.setattr(
        vision_pipeline.home_and_scan_positions, "_stepper", FakeStepper(), raising=False
    )
    monkeypatch.setattr(
        vision_pipeline.home_and_scan_positions, "_limits", limits, raising=False
    )
    monkeypatch.setattr(
        vision_pipeline.capture_frame, "_hardware_position_mm", 0.0, raising=False
    )
    monkeypatch.setattr(vision_pipeline.capture_frame, "_camera", None, raising=False)

    frame = vision_pipeline.capture_frame(10.0)

    assert frame["position_mm"] == 5.0
    assert frame["limit_reached"] is True
    assert frame["image"] is not None


def test_capture_frame_reports_far_limit_at_exact_target(monkeypatch):
    class FakeStepper:
        def step(self, n, forward, delay_s, stop_if=None):
            assert n == 80
            limits.far.is_pressed = True
            return n

    class FakeCamera:
        def capture_array(self):
            return object()

    limits = SimpleNamespace(
        home=SimpleNamespace(is_pressed=False),
        far=SimpleNamespace(is_pressed=False),
    )
    monkeypatch.setattr(common, "load", lambda: {
        "carriage": {"steps_per_mm": 8, "step_delay_s": 0.001},
    })
    monkeypatch.setattr(hw, "open_camera", FakeCamera)
    monkeypatch.setattr(
        vision_pipeline.home_and_scan_positions, "_stepper", FakeStepper(), raising=False
    )
    monkeypatch.setattr(
        vision_pipeline.home_and_scan_positions, "_limits", limits, raising=False
    )
    monkeypatch.setattr(
        vision_pipeline.capture_frame, "_hardware_position_mm", 0.0, raising=False
    )
    monkeypatch.setattr(vision_pipeline.capture_frame, "_camera", None, raising=False)

    frame = vision_pipeline.capture_frame(10.0)

    assert frame["position_mm"] == 10.0
    assert frame["limit_reached"] is True


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
    assert all("mode" in event for event in data["events"])


def test_automation_history_can_include_older_manila_records(client):
    db = SessionLocal()
    batch = db.query(StorageBatch).filter(StorageBatch.ended_at.is_(None)).first()
    db.add(
        SensorReading(
            batch_id=batch.id,
            timestamp=datetime.utcnow() - timedelta(days=5),
            temperature_c=11.8,
            humidity_pct=92.0,
            control_mode="MANUAL_OVERRIDE",
            peltier_pwm_pct=0.0,
            mister_active=1,
            correction_note="Mister activated",
        )
    )
    db.commit()
    db.close()

    response = client.get("/api/automation/history?days=10&limit=300")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
    assert len(data["events"]) == 1
    assert data["events"][0]["mode"] == "MANUAL_OVERRIDE"
    assert data["events"][0]["note"] == "Mister activated"


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
    monkeypatch.delenv("CUKEGUARD_SMS_PROVIDER", raising=False)
    active_before = client.get("/api/batch/active").json()
    batch_response = client.post(
        "/api/batch",
        json={"initial_quantity": 4},
    )
    assert batch_response.status_code == 200
    new_batch = batch_response.json()
    assert new_batch["id"] > active_before["id"]
    assert new_batch["batch_code"] == f"BATCH-{new_batch['id']:04d}"
    assert new_batch["initial_quantity"] == 4
    assert active_before["owner_phone"] == OWNER_PHONE
    assert client.get("/api/batch/active").json()["owner_phone"] == OWNER_PHONE
    assert client.get("/api/scan/latest").status_code == 404

    scan_call = {}

    def fixed_scan(batch_id, expected_count=None, on_capture=None):
        scan_call.update(batch_id=batch_id, expected_count=expected_count)
        samples = [
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
                "condition": "good",
                "est_shelf_life_days": 14.0,
                "track_position_mm": 300,
                "cucumber_number": 3,
            },
        ]
        if on_capture:
            for sample in samples:
                on_capture(sample)
        return samples, {"good": 2, "bad": 1}

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
    assert scan_call == {"batch_id": new_batch["id"], "expected_count": 4}

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
    assert all(
        isinstance(sample["est_shelf_life_days"], float)
        for sample in scan["samples"]
    )
    assert scan["counts"] == {"good": 2, "bad": 1}
    assert [sample["cucumber_number"] for sample in scan["samples"]] == [1, 2, 3]

    alert_response = client.get("/api/alerts/history?limit=20")
    assert alert_response.status_code == 200
    alert = next(
        row for row in alert_response.json()
        if f"Scan S-{scan['scan_session_id']:04d}:" in row["message"]
    )
    alert_message = alert["message"]
    assert alert["delivery_status"] == "failed: provider unconfigured"
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


def test_manual_scan_returns_detector_failure_to_dashboard(client, monkeypatch):
    def fail_scan(*args, **kwargs):
        raise RuntimeError("YOLO detected only 2 of 4 cucumbers")

    monkeypatch.setattr(vision_pipeline, "run_scan", fail_scan)

    response = client.post("/api/scan/manual")

    assert response.status_code == 422
    assert response.json()["detail"] == "YOLO detected only 2 of 4 cucumbers"
