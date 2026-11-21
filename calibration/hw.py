"""
hw.py - pin map and device wrappers used by the calibration scripts.

EDIT THE CONSTANTS BELOW to match your real wiring before running anything.
Hardware libraries are imported inside the classes so the analysis code can be
tested on a laptop.

Install on the Pi (inside your venv):
    pip install gpiozero adafruit-circuitpython-sht31d adafruit-circuitpython-tca9548a \
                w1thermsensor adafruit-circuitpython-motorkit
    (picamera2 comes from apt: sudo apt install python3-picamera2)
"""
import signal
import statistics
import time
from contextlib import contextmanager

from . import common

# ---------- EDIT: pins (BCM numbers) ----------
PIN_PELTIER_PWM = 18      # MOSFET gate signal
PIN_HEATSINK_FANS = 23    # hot-side fans (on/off)
PIN_MIST_RELAY = 24       # relay driving the 24 V mist maker
PIN_LED = 25              # LED strip switch
PIN_LIMIT_HOME = 5        # limit switch to GND, internal pull-up
PIN_LIMIT_FAR = 6

RELAY_ACTIVE_HIGH = True  # set False if your relay board switches on a LOW input
PELTIER_PWM_HZ = 1000
FAN_OVERRUN_S = 45        # fans keep running this long after the Peltier stops

# ---------- EDIT: sensors ----------
SHT31_MUX_CHANNELS = []   # [] = SHT31-D directly on the I2C bus; [0, 1] = behind TCA9548A channels
SHT31_ADDRESSES = [0x44]  # used only when there is no mux (0x45 if the ADDR pin is pulled high)

# ---------- EDIT: stepper ----------
# Set these to BCM GPIO numbers before running any stepper calibration.
# The physical GPIO input level may differ from the logical enable state:
# DRV8825 nENABLE is active-low, so ENABLE_ACTIVE_LOW should normally be True.
PIN_STEPPER_STEP = None
PIN_STEPPER_DIR = None
PIN_STEPPER_ENABLE = None
PIN_STEPPER_SLEEP = None
PIN_STEPPER_RESET = None

ENABLE_ACTIVE_LOW = True
DIR_FORWARD_HIGH = True
STEP_STYLE = "MICROSTEP_1_16"
HOME_IS_BACKWARD = True

# ---------- hard safety limits enforced by the scripts ----------
ABS_MIN_TEMP_C = 8.0      # cooling is cut below this (cucumbers suffer chilling injury near/below 10 C)
ABS_MAX_RH = 98.5         # misting is cut above this (condensation / wet cucumbers)


class Sensors:
    """SHT31-D (one or more) and the DS18B20 probe."""

    def __init__(self):
        import board
        import busio
        import adafruit_sht31d
        i2c = busio.I2C(board.SCL, board.SDA)
        self._sht = []
        if SHT31_MUX_CHANNELS:
            import adafruit_tca9548a
            mux = adafruit_tca9548a.TCA9548A(i2c)
            for ch in SHT31_MUX_CHANNELS:
                self._sht.append(adafruit_sht31d.SHT31D(mux[ch]))
        else:
            for addr in SHT31_ADDRESSES:
                self._sht.append(adafruit_sht31d.SHT31D(i2c, address=addr))
        try:
            from w1thermsensor import W1ThermSensor
            self.ds = W1ThermSensor()
        except Exception:
            self.ds = None

    @property
    def sht_count(self) -> int:
        return len(self._sht)

    def sht_temp(self, i: int) -> float:
        return float(self._sht[i].temperature)

    def sht_rh(self, i: int) -> float:
        return float(self._sht[i].relative_humidity)

    def ds_temp(self) -> float:
        return float(self.ds.get_temperature())

    def mean_temp(self, cal=None) -> float:
        """Average of all temperature sensors, with calibration applied if given."""
        vals = [common.apply_sensor_cal(cal, f"sht31_{i}_temp", self.sht_temp(i)) for i in range(self.sht_count)]
        if self.ds is not None:
            vals.append(common.apply_sensor_cal(cal, "ds18b20_temp", self.ds_temp()))
        return statistics.fmean(vals)

    def mean_rh(self, cal=None) -> float:
        vals = [common.apply_sensor_cal(cal, f"sht31_{i}_rh", self.sht_rh(i)) for i in range(self.sht_count)]
        return statistics.fmean(vals)


