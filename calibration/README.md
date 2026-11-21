# CukeGuard calibration scripts

Seven scripts, one per thing that needs calibrating. Each writes its results to
`calibration/calibration.json`, which the backend loads at startup.

**Status:** the maths (fits, pulse and cooling analysis, settle choice, threshold
search) is covered by `pytest calibration/` and passes. The hardware calls
(`hw.py`, and the scripts that use it) have NOT been run on a real Pi. Expect to
fix pin numbers and small driver details on first contact. Run each script
with the load disconnected or the chamber empty the first time.

**Camera note:** the planned system uses a USB/UVC webcam connected to the
Raspberry Pi. Webcam capture and webcam-specific calibration are not implemented
yet. The current `hw.open_camera()` and `calibrate_camera` code still target
`picamera2`; do not use them for the planned webcam until that adapter is
replaced and tested.

## Setup

Copy the `calibration/` folder into the repo root, then edit the constants at the
top of `calibration/hw.py` (pins, relay polarity, mux channels, stepper style).

```bash
pip install gpiozero adafruit-circuitpython-sht31d adafruit-circuitpython-tca9548a \
            w1thermsensor adafruit-circuitpython-motorkit numpy opencv-python scikit-image
# Only for the legacy Pi-camera calibration code: install picamera2 from apt.
```
Run everything from the repo root as modules, e.g. `python -m calibration.calibrate_sensors ...`.

## Order

| # | Script | What it fixes | You need |
|---|---|---|---|
| 1 | `calibrate_sensors` | SHT31-D and DS18B20 offset and gain | A trusted reference thermometer/hygrometer; ice water; salt jars for RH |
| 2 | `calibrate_camera` | Legacy Pi-camera calibration only; webcam calibration is pending | Grey card, green reference |
| 3 | `calibrate_stepper` | Limit switches, steps per mm, slot positions | Ruler along the track |
| 4 | `calibrate_settle` | Wait time after each move before capturing | Printed text target or a cucumber |
| 5 | `calibrate_cooling` | Peltier lag, cooling rate, coasting, warm-up | Empty chamber |
| 6 | `calibrate_mist` | Mist pulse length and minimum pause | Empty chamber, water in the mister |
| 7 | `calibrate_thresholds` | Hue range and LBP cutoff from your own labeled photos | Photos taken AFTER step 2 |

Do 1 before 5 and 6 (they use the corrected sensor values). Do 2 before taking
the photos used in step 7, because hue depends on white balance and lighting.

## Safety

- Hot-side fans start before the Peltier and run on after it stops (`FAN_OVERRUN_S`).
- Cooling is cut below 10 C (never below 8 C); misting is cut above 98.5 % RH.
- Every script switches all outputs off on exit, Ctrl+C and SIGTERM.
- Do not put the SHT31-D board in water. Only the waterproof DS18B20 goes in an ice bath.
- Keep cucumbers out of the chamber during cooling and mist tests.

## Apply the results in the backend

```python
from calibration import common
CAL = common.load()

# sensors: correct every raw reading
temp = common.apply_sensor_cal(CAL, "sht31_0_temp", raw_temp)
rh   = common.apply_sensor_cal(CAL, "sht31_0_rh", raw_rh)

# camera: lock white balance / exposure / focus at start-up (BGR888 format, see below)
picam2.set_controls(common.camera_controls_from(CAL))

# mist: pulse, then wait before the next decision
MIST_PULSE_S = CAL.get("mist", {}).get("pulse_on_s", 3)
MIST_MIN_OFF_S = CAL.get("mist", {}).get("min_off_s", 60)

# cooling: switch the Peltier off early enough that coasting stays above 10 C
PELTIER_OFF_AT = 10.0 + CAL.get("cooling", {}).get("off_margin_above_min_c", 0.5)

# carriage
STEPS_PER_MM = CAL["carriage"]["steps_per_mm"]; SETTLE_S = CAL["carriage"].get("settle_s", 1.0)
SLOT_STEPS = CAL["carriage"]["positions_steps"]

# classifier thresholds (replace the constants in backend/feature_extraction.py)
t = CAL.get("thresholds")
if t:
    HUE_PASS_MIN, HUE_PASS_MAX, LBP_VARIANCE_PASS_MAX = t["hue_min"], t["hue_max"], t["lbp_max"]
```

## Things to know

- **Colour channel order.** picamera2's `"BGR888"` format gives arrays in R,G,B order (the name is confusing).
  A swapped channel order changes hue completely. Run `calibrate_camera --check-channels`
  with a red object first. If you used the earlier `hardware/camera.py` that did not set a format,
  check it the same way.
- **DRV8825 stepper.** `hw.Stepper` uses GPIO STEP/DIR/ENABLE control for a DRV8825.
  set `PIN_STEPPER_STEP`, `PIN_STEPPER_DIR`, `PIN_STEPPER_ENABLE`, `ENABLE_ACTIVE_LOW`,
  `DIR_FORWARD_HIGH`, and `STEP_STYLE` in `calibration/hw.py`. The installed DRV8825 adapter
  deliberately refuses to run until these GPIO pins are assigned.
- **Reference accuracy.** A calibration cannot be better than its reference. Record the reference
  instrument and its stated accuracy for your thesis.
- **RH calibration** by salt jars needs hours to equilibrate; wait, then run with `--settle 0`.
- **Thresholds.** The search uses half your photos and reports on the other half. With few images
  the result is unstable; confirm it on a new, independent, expert-verified set before relying on it.
- **Mist test.** Your chamber cannot dehumidify, so the script asks you to vent below 88 % RH between pulses.
