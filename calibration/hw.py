"""
hw.py - pin map and device wrappers used by the calibration scripts.

EDIT THE CONSTANTS BELOW to match your real wiring before running anything.
Hardware libraries are imported inside the classes so the analysis code can be
tested on a laptop.

Install on the Pi (inside your venv):
    pip install gpiozero adafruit-circuitpython-sht4x adafruit-circuitpython-tca9548a \
                w1thermsensor opencv-python
"""
import signal
import statistics
import time
from contextlib import contextmanager

from . import common

# ---------- Raspberry Pi 4 GPIO assignments (BCM numbers) ----------
# Do not infer a Peltier or fan control scheme from the 4-relay module:
# the PDF does not state which relay output/coil controls them.
PIN_PELTIER_PWM = None    # Not assigned in the wiring PDF
PIN_HEATSINK_FANS = None  # GPIO12/13 reserved; assignment not confirmed
PIN_MIST_BUTTON = 21      # transistor switch for the mist-board button circuit
PIN_MIST_RELEASE = 21
PIN_LED = 18              # WS2812B data, 330 ohm series resistor
PIN_LIMIT_HOME = 5        # NC limit switch to GND, internal pull-up
PIN_LIMIT_FAR = 6

PIN_RELAY_IN1 = 16
PIN_RELAY_IN2 = 19
PIN_RELAY_IN3 = 20
PIN_RELAY_IN4 = 26
RELAY_ACTIVE_HIGH = False # active-low relay module
PIN_MUX_RST = 24
PELTIER_PWM_HZ = 1000
FAN_OVERRUN_S = 45        # fans keep running this long after the Peltier stops
MIST_PRESS_S = 0.5

# ---------- EDIT: sensors ----------
SHT41_MUX_CHANNELS = [0, 1, 2]  # three SHT41 sensors, one on each used mux channel
SHT41_ADDRESS = 0x44

# ---------- stepper ----------
PIN_STEPPER_STEP = 17
PIN_STEPPER_DIR = 27
PIN_STEPPER_ENABLE = 22
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
    """SHT41 sensors (through TCA9548A) and the DS18B20 probe."""

    def __init__(self):
        import board
        import busio
        i2c = busio.I2C(board.SCL, board.SDA)
        self._sht = []
        if SHT41_MUX_CHANNELS:
            import adafruit_sht4x
            import adafruit_tca9548a
            if PIN_MUX_RST is not None:
                from gpiozero import DigitalOutputDevice
                rst = DigitalOutputDevice(PIN_MUX_RST, initial_value=True)
                rst.off()
                time.sleep(0.01)
                rst.on()
                rst.close()
            mux = adafruit_tca9548a.TCA9548A(i2c, address=0x70)
            for ch in SHT41_MUX_CHANNELS:
                self._sht.append(adafruit_sht4x.SHT4x(mux[ch]))
        else:
            import adafruit_sht4x
            self._sht.append(adafruit_sht4x.SHT4x(i2c))
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
        vals = [common.apply_sensor_cal(cal, f"sht41_{i}_temp", self.sht_temp(i)) for i in range(self.sht_count)]
        if self.ds is not None:
            vals.append(common.apply_sensor_cal(cal, "ds18b20_temp", self.ds_temp()))
        return statistics.fmean(vals)

    def mean_rh(self, cal=None) -> float:
        vals = [common.apply_sensor_cal(cal, f"sht41_{i}_rh", self.sht_rh(i)) for i in range(self.sht_count)]
        return statistics.fmean(vals)


class Outputs:
    """Configured outputs. Relay loads and Peltier PWM are not guessed."""

    def __init__(self):
        from gpiozero import DigitalOutputDevice
        self._mist = DigitalOutputDevice(PIN_MIST_BUTTON, initial_value=False)
        self._peltier_used = False
        self._fans = None
        self._peltier = None

    def peltier(self, duty_pct: float) -> None:
        if PIN_PELTIER_PWM is None:
            raise RuntimeError(
                "Peltier PWM/heatsink control is not assigned in calibration/hw.py "
                "because the wiring PDF does not identify the 4-relay load mapping."
            )

    def mist(self, on: bool) -> None:
        if on:
            self._mist.on()
            time.sleep(MIST_PRESS_S)
            self._mist.off()

    def led(self, on: bool) -> None:
        # WS2812B is a one-wire device, not a gpiozero output. Integration
        # requires the rpi_ws281x library and root; this remains a placeholder.
        if on:
            print("LED requested ON on GPIO18 (WS2812B; use rpi_ws281x adapter)")
        else:
            print("LED requested OFF on GPIO18 (WS2812B; use rpi_ws281x adapter)")

    def shutdown(self) -> None:
        self._mist.off()
        self._mist.close()


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
    """The two NC limit switches, wired COM to GND."""

    def __init__(self):
        self.home = _NCSwitch(PIN_LIMIT_HOME)
        self.far = _NCSwitch(PIN_LIMIT_FAR)

    def close(self):
        self.home.close()
        self.far.close()


class _NCSwitch:
    """Treat an open NC contact as a reached limit; a broken wire is fail-safe."""

    def __init__(self, pin):
        from gpiozero import Button
        self._button = Button(pin, pull_up=True, bounce_time=None)

    @property
    def is_pressed(self) -> bool:
        return not self._button.is_pressed

    def close(self):
        self._button.close()


class UsbCamera:
    """Small capture wrapper for OpenCV's /dev/video0 adapter."""

    def __init__(self, size=(1280, 720), index=0):
        import cv2
        self._cv2 = cv2
        self._cam = cv2.VideoCapture(index)
        if not self._cam.isOpened():
            self._cam.release()
            raise RuntimeError("USB webcam is not available at /dev/video0")
        self._cam.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        self._cam.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])

    def capture_array(self):
        ok, frame = self._cam.read()
        if not ok or frame is None:
            raise RuntimeError("USB webcam frame capture failed")
        return self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)

    def close(self):
        self._cam.release()


def open_camera(size=(1280, 720)):
    """Open the USB/UVC webcam used by CukeGuard."""
    return UsbCamera(size=size, index=0)
