"""
vision_pipeline.py — ripeness classification pipeline.

Right now this SIMULATES a scan pass: the stepper "moves" the carriage,
a "camera" captures a frame at each position, and detection/classification
results are generated statistically to look like real YOLO + HSI + LBP output.

TO GO LIVE ON THE PI:
  - Replace `home_and_scan_positions()` with real NEMA17 stepper control
    (RPi.GPIO / gpiozero step pulses) to traverse the V-slot track.
  - Replace `capture_frame()` with a real picamera2 capture.
  - Replace `detect_and_classify()` with your trained YOLO model
    (ultralytics) for bounding boxes + confidence, OpenCV for HSI hue
    extraction, and your LBP texture function, then feed the fused
    feature vector into your ripeness classifier.
  - Everything else (session bookkeeping, DB writes, API shape) stays the same.
"""
import random

TRACK_LENGTH_MM = 600
STEP_MM = 100  # one capture every 100mm along the track

CLASS_WEIGHTS = {
    "ripe": 0.35,
    "near_ripe": 0.30,
    "unripe": 0.30,
    "spoiled": 0.05,
}

SHELF_LIFE_BY_CLASS = {
    "ripe": (2, 5),
    "near_ripe": (5, 9),
    "unripe": (9, 15),
    "spoiled": (0, 1),
}

# Owner-facing gate: "spoiled" (and over-ripe/near-spoiled ripe stock) is what
# should actually pull someone out to the shelter, not every ripeness label.
BAD_CONDITION_CLASSES = {"spoiled"}


def condition_for(ripeness_class: str, shelf_life_days: float) -> str:
    """Collapses the 4-way ripeness label into the simple good/bad gate
    that drives the SMS alert. Tune this rule to match your defense
    criteria (e.g. also flag 'ripe' with <1 day shelf life left)."""
    if ripeness_class in BAD_CONDITION_CLASSES:
        return "bad"
    if ripeness_class == "ripe" and shelf_life_days < 1.0:
        return "bad"
    return "good"


def home_and_scan_positions():
    """SIMULATED stepper sweep. Replace with real NEMA17 step sequence."""
    return list(range(0, TRACK_LENGTH_MM + 1, STEP_MM))


def capture_frame(position_mm: float):
    """SIMULATED camera capture. Replace with picamera2 frame grab."""
    return {"position_mm": position_mm}


def detect_and_classify(frame: dict):
    """
    SIMULATED YOLO detection + HSI/LBP feature fusion + classification.
    Returns 0-2 fake "detections" per frame position, each with the fields
    your CucumberSample table expects.
    """
    detections = []
    n_objects = random.choices([0, 1, 2], weights=[0.2, 0.55, 0.25])[0]
    for _ in range(n_objects):
        ripeness = random.choices(
            list(CLASS_WEIGHTS.keys()), weights=list(CLASS_WEIGHTS.values())
        )[0]
        lo, hi = SHELF_LIFE_BY_CLASS[ripeness]
        detections.append({
            "bbox_x": round(random.uniform(0, 640), 1),
            "bbox_y": round(random.uniform(0, 480), 1),
            "bbox_w": round(random.uniform(60, 140), 1),
            "bbox_h": round(random.uniform(40, 90), 1),
            "yolo_confidence": round(random.uniform(0.72, 0.98), 3),
            "hsi_hue_mean": round(random.uniform(35, 95), 2),   # green-yellow hue range
            "lbp_texture_score": round(random.uniform(0.1, 0.9), 3),
            "ripeness_class": ripeness,
            "est_shelf_life_days": round(random.uniform(lo, hi), 1),
        })
    for d in detections:
        d["condition"] = condition_for(d["ripeness_class"], d["est_shelf_life_days"])
    return detections


def run_scan(batch_id: int):
    """
    Runs one full scan pass and returns (samples, counts) ready to persist.
    `samples` is a list of dicts matching CucumberSample columns
    (minus scan_session_id, which the caller assigns after creating the row).
    """
    samples = []
    counts = {"ripe": 0, "near_ripe": 0, "unripe": 0, "spoiled": 0}
    bad_count = 0

    for pos in home_and_scan_positions():
        frame = capture_frame(pos)
        for det in detect_and_classify(frame):
            det["track_position_mm"] = pos
            samples.append(det)
            counts[det["ripeness_class"]] += 1
            if det["condition"] == "bad":
                bad_count += 1

    return samples, counts, bad_count
