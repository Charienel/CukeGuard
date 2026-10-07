"""
vision_pipeline.py — cucumber condition detection pipeline.

Keras makes the primary condition prediction. HSI/LBP provides a second opinion
for uncertain predictions and model-positive good predictions, with conflicts
sent for inspection instead of being forced into a good/bad result.
"""
import os
import random
from typing import Optional

import numpy as np

from . import feature_extraction, model_classifier

SECOND_OPINION_CONFIDENCE_MIN = 0.8
# Kept as an alias for callers that used the previous threshold name.
DETECTION_CONFIDENCE_MIN = SECOND_OPINION_CONFIDENCE_MIN
TRACK_LENGTH_MM = 600
STEP_MM = 100


def home_and_scan_positions():
    """SIMULATED right-to-left sweep. Replace with real NEMA17 step sequence."""
    return list(range(TRACK_LENGTH_MM, -1, -STEP_MM))


def capture_frame(position_mm: float):
    """SIMULATED camera capture. Replace with picamera2 frame grab."""
    return {"position_mm": position_mm}


def detect_and_classify(frame: dict, object_count: Optional[int] = None):
    """
    Real classification path: Keras primary prediction + conditional HSI/LBP review.
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
        model_condition = prediction["condition"]
        needs_second_opinion = (
            prediction["confidence"] < SECOND_OPINION_CONFIDENCE_MIN
            or model_condition == "good"
        )
        features = None
        condition = model_condition
        if needs_second_opinion:
            image_array = np.asarray(
                image.convert("RGB") if hasattr(image, "convert") else image
            )
            features = feature_extraction.classify(image_array)
            if features["condition"] != model_condition:
                condition = "needs_inspection"

        return [{
            "bbox_x": 0.0,
            "bbox_y": 0.0,
            "bbox_w": float(prediction["width"]),
            "bbox_h": float(prediction["height"]),
            "yolo_confidence": prediction["confidence"],
            "hsi_hue_mean": features["hsi_hue_mean"] if features else None,
            "lbp_texture_score": features["lbp_texture_score"] if features else None,
            "condition": condition,
            "est_shelf_life_days": None,
        }]

    detections = []
    n_objects = (
        object_count
        if object_count is not None
        else random.choices([0, 1, 2], weights=[0.2, 0.55, 0.25])[0]
    )
    for _ in range(n_objects):
        condition = random.choices(["good", "bad"], weights=[0.9, 0.1])[0]
        shelf_life_days = random.uniform(10, 18) if condition == "good" else random.uniform(0, 3)
        detections.append({
            "bbox_x": round(random.uniform(0, 640), 1),
            "bbox_y": round(random.uniform(0, 480), 1),
            "bbox_w": round(random.uniform(60, 140), 1),
            "bbox_h": round(random.uniform(40, 90), 1),
            "yolo_confidence": round(random.uniform(0.72, 0.98), 3),
            "hsi_hue_mean": round(random.uniform(35, 95), 2),
            "lbp_texture_score": round(random.uniform(0.1, 0.9), 3),
            "condition": condition,
            "est_shelf_life_days": round(shelf_life_days, 1),
        })
    return detections


def run_scan(batch_id: int, expected_count: Optional[int] = None):
    """
    Runs one full scan pass and returns (samples, condition_counts).
    `samples` is a list of dicts matching CucumberSample columns
    (minus batch_id and scan_id, which the caller assigns after creating the row).
    """
    samples = []
    counts = {"good": 0, "bad": 0, "needs_inspection": 0}

    positions = home_and_scan_positions()
    if expected_count is not None and expected_count > 0:
        for cucumber_index in range(expected_count):
            position_index = min(
                cucumber_index * len(positions) // expected_count,
                len(positions) - 1,
            )
            pos = positions[position_index]
            for det in detect_and_classify(capture_frame(pos), object_count=1):
                det["track_position_mm"] = pos
                samples.append(det)
    else:
        for pos in positions:
            frame = capture_frame(pos)
            for det in detect_and_classify(frame):
                det["track_position_mm"] = pos
                samples.append(det)

    for sample in samples:
        counts[sample["condition"]] += 1

    samples.sort(
        key=lambda sample: (sample["track_position_mm"], sample["bbox_x"]),
        reverse=True,
    )
    for cucumber_number, sample in enumerate(samples, start=1):
        sample["cucumber_number"] = cucumber_number

    return samples, counts
