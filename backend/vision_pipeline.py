"""Cucumber condition detection pipeline with Good/Bad results."""
import os
import time
from typing import Optional

import numpy as np

from . import feature_extraction, iot_controller, model_classifier

TRACK_LENGTH_MM = 600
STEP_MM = 100
FIXED_CUCUMBER_COUNT = 4
FIXED_POSITION_SPACING_MM = 63.5
LED_PRELIGHT_SECONDS = float(os.getenv("CUKEGUARD_LED_PRELIGHT_SECONDS", "10"))
# Retained for the legacy approach-comparison script.
DETECTION_CONFIDENCE_MIN = 0.8


def home_and_scan_positions():
    """Return the four fixed cucumber capture positions."""
    try:
        from calibration import common, hw
        carriage = common.load().get("carriage", {})
        spm = float(carriage.get("steps_per_mm", 0))
        if spm <= 0:
            raise RuntimeError("Run calibrate_stepper --steps-per-mm before a hardware scan")
        stepper = getattr(home_and_scan_positions, "_stepper", None)
        limits = getattr(home_and_scan_positions, "_limits", None)
        if stepper is None:
            stepper = hw.Stepper()
            limits = hw.Limits()
            home_and_scan_positions._stepper = stepper
            home_and_scan_positions._limits = limits
        if not limits:
            raise RuntimeError("Limit switches are not initialized")
        stepper.step(
            20000,
            not hw.HOME_IS_BACKWARD,
            carriage.get("step_delay_s", 0.005),
            stop_if=lambda: limits.home.is_pressed,
        )
        if not limits.home.is_pressed:
            raise RuntimeError("Stepper did not reach the home limit switch")
        capture_frame._hardware_position_mm = 0.0
        return [i * FIXED_POSITION_SPACING_MM for i in range(FIXED_CUCUMBER_COUNT)]
    except Exception as exc:
        raise RuntimeError(f"Hardware scan positioning failed: {exc}") from exc


def capture_frame(position_mm: float):
    """Capture an RGB frame from the USB webcam at a fixed stepper position."""
    from calibration import hw
    from calibration import common
    cam = getattr(capture_frame, "_camera", None)
    if cam is None:
        cam = hw.open_camera()
        capture_frame._camera = cam
    carriage = common.load().get("carriage", {})
    spm = float(carriage.get("steps_per_mm", 0))
    if spm <= 0:
        raise RuntimeError("steps_per_mm is not calibrated")
    stepper = getattr(home_and_scan_positions, "_stepper", None)
    limits = getattr(home_and_scan_positions, "_limits", None)
    if stepper is None or limits is None:
        raise RuntimeError("Hardware scan has not completed homing")
    current_mm = getattr(capture_frame, "_hardware_position_mm", 0.0)
    delta_mm = float(position_mm) - current_mm
    if abs(delta_mm) > 1e-9:
        n = round(abs(delta_mm) * spm)
        forward = hw.HOME_IS_BACKWARD if delta_mm > 0 else not hw.HOME_IS_BACKWARD
        taken = stepper.step(
            n,
            forward,
            carriage.get("step_delay_s", 0.005),
            stop_if=lambda: limits.far.is_pressed if delta_mm > 0 else limits.home.is_pressed,
        )
        if taken < n:
            raise RuntimeError("Stepper stopped at a limit switch before the indexed position")
    capture_frame._hardware_position_mm = float(position_mm)
    time.sleep(max(0.0, float(carriage.get("settle_s", 1.0))))
    return {"position_mm": position_mm, "image": cam.capture_array()}


