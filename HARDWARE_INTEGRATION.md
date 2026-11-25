# CukeGuard Hardware Integration Guide

This guide covers integrating the CukeGuard hardware on a Raspberry Pi.
The application is configured for hardware mode; scans and climate readings
require the Pi and the physical devices to be connected and calibrated.

The stepper uses a plain DRV8825-style GPIO interface rather than an assumed
Raspberry Pi Stepper Motor HAT; exact expansion-board protection and current
limiting must still be verified from the board manufacturer's documentation.

## Current software hooks

| Hardware | Existing integration point | Work needed |
|---|---|---|
| USB/UVC webcam at `/dev/video0` | `backend/vision_pipeline.py:capture_frame()` | Hardware adapter added with OpenCV. Device 0, RGB conversion and capture errors are implemented; expose/focus and V4L2 controls still need validation. |
| Cucumber detector/classifier | `backend/vision_pipeline.py:detect_and_classify()` | Bundled YOLOv8n weights detect cucumber boxes during the sweep; each box crop is passed to the Keras Good/Bad classifier. The bundled model's `good` mapping is verified on the supplied crops; validate it with camera images before relying on alerts. |
| NEMA17 + DRV8825 stepper driver | `calibration/hw.py:Stepper` | GPIO17 STEP, GPIO27 DIR and GPIO22 ENABLE are assigned. Calibrate microsteps, current, homing and travel before hardware scans. |
| Two lever limit switches | `calibration/hw.py:Limits` | NC switches are assigned to GPIO5 (home) and GPIO6 (far) with GND and internal pull-ups; polarity and fail-safe stops still require testing. |
| SHT41 x3 | `backend/iot_controller.py:read_temperature_humidity()` | Hardware read is implemented through TCA9548A channels 0-2 at address 0x70; sensor calibration remains required. |
| DS18B20 | `backend/iot_controller.py:read_temperature_humidity()` | Enable and test the Pi's 1-Wire interface; read the probe and handle missing/invalid values explicitly. |
| TCA9548A | Sensor initialization | Address 0x70, reset GPIO24, SHT41 on channels 0-2 are configured. |
| TEC1-12706, heatsink/fans | Continuously powered (user-confirmed) | The cooler is not monitored or switched by software. Watch temperature and switch it off at its power source if it falls below 10°C. |
| Mist board button circuit | `backend/iot_controller.py:press_mister_button()` | GPIO21 (physical pin 40), GND physical pin 39, 1 kΩ series base resistor. The board button toggles; software sends one 0.5 s press only when the desired state changes. |
| 12V/24V supplies + 5V buck | Power design, not Python | Check each device's voltage/current requirements, common-ground/isolation requirements, supply headroom, fusing, and wire gauge against its datasheet. |
| Cabin LED strip | `backend/iot_controller.py:set_cabin_light()` called by `backend/vision_pipeline.py:run_scan()` | GPIO18 WS2812B data with 330 Ω series resistor. Requires `rpi_ws281x` and root; not active until tested. |
| SIM800L EVB | `backend/notifier.py:send_sms()` | UART uses Pi GPIO15/RXD (physical pin 10) and GPIO14/TXD (physical pin 8), default `/dev/serial0` at 9600 baud. Confirm EVB UART logic levels, enable the Pi UART, use an active SIM/antenna, and provide adequate power at EVB 5VIN. |

## Sweep behavior

Home/MIN (GPIO5) is the right endpoint. The carriage homes there, advances in
5 mm camera samples toward Far/MAX (GPIO6), with a 254 mm software travel bound.
The Far limit should be reached within that distance; otherwise the scan fails.
Each detected cucumber crop is classified by the Keras model; the best detection
in each of the four 63.5 mm sections along the approximately 254 mm rail is
retained. The carriage then reverses toward Home/MIN and stops at that switch.
If fewer than four cucumbers are found, the scan fails explicitly rather than
inserting placeholder Bad samples. The bundled Keras model uses the verified
`good` positive-class mapping.
The carriage returns home only after Far/MAX was reached. If the Far/MAX limit
is not reached, the scan reports an error and leaves the carriage at its current
position for manual inspection.

The limit inputs are NC, wired COM to GND with the Pi pull-up. The software
inverts the GPIO button state so an open contact means "limit reached"; a broken
wire therefore also stops movement. Run the limit-test procedure and verify the
direction with motor power disconnected before any powered movement.

## Raspberry Pi integration steps

1. **Check the hardware and power.** Record the Pi model/OS, DRV8825 board
   ratings, NEMA17 phase current, switch type, and supply ratings. Disconnect
   motor and actuator power while inspecting wiring. Never connect motor coils,
   Peltier, mist-maker, fan, or LED power loads directly to Pi GPIO.
