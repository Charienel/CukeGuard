"""
iot_controller.py — climate control loop.

Right now this SIMULATES the SHT31-D + DS18B20 sensors and the
Dual-Peltier / ultrasonic mister actuators, so the whole system runs
without the physical rig attached.

TO GO LIVE ON THE PI:
  - Replace `read_temperature_humidity()` with real SHT31-D I2C reads
    (e.g. via `adafruit_sht31d`) and DS18B20 1-Wire reads.
  - Replace `apply_peltier_pwm()` / `set_mister()` with RPi.GPIO / gpiozero
    calls to the MOSFET driving the Peltier array and the relay driving
    the ultrasonic mister.
  - Everything else (the control loop, DB writes, API shape) stays the same.
"""
import random
import time
from datetime import datetime

from .database import SessionLocal, SensorReading, StorageBatch

TARGET_TEMP_RANGE = (10.0, 12.5)
TARGET_HUMIDITY_RANGE = (90.0, 95.0)
DATA_MODE = "simulation"

_sim_state = {"temp": 11.5, "humidity": 92.5, "peltier_pwm": 0.0, "mister": False}


def read_temperature_humidity():
    """SIMULATED sensor read. Replace with real SHT31-D / DS18B20 calls."""
    temp = _sim_state["temp"] + random.uniform(-0.08, 0.08)
    humidity = _sim_state["humidity"] + random.uniform(-0.2, 0.2)
    return round(temp, 2), round(humidity, 2)


def apply_peltier_pwm(pwm_pct: float):
    """SIMULATED actuator write. Replace with GPIO PWM to the Peltier MOSFET."""
    _sim_state["peltier_pwm"] = pwm_pct
    # cooling effect proportional to pwm
    _sim_state["temp"] -= (pwm_pct / 100.0) * 0.25


def set_mister(active: bool):
    """SIMULATED actuator write. Replace with GPIO relay control."""
    _sim_state["mister"] = active
    if active:
        _sim_state["humidity"] += 1.2


def plan_corrections(temp: float, humidity: float) -> tuple[float, bool, list[str]]:
    """Plan actions using the installed cooling and misting capabilities."""
    temp_min, temp_max = TARGET_TEMP_RANGE
    humidity_min, humidity_max = TARGET_HUMIDITY_RANGE
    notes = []

    peltier_pwm = 0.0
    if temp > temp_max:
        peltier_pwm = min(100.0, (temp - temp_max) * 40)
        notes.append(f"Temperature high; cooling at {peltier_pwm:.0f}%")
    elif temp < temp_min:
        notes.append("Temperature low; heating hardware required")

    mister_active = humidity < humidity_min
    if mister_active:
        notes.append("Humidity low; mister on")
    elif humidity > humidity_max:
        notes.append("Humidity high; dehumidification hardware required")

    return peltier_pwm, mister_active, notes


def control_tick(batch: StorageBatch, db: SessionLocal) -> SensorReading:
    """One iteration of the closed-loop controller. Called on a timer."""
    temp, humidity = read_temperature_humidity()
    pwm, mister_active, note_parts = plan_corrections(temp, humidity)
    apply_peltier_pwm(pwm)
    set_mister(mister_active)

    reading = SensorReading(
        batch_id=batch.id,
        timestamp=datetime.utcnow(),
        temperature_c=temp,
        humidity_pct=humidity,
        peltier_pwm_pct=_sim_state["peltier_pwm"],
        mister_active=int(_sim_state["mister"]),
        correction_note="; ".join(note_parts) if note_parts else None,
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    return reading