class Outputs:
    """Peltier (via MOSFET), heatsink fans, mist relay and LED strip."""

    def __init__(self):
        from gpiozero import OutputDevice, PWMOutputDevice
        self._peltier = PWMOutputDevice(PIN_PELTIER_PWM, frequency=PELTIER_PWM_HZ, initial_value=0)
        self._fans = OutputDevice(PIN_HEATSINK_FANS, initial_value=False)
        self._mist = OutputDevice(PIN_MIST_RELAY, active_high=RELAY_ACTIVE_HIGH, initial_value=False)
        self._led = OutputDevice(PIN_LED, initial_value=False)
        self._peltier_used = False

    def peltier(self, duty_pct: float) -> None:
        duty = max(0.0, min(100.0, duty_pct))
        if duty > 0:
            self._fans.on()                # fans first: never run a Peltier without hot-side cooling
            self._peltier.value = duty / 100.0
            self._peltier_used = True
        else:
            self._peltier.value = 0

    def mist(self, on: bool) -> None:
        self._mist.on() if on else self._mist.off()

    def led(self, on: bool) -> None:
        self._led.on() if on else self._led.off()

    def shutdown(self) -> None:
        self._peltier.value = 0
        self._mist.off()
        self._led.off()
        if self._peltier_used:
            print(f"Peltier off. Keeping heatsink fans on for {FAN_OVERRUN_S} s...")
            time.sleep(FAN_OVERRUN_S)
        self._fans.off()
        for dev in (self._peltier, self._fans, self._mist, self._led):
            dev.close()


@contextmanager
def outputs():
    """Yields Outputs and guarantees everything is switched off, even on Ctrl+C or SIGTERM."""
    def _term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    out = Outputs()
    try:
        yield out
    finally:
        out.shutdown()


class Stepper:
    """GPIO STEP/DIR/ENABLE adapter for a DRV8825."""

    def __init__(self):
        try:
            from gpiozero import DigitalOutputDevice
        except Exception as exc:
            raise RuntimeError("gpiozero is not installed on the Raspberry Pi") from exc

        missing = [
            name
            for name, pin in (
                ("PIN_STEPPER_STEP", PIN_STEPPER_STEP),
                ("PIN_STEPPER_DIR", PIN_STEPPER_DIR),
                ("PIN_STEPPER_ENABLE", PIN_STEPPER_ENABLE),
            )
            if pin is None
        ]
        if missing:
            raise RuntimeError(
                "DRV8825 GPIO pins are not configured. Set "
                + ", ".join(missing)
                + " in calibration/hw.py"
            )

        self._step = DigitalOutputDevice(PIN_STEPPER_STEP, initial_value=False)
        self._dir = DigitalOutputDevice(PIN_STEPPER_DIR, initial_value=False)
        self._enable = DigitalOutputDevice(
            PIN_STEPPER_ENABLE,
            active_high=not ENABLE_ACTIVE_LOW,
            initial_value=False,
        )

        self._sleep = None
        if PIN_STEPPER_SLEEP is not None:
            self._sleep = DigitalOutputDevice(PIN_STEPPER_SLEEP, initial_value=False)
        self._reset = None
        if PIN_STEPPER_RESET is not None:
            self._reset = DigitalOutputDevice(PIN_STEPPER_RESET, initial_value=False)

        if self._sleep is not None:
            self._sleep.on()
        if self._reset is not None:
            self._reset.on()
        self._enable.off()

    def step(self, n: int, forward: bool = True, delay_s: float = 0.005, stop_if=None) -> int:
        """Take up to n steps; stop early if stop_if() is true. Returns steps taken."""
        if n < 0:
            raise ValueError("n must be zero or greater")
        if delay_s < 0:
            raise ValueError("delay_s must be zero or greater")

        direction_high = forward == DIR_FORWARD_HIGH
        if direction_high:
            self._dir.on()
        else:
            self._dir.off()
        self.enable()

        taken = 0
        for _ in range(n):
            if stop_if is not None and stop_if():
                break
            self._step.on()
            time.sleep(max(delay_s / 2.0, 0.000002))
            self._step.off()
            time.sleep(max(delay_s / 2.0, 0.000002))
            taken += 1
        return taken

    def enable(self) -> None:
        self._enable.on()

    def release(self) -> None:
        self._enable.off()
        if self._sleep is not None:
            self._sleep.off()
        for device in (self._enable, self._dir, self._step):
            device.close()
        if self._sleep is not None:
            self._sleep.close()
        if self._reset is not None:
            self._reset.close()


class Limits:
    """The two limit switches (normally open, wired to GND)."""

    def __init__(self):
        from gpiozero import Button
        self.home = Button(PIN_LIMIT_HOME, pull_up=True)
        self.far = Button(PIN_LIMIT_FAR, pull_up=True)


def open_camera(size=(1280, 720)):
    """Start the Pi camera. picamera2's 'BGR888' format gives arrays in R,G,B order
    (the naming is confusing, see README); run calibrate_camera --check-channels to confirm."""
    from picamera2 import Picamera2
    cam = Picamera2()
    cam.configure(cam.create_still_configuration(main={"size": size, "format": "BGR888"}))
    cam.start()
    return cam
