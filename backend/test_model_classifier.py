import json
import zipfile

import pytest

from backend.model_classifier import (
    MODEL_PATH,
    compare_mapping_metrics,
    compare_mapping_scores,
    decode_binary_score,
)
from backend import vision_pipeline


def test_sigmoid_score_maps_good_when_good_is_positive():
    assert decode_binary_score(0.8, "good") == ("good", 0.8, 0.8)


def test_sigmoid_score_maps_bad_when_bad_is_positive():
    condition, confidence, probability_good = decode_binary_score(0.8, "bad")
    assert condition == "bad"
    assert confidence == 0.8
    assert probability_good == pytest.approx(0.2)


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_sigmoid_score_rejects_out_of_range_values(score):
    with pytest.raises(ValueError, match="between 0 and 1"):
        decode_binary_score(score, "good")


def test_sigmoid_score_requires_an_explicit_mapping():
    with pytest.raises(ValueError, match="positive_label"):
        decode_binary_score(0.8, "unknown")


def test_mapping_verifier_counts_both_sigmoid_orientations():
    assert compare_mapping_scores([
        (0.9, True),
        (0.8, False),
        (0.2, False),
    ]) == (2, 1)


def test_mapping_verifier_supports_two_class_softmax_outputs():
    metrics = compare_mapping_metrics([
        ([0.1, 0.9], True),
        ([0.8, 0.2], False),
        ([0.2, 0.8], False),
    ])

    assert metrics["default"]["correct"] == 2
    assert metrics["default"]["good_correct"] == 1
    assert metrics["default"]["bad_correct"] == 1
    assert metrics["flipped"]["correct"] == 1


def test_selected_model_has_two_class_softmax_and_128_rgb_input():
    assert MODEL_PATH.is_file()
    with zipfile.ZipFile(MODEL_PATH) as archive:
        config = json.loads(archive.read("config.json"))

    layers = config["config"]["layers"]
    input_layer = next(layer for layer in layers if layer["class_name"] == "InputLayer")
    output_layer = [layer for layer in layers if layer["class_name"] == "Dense"][-1]
    assert input_layer["config"]["batch_shape"] == [None, 128, 128, 3]
    assert output_layer["config"]["units"] == 2
    assert output_layer["config"]["activation"] == "softmax"


def test_verified_mapping_classifies_one_frame_as_one_cucumber(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "good")
    monkeypatch.setattr(
        vision_pipeline.model_classifier,
        "predict_image",
        lambda image, positive_label, model_path=None: {
            "condition": "good",
            "confidence": 0.9,
            "probability_good": 0.9,
            "width": 640,
            "height": 480,
        },
    )

    detections = vision_pipeline.detect_and_classify({"image": object(), "position_mm": 200})

    assert len(detections) == 1
    assert detections[0]["condition"] == "good"
    assert detections[0]["bbox_w"] == 640
    assert detections[0]["bbox_h"] == 480
    assert detections[0]["hsi_hue_mean"] is None
    assert detections[0]["lbp_texture_score"] is None
    assert detections[0]["est_shelf_life_days"] is None


def test_verified_mapping_requires_an_image_frame(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "bad")

    with pytest.raises(RuntimeError, match="did not provide an RGB image"):
        vision_pipeline.detect_and_classify({"position_mm": 200})


def test_unverified_mapping_keeps_the_demo_path(monkeypatch):
    monkeypatch.delenv("CUKEGUARD_MODEL_POSITIVE_LABEL", raising=False)
    monkeypatch.setattr(vision_pipeline.random, "choices", lambda *args, **kwargs: [0])

    assert vision_pipeline.detect_and_classify({"position_mm": 200}) == []