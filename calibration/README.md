# CukeGuard calibration scripts

Seven scripts, one per thing that needs calibrating. Each writes its results to
`calibration/calibration.json`, which the backend loads at startup.

**Status:** the maths (fits, pulse and cooling analysis, settle choice, threshold
search) is covered by `pytest calibration/` and passes. The hardware calls
(`hw.py`, and the scripts that use it) have NOT been run on a real Pi. Expect to
fix pin numbers and small driver details on first contact. Run each script
with the load disconnected or the chamber empty the first time.

**Camera note:** scan capture uses a USB/UVC webcam at `/dev/video0`.
`calibrate_camera` checks USB image quality and RGB channel order; it does not
apply device-specific controls. Set focus/exposure with controls supported by
the webcam, then run the calibration diagnostic.

## Setup

Copy the `calibration/` folder into the repo root, then edit the constants at the
top of `calibration/hw.py` (pins, relay polarity, mux channels, stepper style).

```bash
pip install gpiozero adafruit-blinka adafruit-circuitpython-sht4x \
            adafruit-circuitpython-tca9548a w1thermsensor numpy opencv-python \
            scikit-image pyserial
```
Run everything from the repo root as modules, e.g. `python -m calibration.calibrate_sensors ...`.

## Order

| # | Script | What it fixes | You need |
|---|---|---|---|
| 1 | `calibrate_sensors` | SHT41 and DS18B20 offset and gain | A trusted reference thermometer/hygrometer; ice water; salt jars for RH |
| 2 | Camera setup | Set USB webcam focus/exposure manually; use `calibrate_camera` to check RGB order and image consistency | Grey card, red and green references |
| 3 | `calibrate_stepper` | Limit switches, steps per mm, slot positions | Ruler along the track |
| 4 | `calibrate_settle` | Wait time after each move before capturing | Printed text target or a cucumber |
| 5 | `calibrate_cooling` | Peltier response; blocked until its output is wired | Do not run until control hardware is configured |
| 6 | `calibrate_mist` | Legacy relay timing; incompatible with the 0.5 s button adapter | Do not run until adapted to the mist board |
| 7 | `calibrate_thresholds` | Hue range and LBP cutoff from your own labeled photos | Photos taken AFTER step 2 |

Do 1 before any climate calibration. Set and verify camera focus/exposure
before taking the photos used in step 7, because hue depends on lighting.

## Safety

- Peltier/fan control is not configured in the supplied pin map; do not run cooling calibration.
- Do not put an SHT41 board in water. Only the waterproof DS18B20 goes in an ice bath.
- Every script switches all outputs off on exit, Ctrl+C and SIGTERM.
- Keep cucumbers out of the chamber during cooling and mist tests.

## Apply the results in the backend

```python
from calibration import common
CAL = common.load()

# sensors: correct every raw reading
temp = common.apply_sensor_cal(CAL, "sht41_0_temp", raw_temp)
rh   = common.apply_sensor_cal(CAL, "sht41_0_rh", raw_rh)

# carriage
STEPS_PER_MM = CAL["carriage"]["steps_per_mm"]; SETTLE_S = CAL["carriage"].get("settle_s", 1.0)
SLOT_STEPS = CAL["carriage"]["positions_steps"]

# classifier thresholds (used by the feature-extraction module)
t = CAL.get("thresholds")
if t:
    HUE_PASS_MIN, HUE_PASS_MAX, LBP_VARIANCE_PASS_MAX = t["hue_min"], t["hue_max"], t["lbp_max"]
```

## Things to know

- **Colour channel order.** The USB webcam adapter converts OpenCV BGR frames to RGB.
  Verify with a red object before collecting threshold calibration photos.
- **DRV8825 stepper.** GPIO17=STEP, GPIO27=DIR and GPIO22=ENABLE. Verify direction,
  active-low enable, microstep pins, current limit, homing, and travel with the
  motor supply disconnected before powered tests.
- **Reference accuracy.** A calibration cannot be better than its reference. Record the reference
  instrument and its stated accuracy for your thesis.
- **RH calibration** by salt jars needs hours to equilibrate; wait, then run with `--settle 0`.
- **Thresholds.** The search uses half your photos and reports on the other half. With few images
  the result is unstable; confirm it on a new, independent, expert-verified set before relying on it.
- **Mist test.** The current mist adapter emulates a 0.5-second button press, so
  `calibrate_mist`'s variable-duration relay test is incompatible and must not be used.