2. **Enable Pi interfaces.** Run `sudo raspi-config`; enable I2C, 1-Wire, and
   the serial port, and disable the serial login shell while leaving UART
   hardware enabled. Reboot. Add the user to `gpio`, `i2c`, and `dialout` groups
   if running without root, then log out and back in.
3. **Install the application.** From Raspberry Pi OS:

   ```bash
   sudo apt update
   sudo apt install -y python3-venv python3-pip python3-opencv i2c-tools
   git clone https://github.com/Charienel/CukeGuard.git
   cd CukeGuard
   python3 -m venv .venv --system-site-packages
   . .venv/bin/activate
   pip install -r requirements.txt
   pip install gpiozero adafruit-blinka adafruit-circuitpython-sht4x \
     adafruit-circuitpython-tca9548a w1thermsensor scikit-image rpi-ws281x
   pip install -r requirements-model.txt
   cp .env.example .env
   ```

   Set `CUKEGUARD_OWNER_PHONE` to the recipient in international format,
   `CUKEGUARD_SMS_PROVIDER=sim800l`, and
   `CUKEGUARD_SIM800L_PORT=/dev/serial0` in `.env`. Before first startup,
   inspect the physical mister's state and set
   `CUKEGUARD_MISTER_INITIAL_STATE=on` or `off` to match. The board has no
   feedback to discover its state automatically. Repeat this check if the
   mister board is power-cycled independently. The local `.env` is ignored by
   Git; do not commit it.
4. **Wire and verify low-voltage signals.** Follow the GPIO pin table above.
   The SIM800L TXD goes to Pi GPIO15/RXD (physical pin 10); modem RXD goes to
   Pi GPIO14/TXD (physical pin 8); UART GND is common. Power the EVB at its
   documented 5VIN input from a supply capable of modem current bursts. Confirm
   UART logic levels are safe for Pi 3.3 V before connecting. Fit the antenna
   and an active SIM. For the DRV8825, verify STEP/DIR/ENABLE and set its
   microstep pins and current limit according to the driver and motor data.
   Use the wiring diagram and component datasheets; this table is not a power
   wiring plan.
5. **Check devices before motion.** Confirm the I2C mux at `0x70` with
   `i2cdetect -y 1`; confirm the DS18B20 appears under
   `/sys/bus/w1/devices/`; and confirm the webcam with `v4l2-ctl --list-devices`
   (install `v4l-utils` if needed). Test limit switches before connecting motor
   supply:

   ```bash
   python -m calibration.calibrate_stepper --limit-test
   ```

   Check SIM800L UART responses with a serial terminal at 9600 baud; `AT`
   should return `OK`. Do not send test texts to anyone without consent.
6. **Calibrate sensors and stepper.** Use a trusted reference thermometer and
   hygrometer, then run:

   ```bash
   python -m calibration.calibrate_sensors --quantity temp --settle 30 --average 30
   python -m calibration.calibrate_sensors --quantity rh --settle 0 --average 60
   python -m calibration.calibrate_stepper --steps-per-mm --test-steps 2000
   ```

   Keep the motor's travel clear and have an accessible power cut-off. Verify
   the actual home direction, limit response, and steps/mm. The scan stepper
   calibration JSON must contain a valid `steps_per_mm` before scanning.
7. **Calibrate camera and light.** Secure the USB webcam and fixed lighting.
   The current camera adapter captures RGB frames but does not apply the legacy
   Pi-camera white-balance/focus controls. Adjust the webcam's focus/exposure
   using its supported controls, check the RGB channel order with a red object,
   and confirm the detector sees all four cucumbers during a supervised sweep.
   `CUKEGUARD_LED_PIXELS` should match the strip length.
8. **Run the service and verify readings.** The WS2812B driver may need root;
   start in the project directory:

   ```bash
   sudo .venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
   ```

   Open `http://<raspberry-pi-ip>:8000`. Fresh climate readings should appear
   every five seconds. The Peltier remains powered independently of the app:
   compare live readings with the 10°C lower limit and switch it off at its
   power source if the chamber gets too cold. If the dashboard says sensor data
   is stale, check the backend log and sensor wiring; old database rows are not
   live readings.
9. **Supervise a first scan.** With the carriage secured and emergency power
   cut-off within reach, click **Run Scan** once. Confirm light, homing,
   cucumber detections, and return movement before leaving it unattended.
   Scheduled scans run every three hours. SMS delivery requires cellular
   coverage, SMS service/credit, and a working SIM800L; `submitted` means the
   modem accepted the command, not that the recipient confirmed receipt.

**Cooling is not software-controlled:** the Peltier is continuously powered,
but CukeGuard cannot verify or switch it. It can drive the chamber below 10°C;
watch sensor readings and switch it off at its power source if needed. Do not
leave the system unattended unless an independent thermostat or safety cutoff
prevents overcooling.

Do not power the assembled system until electrical connections, polarity,
logic levels, fusing, current limits, and load-driver ratings have been checked
against the manufacturer's documentation by a qualified person.