def detect_and_classify(frame: dict, object_count: Optional[int] = None):
    """
    In model mode, HSI/LBP supplies the final Good/Bad condition. Keras
    confidence is retained as supporting information.
    Returns 0-2 detections per frame with the fields your CucumberSample table expects.
    """
    positive_label = os.getenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "").strip().lower()
    if positive_label:
        if positive_label not in {"good", "bad"}:
            raise ValueError("CUKEGUARD_MODEL_POSITIVE_LABEL must be 'good' or 'bad'")
        if object_count == 0:
            return []
        image = frame.get("image") if isinstance(frame, dict) else frame
        if image is None:
            raise RuntimeError(
                "Keras classification is enabled but capture_frame did not provide an RGB image"
            )

        prediction = model_classifier.predict_image(
            image,
            positive_label,
            model_path=os.getenv("CUKEGUARD_MODEL_PATH"),
        )
        image_array = np.asarray(
            image.convert("RGB") if hasattr(image, "convert") else image
        )
        features = feature_extraction.classify(image_array)

        return [{
            "bbox_x": 0.0,
            "bbox_y": 0.0,
            "bbox_w": float(prediction["width"]),
            "bbox_h": float(prediction["height"]),
            "yolo_confidence": prediction["confidence"],
            "hsi_hue_mean": features["hsi_hue_mean"],
            "lbp_texture_score": features["lbp_texture_score"],
            "condition": features["condition"],
            "est_shelf_life_days": None,
        }]

    image = frame.get("image") if isinstance(frame, dict) else frame
    if image is None:
        raise RuntimeError("USB capture did not provide an RGB image")
    image_array = np.asarray(
        image.convert("RGB") if hasattr(image, "convert") else image
    )
    features = feature_extraction.classify(image_array)
    return [{
        "bbox_x": 0.0,
        "bbox_y": 0.0,
        "bbox_w": float(image_array.shape[1]),
        "bbox_h": float(image_array.shape[0]),
        "yolo_confidence": 1.0,
        "hsi_hue_mean": features["hsi_hue_mean"],
        "lbp_texture_score": features["lbp_texture_score"],
        "condition": features["condition"],
        "est_shelf_life_days": None,
    }]


def run_scan(batch_id: int, expected_count: Optional[int] = None, on_capture=None):
    """
    Runs one full scan pass and returns (samples, condition_counts).
    `samples` is a list of dicts matching CucumberSample columns
    (minus batch_id and scan_id, which the caller assigns after creating the row).
    """
    iot_controller.set_cabin_light(True)
    scan_completed = False
    try:
        if LED_PRELIGHT_SECONDS > 0:
            time.sleep(LED_PRELIGHT_SECONDS)
        samples = []
        counts = {"good": 0, "bad": 0}

        positions = home_and_scan_positions()
        for cucumber_index, pos in enumerate(positions, start=1):
            frame = capture_frame(pos)
            detections = detect_and_classify(frame, object_count=1)
            if not detections:
                detections = [{
                    "bbox_x": 0.0,
                    "bbox_y": 0.0,
                    "bbox_w": 0.0,
                    "bbox_h": 0.0,
                    "yolo_confidence": 0.0,
                    "hsi_hue_mean": None,
                    "lbp_texture_score": None,
                    "condition": "bad",
                    "est_shelf_life_days": None,
                }]
            for det in detections[:1]:
                det["track_position_mm"] = pos
                det["cucumber_number"] = cucumber_index
                counts[det["condition"]] += 1
                samples.append(det)
                if on_capture is not None:
                    on_capture(det)

        scan_completed = True
        return samples, counts
    finally:
        stepper = getattr(home_and_scan_positions, "_stepper", None)
        limits = getattr(home_and_scan_positions, "_limits", None)
        try:
            if stepper is not None and limits is not None:
                from calibration import common, hw
                carriage = common.load().get("carriage", {})
                delay_s = carriage.get("step_delay_s", 0.005)
                if scan_completed and not limits.far.is_pressed:
                    stepper.step(
                        20000,
                        hw.HOME_IS_BACKWARD,
                        delay_s,
                        stop_if=lambda: limits.far.is_pressed,
                    )
                    if not limits.far.is_pressed:
                        raise RuntimeError("Stepper did not reach the far limit before returning home")
                if not limits.home.is_pressed:
                    stepper.step(
                        20000,
                        not hw.HOME_IS_BACKWARD,
                        delay_s,
                        stop_if=lambda: limits.home.is_pressed,
                    )
                if not limits.home.is_pressed:
                    raise RuntimeError("Stepper did not return to the home limit")
        finally:
            try:
                iot_controller.set_cabin_light(False)
            finally:
                cam = getattr(capture_frame, "_camera", None)
                try:
                    if cam is not None:
                        cam.close()
                finally:
                    capture_frame._camera = None
                    if stepper is not None:
                        stepper.release()
                    home_and_scan_positions._stepper = None
                    home_and_scan_positions._limits = None
