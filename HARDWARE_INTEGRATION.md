# CukeGuard Hardware Integration Guide

This guide maps the planned Raspberry Pi equipment to the current software.
The app is still in simulation mode. No GPIO pins, stepper HAT driver, or
electrical ratings are inferred from product names.

## Current software hooks

| Hardware | Existing integration point | Work needed |
|---|---|---|
| Raspberry Pi Camera Module | `backend/vision_pipeline.py:capture_frame()` | Use the Pi camera library supported by the installed Pi OS (commonly `picamera2`); return an RGB frame and report capture errors. |
| MobileNetV2 `.keras` model | `backend/model_classifier.py:predict_image()` | Use only on one isolated cucumber crop per indexed motor position. Verify class mapping with independent good/bad samples before enabling owner alerts. |
| NEMA17 + Raspberry Pi Stepper Motor HAT | `backend/vision_pipeline.py:home_and_scan_positions()` | Identify the exact HAT/driver and vendor Python API first. Implement home, bounded movement, speed/current settings, and stop/cleanup using that API. |
| Two lever limit switches | New motor adapter called from the scan-position hook | Confirm normally-open/closed terminals, voltage compatibility, pull-up/down requirements, polarity, debounce, and fail-safe stop behavior before assigning Pi pins. |
| SHT31-D | `backend/iot_controller.py:read_temperature_humidity()` | Read the device using its supported I2C library. |
| DS18B20 | `backend/iot_controller.py:read_temperature_humidity()` | Enable and test the Pi's 1-Wire interface; read the probe and handle missing/invalid values explicitly. |
| TCA9548A | Sensor initialization, only if needed | Confirm I2C address, channel, and actual wiring. Do not add a multiplexer when sensors can safely share the bus. |
| TEC1-12706, CPU heatsinks/fans, MOSFET module | `backend/iot_controller.py:apply_peltier_pwm()` | Verify polarity, current, driver ratings, heatsink capacity, airflow, and temperature feedback. A TEC is a high-current load; never power it from Pi GPIO. |
| 24V mist maker + opto-isolated relay | `backend/iot_controller.py:set_mister()` | Verify relay input compatibility, load current, isolation, supply, water handling, and safe location away from exposed electronics. |
| 12V/24V supplies + 5V buck | Power design, not Python | Check each device's voltage/current requirements, common-ground/isolation requirements, supply headroom, fusing, and wire gauge against its datasheet. |
| Waterproof LED strip | Optional lighting adapter | Verify strip voltage/current, switching method, moisture protection, and cable routing. |

## Indexed scan behavior

The intended scan is one cucumber per indexed stop:

1. Home the carriage against its home limit switch at low speed. Stop
   immediately if the switch state is inconsistent or motion times out.
2. Move the calibrated number of steps to position #1; stop and allow the
   carriage/camera to settle.
3. Capture a frame, crop the single cucumber, convert it to RGB, and classify
   the crop with the verified MobileNetV2 `.keras` model.
4. Save that numbered sample, refresh the latest-scan map, and alert immediately
   for a bad result (rather than waiting for the full shelf scan).
5. Repeat by one calibrated position at a time. Stop at the far-end switch.
   Record the number of positions/cucumbers visited as the scan count.

Current `run_scan()` and `_execute_scan()` are simulated and batch bad-cucumber
alerts after a completed scan. Real per-cucumber alerting requires changing the
scan execution path to persist/classify each captured stop and notify as soon as
that stop returns a bad result. Do not enable both a simulated scanner and a
physical motor driver at once.

## Safe bring-up sequence

1. Record the Raspberry Pi model/OS, exact motor HAT make/model and driver chip,
   NEMA17 rated current/phase resistance, limit-switch wiring diagram, and the
   ratings printed on each power/driver module.
2. Review the HAT manufacturer's wiring and software documentation. Confirm
   motor current limiting and the approved Pi interface. The motor coils must
   connect to the driver output, never directly to Raspberry Pi GPIO.
3. With the motor/load power disconnected, verify limit-switch logic and sensor
   readings. Test each driver output with the load disconnected where the
   module's documentation permits it.
4. Mechanically secure the carriage, keep fingers clear of the belt/pulleys,
   test low-speed homing with an emergency power cut available, and confirm
   both end stops reliably stop motion. Define a timeout and travel bounds.
5. Test camera focus, lighting, cucumber framing, crop size, and position
   calibration. Validate the classifier using independent real camera crops,
   especially bad-condition recall, before sending real alerts.
6. Test the TEC/fans and mister one at a time with measured sensors and a safe
   thermal/moisture setup. Ensure condensation or spray cannot reach the Pi,
   connectors, supplies, or exposed circuits.
7. Only after hardware checks, implement explicit hardware mode and require
   configuration validation at startup. Keep simulation as the default.

## Enabling real integrations

The current simulation hooks intentionally remain the defaults. Once the exact
boards and pin/interface details are known:

- Add a platform-specific motor/camera/sensor adapter behind these existing
  functions rather than putting GPIO code in the API routes.
- Select hardware mode explicitly in configuration; fail startup on missing
  settings instead of silently falling back to simulation.
- Keep GPIO cleanup in `finally`/shutdown handlers. Stop motor outputs on
  exceptions, a triggered limit switch, or an operation timeout.
- Log hardware faults visibly, and do not mark an actuator as active merely
  because a command was issued; verify feedback where available.
- Install hardware-only packages on the Raspberry Pi environment, not the
  Windows development machine.

Do not use this guide as a wiring diagram. Verify all electrical connections
with the manufacturer's documentation and a qualified person before powering
the assembled system.
