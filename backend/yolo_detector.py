"""Ultralytics adapter for the bundled cucumber growth-stage detector."""
import os
from functools import lru_cache
from pathlib import Path


DEFAULT_MODEL_PATH = (
    Path(__file__).resolve().parent.parent / "models" / "yolov8n_cucumber.pt"
)
CUCUMBER_STAGES = ("developing", "maturing", "spotting", "rotting")


@lru_cache(maxsize=2)
def _load_model(model_path: str):
    try:
        from ultralytics import YOLO
    except ImportError as error:
        raise RuntimeError(
            "Install requirements-model.txt to run the cucumber YOLO detector"
        ) from error
    return YOLO(model_path)


def detect_cucumbers(rgb_image, model_path=None):
    """Return cucumber boxes in RGB-frame coordinates with YOLO confidence."""
    import numpy as np

    path = Path(model_path or os.getenv("CUKEGUARD_YOLO_MODEL_PATH") or DEFAULT_MODEL_PATH).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Cucumber YOLO weights not found: {path}")

    model = _load_model(str(path))
    confidence = float(os.getenv("CUKEGUARD_YOLO_CONFIDENCE", "0.25"))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("CUKEGUARD_YOLO_CONFIDENCE must be between 0 and 1")

    names = model.names
    class_names = list(names.values()) if isinstance(names, dict) else list(names)
    if not all(
        any(stage in str(name).lower() for name in class_names)
        for stage in CUCUMBER_STAGES
    ):
        raise RuntimeError(
            f"YOLO weights do not contain the four cucumber growth-stage classes: {class_names}"
        )

    image = np.asarray(rgb_image)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("YOLO input must be an RGB image")
    # Ultralytics expects OpenCV-style BGR arrays, while the camera adapter returns RGB.
    result = model.predict(
        source=image[:, :, ::-1].copy(),
        conf=confidence,
        verbose=False,
    )[0]

    detections = []
    for box in result.boxes:
        coordinates = box.xyxy[0].detach().cpu().tolist()
        class_id = int(box.cls[0].item())
        detections.append({
            "bbox_x": float(coordinates[0]),
            "bbox_y": float(coordinates[1]),
            "bbox_x2": float(coordinates[2]),
            "bbox_y2": float(coordinates[3]),
            "yolo_confidence": float(box.conf[0].item()),
            "class_name": str(class_names[class_id]),
        })
    return detections
