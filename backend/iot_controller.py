"""Hardware climate control loop for the Raspberry Pi."""
import os
import time
from datetime import datetime

from .database import SessionLocal, SensorReading, StorageBatch

TARGET_TEMP_RANGE = (10.0, 12.5)
TARGET_HUMIDITY_RANGE = (90.0, 95.0)
DATA_MODE = os.getenv("CUKEGUARD_DATA_MODE", "hardware").strip().lower()
if DATA_MODE != "hardware":
    raise RuntimeError("Simulation mode is disabled; set CUKEGUARD_DATA_MODE=hardware")

_actuator_state = {
    "peltier_pwm": 0.0,
    "mister": False,
    "cabin_light": False,
}


def read_temperature_humidity():
    """Read real SHT41/DS18B20 sensors only in hardware mode."""
    try:
        from calibration import common, hw
        sensors = getattr(read_temperature_humidity, "_sensors", None)
        if sensors is None:
            sensors = hw.Sensors()
            read_temperature_humidity._sensors = sensors
        cal = common.load()
        return round(sensors.mean_temp(cal), 2), round(sensors.mean_rh(cal), 2)
    except Exception as exc:
        raise RuntimeError(f"Hardware sensor read failed: {exc}") from exc


def apply_peltier_pwm(pwm_pct: float):
    """Peltier PWM remains blocked until its control pin is assigned."""
    raise RuntimeError(
        "Peltier/heatsink control is not assigned by the supplied wiring PDF"
    )


def set_mister(active: bool):
    """Press the mist-board button on GPIO21."""
    if active:
        from gpiozero import DigitalOutputDevice
        mist = DigitalOutputDevice(21, initial_value=False)
        try:
            mist.on()
            time.sleep(0.5)
            mist.off()
        finally:
            mist.close()
        _actuator_state["mister"] = True
    else:
        _actuator_state["mister"] = False


def set_cabin_light(active: bool):
    """Drive the WS2812B strip through rpi_ws281x when available."""
    try:
        from rpi_ws281x import Adafruit_NeoPixel, Color
    except ImportError as exc:
        raise RuntimeError(
            "WS2812B control requires the rpi_ws281x package and sudo/root"
        ) from exc

    pixel_count = int(os.getenv("CUKEGUARD_LED_PIXELS", "1"))
    brightness = int(os.getenv("CUKEGUARD_LED_BRIGHTNESS", "64"))
    strip = Adafruit_NeoPixel(pixel_count, 18, 800000, 5, False, brightness, 0)
    strip.begin()
    if active:
        for index in range(pixel_count):
            strip.setPixelColor(index, Color(brightness, brightness, brightness))
    else:
        for index in range(pixel_count):
            strip.setPixelColor(index, Color(0, 0, 0))
    strip.show()
    _actuator_state["cabin_light"] = active


def plan_corrections(temp: float, humidity: float) -> tuple[float, bool, list[str]]:
    """Plan actions using the installed cooling and misting capabilities."""
    temp_min, temp_max = TARGET_TEMP_RANGE
    humidity_min, humidity_max = TARGET_HUMIDITY_RANGE
    notes = []

    peltier_pwm = 0.0
    if temp > temp_max:
        peltier_pwm = min(100.0, (temp - temp_max) * 40)
        notes.append(
            f"Temperature high; cooler activated (cooling at {peltier_pwm:.0f}%)"
        )
    elif temp < temp_min:
        notes.append("Temperature low; heating hardware required")

    mister_active = humidity < humidity_min
    if mister_active:
        notes.append("Humidity low; humidifier boosted (mister on)")
    elif humidity > humidity_max:
        notes.append("Humidity high; dehumidification hardware required")

    return peltier_pwm, mister_active, notes


def control_tick(batch: StorageBatch, db: SessionLocal) -> SensorReading:
    """One iteration of the closed-loop controller. Called on a timer."""
    temp, humidity = read_temperature_humidity()
    pwm, mister_active, note_parts = plan_corrections(temp, humidity)
    if DATA_MODE == "hardware":
        if pwm > 0:
            note_parts.append("Peltier PWM is not configured by the wiring PDF")
            pwm = 0.0
    else:
        apply_peltier_pwm(pwm)
    set_mister(mister_active)

    reading = SensorReading(
        batch_id=batch.id,
        timestamp=datetime.utcnow(),
        temperature_c=temp,
        humidity_pct=humidity,
        peltier_pwm_pct=_actuator_state["peltier_pwm"],
        mister_active=int(_actuator_state["mister"]),
        correction_note="; ".join(note_parts) if note_parts else None,
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    return reading
