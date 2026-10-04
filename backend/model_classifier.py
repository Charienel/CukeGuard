"""Optional Keras classifier support for single-cucumber RGB frames."""
from functools import lru_cache
from pathlib import Path
from typing import Optional


MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "cucumber_condition.keras"
IMAGE_SIZE = (128, 128)


def decode_binary_score(score: float, positive_label: str) -> tuple[str, float, float]:
    """Convert a one-unit sigmoid score to condition, confidence, and P(good)."""
    if positive_label not in {"good", "bad"}:
        raise ValueError("positive_label must be 'good' or 'bad'")
    if not 0.0 <= score <= 1.0:
        raise ValueError("model sigmoid score must be between 0 and 1")

    probability_good = score if positive_label == "good" else 1.0 - score
    condition = "good" if probability_good >= 0.5 else "bad"
    confidence = max(probability_good, 1.0 - probability_good)
    return condition, confidence, probability_good


def _predicts_good(outputs, flipped: bool) -> bool:
    if isinstance(outputs, (int, float)):
        outputs = [outputs]
    if len(outputs) == 1:
        return outputs[0] < 0.5 if flipped else outputs[0] >= 0.5
    if len(outputs) == 2:
        good_index = 0 if flipped else 1
        return max(range(2), key=outputs.__getitem__) == good_index
    raise ValueError("verification supports one sigmoid or two softmax outputs")


def compare_mapping_metrics(samples):
    """Measure both output orientations overall and separately by true class."""
    metrics = {
        "default": {"correct": 0, "good_correct": 0, "good_total": 0, "bad_correct": 0, "bad_total": 0},
        "flipped": {"correct": 0, "good_correct": 0, "good_total": 0, "bad_correct": 0, "bad_total": 0},
    }
    for outputs, is_good in samples:
        for mapping, flipped in (("default", False), ("flipped", True)):
            metric = metrics[mapping]
            predicted_good = _predicts_good(outputs, flipped)
            class_key = "good" if is_good else "bad"
            metric[f"{class_key}_total"] += 1
            if predicted_good == is_good:
                metric["correct"] += 1
                metric[f"{class_key}_correct"] += 1
    return metrics


def compare_mapping_scores(samples) -> tuple[int, int]:
    """Count correct results for Good-positive and Bad-positive mappings."""
    metrics = compare_mapping_metrics(samples)
    return metrics["default"]["correct"], metrics["flipped"]["correct"]


def prepare_rgb_image(image, target_size=IMAGE_SIZE):
    """Resize an RGB image to the model input without applying extra normalization."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "Install requirements-model.txt to run the Keras classifier"
        ) from error

    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] not in {3, 4}:
        raise ValueError("model input must be an RGB or RGBA image")
    if array.shape[2] == 4:
        array = array[:, :, :3]
    if array.dtype != np.uint8:
        if np.issubdtype(array.dtype, np.floating) and array.size and array.max() <= 1.0:
            array = array * 255.0
        array = np.clip(array, 0, 255).astype(np.uint8)

    rgb_image = Image.fromarray(array, mode="RGB")
    width, height = rgb_image.size
    resized = rgb_image.resize(target_size, Image.Resampling.BILINEAR)
    batch = np.expand_dims(np.asarray(resized, dtype=np.float32), axis=0)
    return batch, width, height


@lru_cache(maxsize=2)
def _load_model(model_path: str):
    try:
        from keras.models import load_model
    except ImportError as error:
        raise RuntimeError(
            "Install requirements-model.txt to run the Keras classifier"
        ) from error
    return load_model(model_path, compile=False)


def load_model(model_path: Optional[str] = None):
    """Load the bundled classifier or a caller-supplied Keras model path."""
    return _load_model(str(Path(model_path or MODEL_PATH).resolve()))


def predict_model_output(image, model):
    """Return one sigmoid or two softmax probabilities for an RGB frame."""
    import numpy as np

    input_shape = model.input_shape
    if isinstance(input_shape, list) or len(input_shape) != 4:
        raise ValueError(f"expected one channels-last image input; got {input_shape}")
    height, width, channels = input_shape[1:]
    if channels != 3:
        raise ValueError(f"expected three RGB input channels; got {channels}")
    if height is None or width is None:
        target_size = IMAGE_SIZE
    else:
        target_size = (int(width), int(height))

    batch, original_width, original_height = prepare_rgb_image(image, target_size)
    output = np.asarray(model.predict(batch, verbose=0))
    output = output.reshape(-1)
    if output.size not in {1, 2}:
        raise ValueError(
            f"expected one sigmoid or two softmax outputs; got shape {output.shape}"
        )
    if not np.all(np.isfinite(output)) or np.any(output < 0.0) or np.any(output > 1.0):
        raise ValueError("model output probabilities must be finite and between 0 and 1")
    return output.tolist(), original_width, original_height


def predict_positive_score(image, model):
    """Return the scalar sigmoid output for a legacy binary model."""
    outputs, width, height = predict_model_output(image, model)
    if len(outputs) != 1:
        raise ValueError("expected a single sigmoid output for this model")
    return outputs[0], width, height


def predict_image(image, positive_label: str, model_path: Optional[str] = None):
    """Predict one crop, using only a mapping verified against labeled samples."""
    model = load_model(model_path)
    outputs, width, height = predict_model_output(image, model)
    if len(outputs) == 1:
        condition, confidence, probability_good = decode_binary_score(
            outputs[0], positive_label
        )
    else:
        good_index = 1 if positive_label == "good" else 0
        probability_good = outputs[good_index]
        condition = "good" if max(range(2), key=outputs.__getitem__) == good_index else "bad"
        confidence = max(outputs)
    return {
        "condition": condition,
        "confidence": confidence,
        "probability_good": probability_good,
        "width": width,
        "height": height,
    }