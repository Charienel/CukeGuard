#!/usr/bin/env python3
"""Compare model-only, HSI/LBP-fusion, and hybrid cucumber classification."""

import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import feature_extraction, model_classifier  # noqa: E402
from backend import vision_pipeline  # noqa: E402

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def iter_images(folder: Path):
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS
    )


def _resize_rgb_image(image):
    arr = np.asarray(image.convert("RGB"), dtype=np.uint8)
    resized = Image.fromarray(arr, mode="RGB").resize(model_classifier.IMAGE_SIZE, Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=np.float32)


def predict_outputs_for_images(images):
    if not images:
        return np.empty((0,))
    prepared = [_resize_rgb_image(image) for image in images]
    batch = np.stack(prepared)
    return MODEL.predict(batch, verbose=0)


MODEL = model_classifier.load_model(os.getenv("CUKEGUARD_MODEL_PATH"))


def _keras_decision_from_output(output):
    outputs = np.asarray(output, dtype=np.float32).reshape(-1)
    if outputs.size == 1:
        condition, confidence, _ = model_classifier.decode_binary_score(float(outputs[0]), "good")
        return condition, float(confidence)
    good_index = 1
    probability_good = float(outputs[good_index])
    condition = "good" if int(np.argmax(outputs)) == good_index else "bad"
    confidence = float(np.max(outputs))
    return condition, confidence


def prediction_for_keras_only(image_path: Path, outputs=None):
    if outputs is None:
        image = Image.open(image_path).convert("RGB")
        outputs = predict_outputs_for_images([image])[0]
    condition, _ = _keras_decision_from_output(outputs)
    return condition


def prediction_for_hsi_lbp(image_path: Path, outputs=None):
    if outputs is None:
        image = Image.open(image_path).convert("RGB")
        outputs = predict_outputs_for_images([image])[0]
    condition, confidence = _keras_decision_from_output(outputs)
    if confidence < vision_pipeline.DETECTION_CONFIDENCE_MIN:
        return "bad"
    image = Image.open(image_path).convert("RGB")
    features = feature_extraction.classify(np.asarray(image, dtype=np.uint8))
    return features["condition"]


def prediction_for_hybrid(image_path: Path, outputs=None):
    if outputs is None:
        image = Image.open(image_path).convert("RGB")
        outputs = predict_outputs_for_images([image])[0]
    a = prediction_for_keras_only(image_path, outputs)
    b = prediction_for_hsi_lbp(image_path, outputs)
    return "good" if a == "good" and b == "good" else "bad"


def evaluate_approach(folder: Path, actual_label: str, approach_fn):
    confusion = {"good": {"good": 0, "bad": 0}, "bad": {"good": 0, "bad": 0}}
    total = 0
    correct = 0
    bad_missed = 0
    image_paths = iter_images(folder)
    batch_size = 32
    for start in range(0, len(image_paths), batch_size):
        chunk = image_paths[start : start + batch_size]
        images = [Image.open(image_path).convert("RGB") for image_path in chunk]
        outputs = predict_outputs_for_images(images)
        for image_path, image_output in zip(chunk, outputs):
            prediction = approach_fn(image_path, image_output)
            confusion[actual_label][prediction] += 1
            total += 1
            if prediction == actual_label:
                correct += 1
            if actual_label == "bad" and prediction == "good":
                bad_missed += 1
    accuracy = correct / total if total else 0.0
    return {
        "total": total,
        "accuracy": accuracy,
        "correct": correct,
        "bad_missed": bad_missed,
        "confusion": confusion,
    }


def summarize_results(approach_name, approach_fn, good_dir: Path, bad_dir: Path):
    good_result = evaluate_approach(good_dir, "good", approach_fn)
    bad_result = evaluate_approach(bad_dir, "bad", approach_fn)
    total = good_result["total"] + bad_result["total"]
    correct = good_result["correct"] + bad_result["correct"]
    bad_missed = good_result["bad_missed"] + bad_result["bad_missed"]
    accuracy = correct / total if total else 0.0
    confusion = {
        "good": {
            "good": good_result["confusion"]["good"]["good"],
            "bad": good_result["confusion"]["good"]["bad"],
        },
        "bad": {
            "good": bad_result["confusion"]["bad"]["good"],
            "bad": bad_result["confusion"]["bad"]["bad"],
        },
    }

    print(f"\n=== {approach_name} ===")
    print(f"good samples: {good_result['total']}")
    print(f"bad samples:  {bad_result['total']}")
    print(f"overall accuracy: {accuracy:.4f}")
    print(f"bad cucumbers missed (predicted as good): {bad_missed}")
    print("confusion matrix (rows = actual, cols = predicted):")
    print("          good   bad")
    print(f"good      {confusion['good']['good']:5d} {confusion['good']['bad']:5d}")
    print(f"bad       {confusion['bad']['good']:5d} {confusion['bad']['bad']:5d}")
    if accuracy == 1.0 and total > 20:
        print("WARNING: suspiciously perfect accuracy on this benchmark.")


def main():
    root_dir = ROOT / "verification_samples"
    good_dir = root_dir / "good"
    bad_dir = root_dir / "bad"
    if not good_dir.exists() or not bad_dir.exists():
        raise FileNotFoundError("verification_samples/good and verification_samples/bad must exist")

    good_count = len(iter_images(good_dir))
    bad_count = len(iter_images(bad_dir))
    print("CukeGuard verification sample comparison")
    print(f"good folder: {good_count} images")
    print(f"bad folder:  {bad_count} images")

    summarize_results("Approach A: Keras-only", prediction_for_keras_only, good_dir, bad_dir)
    summarize_results("Approach B: HSI + LBP fusion", prediction_for_hsi_lbp, good_dir, bad_dir)
    summarize_results("Approach C: Hybrid AND", prediction_for_hybrid, good_dir, bad_dir)


if __name__ == "__main__":
    main()
