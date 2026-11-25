"""Cucumber condition detection pipeline with Good/Bad results."""
import math
import os
import time
from typing import Optional

import numpy as np

from . import feature_extraction, iot_controller, model_classifier, yolo_detector

FIXED_CUCUMBER_COUNT = 4
TRACK_LENGTH_MM = 254.0
FIXED_POSITION_SPACING_MM = TRACK_LENGTH_MM / FIXED_CUCUMBER_COUNT
SCAN_SAMPLE_INTERVAL_MM = float(os.getenv("CUKEGUARD_SCAN_SAMPLE_MM", "5"))
SCAN_SETTLE_SECONDS = float(os.getenv("CUKEGUARD_SCAN_SETTLE_SECONDS", "0.1"))
HOME_BACKOFF_STEPS = 200
LED_PRELIGHT_SECONDS = float(os.getenv("CUKEGUARD_LED_PRELIGHT_SECONDS", "10"))


def home_and_scan_positions():
    """Home at the right-side Home/MIN switch and plan a bounded sweep to Far/MAX."""
    try:
        from calibration import common, hw
        carriage = common.load().get("carriage", {})
        spm = float(carriage.get("steps_per_mm", 0))
        if spm <= 0:
            raise RuntimeError("Run calibrate_stepper --steps-per-mm before a hardware scan")
        if SCAN_SAMPLE_INTERVAL_MM <= 0:
            raise RuntimeError("CUKEGUARD_SCAN_SAMPLE_MM must be greater than zero")
        stepper = getattr(home_and_scan_positions, "_stepper", None)
        limits = getattr(home_and_scan_positions, "_limits", None)
        if stepper is None:
            stepper = hw.Stepper()
            limits = hw.Limits()
            home_and_scan_positions._stepper = stepper
            home_and_scan_positions._limits = limits
        if not limits:
            raise RuntimeError("Limit switches are not initialized")
        if limits.home.is_pressed and limits.far.is_pressed:
            raise RuntimeError("Both carriage limit switches are active")
        toward_home = not hw.HOME_IS_BACKWARD
        away_from_home = hw.HOME_IS_BACKWARD
        delay_s = carriage.get("step_delay_s", 0.005)
        max_travel_steps = math.ceil(TRACK_LENGTH_MM * spm)
        if limits.far.is_pressed:
            stepper.step(
                HOME_BACKOFF_STEPS,
                toward_home,
                delay_s * 2,
                stop_if=lambda: limits.home.is_pressed,
            )
        elif limits.home.is_pressed:
            stepper.step(
                HOME_BACKOFF_STEPS,
                away_from_home,
                delay_s * 2,
                stop_if=lambda: limits.far.is_pressed,
            )
        stepper.step(
            max_travel_steps,
            toward_home,
            delay_s,
            stop_if=lambda: limits.home.is_pressed or limits.far.is_pressed,
        )
        if limits.far.is_pressed and not limits.home.is_pressed:
            raise RuntimeError("Far/MAX limit activated while homing; verify motor direction")
        if not limits.home.is_pressed:
            raise RuntimeError("Stepper did not reach the home limit switch")
        capture_frame._hardware_position_mm = 0.0
        max_distance_mm = TRACK_LENGTH_MM
        sample_count = math.floor(max_distance_mm / SCAN_SAMPLE_INTERVAL_MM)
        positions = [
            round(i * SCAN_SAMPLE_INTERVAL_MM, 3)
            for i in range(sample_count + 1)
        ]
        if positions[-1] < max_distance_mm:
            positions.append(round(max_distance_mm, 3))
        return positions
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
    limit_reached = False
    if abs(delta_mm) > 1e-9:
        n = round(abs(delta_mm) * spm)
        forward = hw.HOME_IS_BACKWARD if delta_mm > 0 else not hw.HOME_IS_BACKWARD
        taken = stepper.step(
            n,
            forward,
            carriage.get("step_delay_s", 0.005),
            stop_if=lambda: limits.far.is_pressed if delta_mm > 0 else limits.home.is_pressed,
        )
        limit_reached = (
            delta_mm > 0 and limits.far.is_pressed
        ) or (
            delta_mm < 0 and limits.home.is_pressed
        )
        if taken < n:
            if not limit_reached:
                raise RuntimeError("Stepper stopped before the requested position")
    actual_position_mm = current_mm
    if abs(delta_mm) > 1e-9:
        actual_position_mm += (taken / spm) * (1 if delta_mm > 0 else -1)
    capture_frame._hardware_position_mm = actual_position_mm
    time.sleep(max(0.0, SCAN_SETTLE_SECONDS))
    return {
        "position_mm": actual_position_mm,
        "image": cam.capture_array(),
        "limit_reached": limit_reached,
    }


def detect_cucumber_crops(frame: dict, object_count: Optional[int] = None):
    """Run YOLO on one RGB frame and retain each valid cucumber crop."""
    if object_count == 0:
        return []
    image = frame.get("image") if isinstance(frame, dict) else frame
    if image is None:
        raise RuntimeError("USB capture did not provide an RGB image")
    image_array = np.asarray(image.convert("RGB") if hasattr(image, "convert") else image)
    if image_array.ndim != 3 or image_array.shape[2] != 3:
        raise ValueError("USB capture must provide an RGB image")

    detections = []
    for box in yolo_detector.detect_cucumbers(image_array):
        x1 = max(0, min(image_array.shape[1], int(box["bbox_x"])))
        y1 = max(0, min(image_array.shape[0], int(box["bbox_y"])))
        x2 = max(0, min(image_array.shape[1], int(math.ceil(box["bbox_x2"]))))
        y2 = max(0, min(image_array.shape[0], int(math.ceil(box["bbox_y2"]))))
        crop = image_array[y1:y2, x1:x2]
        if crop.size == 0:
            continue

        detections.append({
            "bbox_x": float(x1),
            "bbox_y": float(y1),
            "bbox_w": float(x2 - x1),
            "bbox_h": float(y2 - y1),
            "yolo_confidence": float(box["yolo_confidence"]),
            "_crop": crop,
        })
    return sorted(detections, key=lambda detection: detection["bbox_x"])


