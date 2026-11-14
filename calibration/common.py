"""
common.py - helpers shared by the calibration scripts.

All results are stored in calibration/calibration.json so the backend can load
them once at startup (see README.md for the few lines that apply them).
"""
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

CAL_PATH = Path(__file__).with_name("calibration.json")


# ---------- storage ----------

def load() -> dict:
    if CAL_PATH.exists():
        return json.loads(CAL_PATH.read_text())
    return {}


def update(section: str, values: dict) -> None:
    """Merge `values` into one section of calibration.json (other keys are kept)."""
    cal = load()
    block = cal.get(section, {})
    block.update(values)
    block["calibrated_at"] = datetime.now().isoformat(timespec="seconds")
    cal[section] = block
    CAL_PATH.write_text(json.dumps(cal, indent=2))
    print(f"Saved '{section}' to {CAL_PATH}")


# ---------- measurement helpers ----------

def average_readings(read, seconds: float, interval: float = 1.0):
    """Call read() about once per `interval` for `seconds`; return (mean, stdev, n)."""
    values = []
    end = time.monotonic() + seconds
    while True:
        values.append(read())
        if time.monotonic() + interval > end:
            break
        time.sleep(interval)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return statistics.fmean(values), sd, len(values)


def linear_fit(raw, ref):
    """Return (gain, offset) so that reference ~= gain * raw + offset.

    One point -> offset only (gain 1). Two or more -> least squares.
    """
    if len(raw) != len(ref) or not raw:
        raise ValueError("need the same, non-zero number of raw and reference points")
    if len(raw) == 1:
        return 1.0, ref[0] - raw[0]
    mx, my = statistics.fmean(raw), statistics.fmean(ref)
    sxx = sum((x - mx) ** 2 for x in raw)
    if sxx == 0:
        raise ValueError("all raw readings are identical; use points at clearly different conditions")
    gain = sum((x - mx) * (y - my) for x, y in zip(raw, ref)) / sxx
    return gain, my - gain * mx


def apply_sensor_cal(cal: dict, key: str, raw: float) -> float:
    """Apply a saved gain/offset to a raw sensor reading (no-op if not calibrated)."""
    c = cal.get("sensors", {}).get(key) if cal else None
    return raw if not c else c["gain"] * raw + c["offset"]


def camera_controls_from(cal: dict) -> dict:
    """picamera2 controls that lock white balance, exposure and focus."""
    c = cal.get("camera") if cal else None
    if not c:
        return {}
    controls = {
        "AwbEnable": False,
        "ColourGains": tuple(c["colour_gains"]),
        "AeEnable": False,
        "ExposureTime": int(c["exposure_time_us"]),
        "AnalogueGain": float(c["analogue_gain"]),
    }
    if c.get("lens_position") is not None:
        controls.update({"AfMode": 0, "LensPosition": float(c["lens_position"])})
    return controls


# ---------- image helpers (images are RGB uint8 arrays) ----------

def roi(img, frac: float = 0.4):
    """Central region of an image (frac = fraction of each side kept)."""
    h, w = img.shape[:2]
    dy, dx = int(h * (1 - frac) / 2), int(w * (1 - frac) / 2)
    return img[dy:h - dy, dx:w - dx]


def hsi_mean_hue(rgb, sat_min: float = 0.2):
    """Mean Hue (degrees) of pixels with Saturation > sat_min, plus mean Saturation.
    Same formula as backend/feature_extraction.py. Returns (None, 0.0) if all masked."""
    import numpy as np
    x = rgb.astype("float64") / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    total = r + g + b
    sat = np.where(total > 0, 1.0 - 3.0 * np.minimum(np.minimum(r, g), b) / np.maximum(total, 1e-6), 0.0)
    num = 0.5 * ((r - g) + (r - b))
    den = np.sqrt((r - g) ** 2 + (r - b) * (g - b)) + 1e-6
    theta = np.arccos(np.clip(num / den, -1.0, 1.0))
    hue = np.degrees(np.where(b <= g, theta, 2 * np.pi - theta))
    mask = sat > sat_min
    if not mask.any():
        return None, 0.0
    return float(hue[mask].mean()), float(sat[mask].mean())


def lbp_variance(rgb) -> float:
    """Variance of per-pixel LBP codes (same as backend/feature_extraction.py)."""
    import cv2
    import numpy as np
    from skimage.feature import local_binary_pattern
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return float(np.var(local_binary_pattern(gray, P=8, R=1, method="uniform")))


def sharpness(rgb, frac: float = 0.5) -> float:
    """Focus / motion-blur measure: variance of the Laplacian of the central region."""
    import cv2
    gray = cv2.cvtColor(roi(rgb, frac), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())