def classify_cucumber_crop(detection: dict):
    """Use the verified Keras mapping and image features on one YOLO crop."""
    positive_label = os.getenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "good").strip().lower()
    if positive_label not in {"good", "bad"}:
        raise RuntimeError(
            "CUKEGUARD_MODEL_POSITIVE_LABEL must be the verified Keras mapping ('good' or 'bad')"
        )
    crop = detection["_crop"]
    prediction = model_classifier.predict_image(
        crop,
        positive_label,
        model_path=os.getenv("CUKEGUARD_MODEL_PATH"),
    )
    features = feature_extraction.classify(crop)
    return {
        "bbox_x": detection["bbox_x"],
        "bbox_y": detection["bbox_y"],
        "bbox_w": detection["bbox_w"],
        "bbox_h": detection["bbox_h"],
        "yolo_confidence": detection["yolo_confidence"],
        "hsi_hue_mean": features["hsi_hue_mean"],
        "lbp_texture_score": features["lbp_texture_score"],
        "condition": prediction["condition"],
        "est_shelf_life_days": None,
    }


def detect_and_classify(frame: dict, object_count: Optional[int] = None):
    """Detect cucumber boxes with YOLO and classify each detected crop with Keras."""
    return [
        classify_cucumber_crop(detection)
        for detection in detect_cucumber_crops(frame, object_count)
    ]


def run_scan(batch_id: int, expected_count: Optional[int] = None, on_capture=None):
    """
    Runs one full scan pass and returns (samples, condition_counts).
    `samples` is a list of dicts matching CucumberSample columns
    (minus batch_id and scan_id, which the caller assigns after creating the row).
    """
    far_limit_reached = False
    iot_controller.set_cabin_light(True)
    try:
        if LED_PRELIGHT_SECONDS > 0:
            time.sleep(LED_PRELIGHT_SECONDS)
        counts = {"good": 0, "bad": 0}

        positions = home_and_scan_positions()
        best_by_cucumber = {}
        stepper = getattr(home_and_scan_positions, "_stepper", None)
        for pos in positions:
            frame = capture_frame(pos)
            actual_pos = float(frame.get("position_mm", pos))
            if 0.0 <= actual_pos <= TRACK_LENGTH_MM:
                slot = min(
                    math.floor(actual_pos / FIXED_POSITION_SPACING_MM),
                    FIXED_CUCUMBER_COUNT - 1,
                )
                for det in detect_cucumber_crops(frame):
                    det["track_position_mm"] = actual_pos
                    det["cucumber_number"] = slot + 1
                    previous = best_by_cucumber.get(slot)
                    if previous is None or det["yolo_confidence"] > previous["yolo_confidence"]:
                        best_by_cucumber[slot] = det

            if frame.get("limit_reached"):
                far_limit_reached = True
                break

        if stepper is not None and not far_limit_reached:
            raise RuntimeError(
                "The carriage did not reach the Far/MAX limit within the scan travel bound; "
                "check limit-switch polarity and calibrated steps/mm"
            )
        missing = [
            str(index + 1)
            for index in range(FIXED_CUCUMBER_COUNT)
            if index not in best_by_cucumber
        ]
        if missing:
            raise RuntimeError(
                f"YOLO detected only {len(best_by_cucumber)} of 4 cucumbers; "
                f"missing cucumber number(s): {', '.join(missing)}"
            )
        samples = []
        for index in range(FIXED_CUCUMBER_COUNT):
            detected = best_by_cucumber[index]
            sample = classify_cucumber_crop(detected)
            sample["track_position_mm"] = detected["track_position_mm"]
            sample["cucumber_number"] = detected["cucumber_number"]
            samples.append(sample)

        for det in samples:
            counts[det["condition"]] += 1
            if on_capture is not None:
                on_capture(det)

        return samples, counts
    finally:
        stepper = getattr(home_and_scan_positions, "_stepper", None)
        limits = getattr(home_and_scan_positions, "_limits", None)
        try:
            if stepper is not None and limits is not None and far_limit_reached:
                from calibration import common, hw
                carriage = common.load().get("carriage", {})
                delay_s = carriage.get("step_delay_s", 0.005)
                max_home_steps = math.ceil(
                    TRACK_LENGTH_MM * float(carriage.get("steps_per_mm", 0))
                )
                if not limits.home.is_pressed:
                    stepper.step(
                        HOME_BACKOFF_STEPS,
                        not hw.HOME_IS_BACKWARD,
                        delay_s * 2,
                        stop_if=lambda: limits.home.is_pressed,
                    )
                    stepper.step(
                        max_home_steps,
                        not hw.HOME_IS_BACKWARD,
                        delay_s,
                        stop_if=lambda: limits.home.is_pressed or limits.far.is_pressed,
                    )
                if limits.far.is_pressed and not limits.home.is_pressed:
                    raise RuntimeError("Far/MAX limit activated while returning home")
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
                    if limits is not None:
                        limits.close()
                    home_and_scan_positions._stepper = None
                    home_and_scan_positions._limits = None
