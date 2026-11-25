import json
import zipfile

import numpy as np
import pytest

from backend.model_classifier import (
    MODEL_PATH,
    compare_mapping_metrics,
    compare_mapping_scores,
    decode_binary_score,
)
from backend import vision_pipeline
from backend import yolo_detector


def patch_one_yolo_box(monkeypatch, box=None):
    box = box or {
        "bbox_x": 0.0,
        "bbox_y": 0.0,
        "bbox_x2": 8.0,
        "bbox_y2": 8.0,
        "yolo_confidence": 0.9,
        "class_name": "developing_cucumber",
    }
    monkeypatch.setattr(
        yolo_detector,
        "detect_cucumbers",
        lambda image: [box],
    )


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
    patch_one_yolo_box(monkeypatch, {
        "bbox_x": 4.0,
        "bbox_y": 5.0,
        "bbox_x2": 50.0,
        "bbox_y2": 55.0,
        "yolo_confidence": 0.91,
        "class_name": "maturing_cucumber",
    })
    classified_crops = []
    monkeypatch.setattr(
        vision_pipeline.model_classifier,
        "predict_image",
        lambda image, positive_label, model_path=None: classified_crops.append(image.shape) or {
            "condition": "good",
            "confidence": 0.9,
            "probability_good": 0.9,
            "width": 128,
            "height": 128,
        },
    )

    green_image = np.zeros((64, 64, 3), dtype=np.uint8)
    green_image[:, :, 1] = 255
    detections = vision_pipeline.detect_and_classify({
        "image": green_image,
        "position_mm": 200,
    })

    assert len(detections) == 1
    assert detections[0]["condition"] == "good"
    assert detections[0]["bbox_x"] == 4
    assert detections[0]["bbox_y"] == 5
    assert detections[0]["bbox_w"] == 46
    assert detections[0]["bbox_h"] == 50
    assert detections[0]["yolo_confidence"] == 0.91
    assert classified_crops == [(50, 46, 3)]
    assert detections[0]["hsi_hue_mean"] is not None
    assert detections[0]["lbp_texture_score"] is not None
    assert detections[0]["est_shelf_life_days"] is None


def test_keras_good_with_hsi_good_is_accepted(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "good")
    patch_one_yolo_box(monkeypatch)
    monkeypatch.setattr(
        vision_pipeline.model_classifier,
        "predict_image",
        lambda *args, **kwargs: {
            "condition": "good", "confidence": 0.95, "width": 80, "height": 60,
        },
    )
    monkeypatch.setattr(
        vision_pipeline.feature_extraction,
        "classify",
        lambda image: {
            "condition": "good", "hsi_hue_mean": 110.0, "lbp_texture_score": 2.0,
        },
    )

    detections = vision_pipeline.detect_and_classify({"image": np.zeros((8, 8, 3), dtype=np.uint8)})

    assert detections[0]["condition"] == "good"
    assert detections[0]["hsi_hue_mean"] == 110.0


def test_keras_good_remains_condition_when_hsi_disagrees(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "good")
    patch_one_yolo_box(monkeypatch)
    monkeypatch.setattr(
        vision_pipeline.model_classifier,
        "predict_image",
        lambda *args, **kwargs: {
            "condition": "good", "confidence": 0.95, "width": 80, "height": 60,
        },
    )
    monkeypatch.setattr(
        vision_pipeline.feature_extraction,
        "classify",
        lambda image: {
            "condition": "bad", "hsi_hue_mean": 55.0, "lbp_texture_score": 35.0,
        },
    )

    detections = vision_pipeline.detect_and_classify({"image": np.zeros((8, 8, 3), dtype=np.uint8)})

    assert detections[0]["condition"] == "good"


def test_keras_bad_remains_condition_when_hsi_disagrees(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "good")
    patch_one_yolo_box(monkeypatch)
    monkeypatch.setattr(
        vision_pipeline.model_classifier,
        "predict_image",
        lambda *args, **kwargs: {
            "condition": "bad", "confidence": 0.65, "width": 80, "height": 60,
        },
    )
    monkeypatch.setattr(
        vision_pipeline.feature_extraction,
        "classify",
        lambda image: {
            "condition": "good", "hsi_hue_mean": 110.0, "lbp_texture_score": 2.0,
        },
    )

    detections = vision_pipeline.detect_and_classify({"image": np.zeros((8, 8, 3), dtype=np.uint8)})

    assert detections[0]["condition"] == "bad"


def test_keras_condition_is_used_with_hsi_features(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "good")
    patch_one_yolo_box(monkeypatch)
    monkeypatch.setattr(
        vision_pipeline.model_classifier,
        "predict_image",
        lambda *args, **kwargs: {
            "condition": "bad", "confidence": 0.95, "width": 80, "height": 60,
        },
    )
    monkeypatch.setattr(
        vision_pipeline.feature_extraction,
        "classify",
        lambda image: {
            "condition": "good", "hsi_hue_mean": 110.0, "lbp_texture_score": 2.0,
        },
    )

    detections = vision_pipeline.detect_and_classify({"image": np.zeros((8, 8, 3), dtype=np.uint8)})

    assert detections[0]["condition"] == "bad"
    assert detections[0]["hsi_hue_mean"] == 110.0


def test_default_verified_mapping_requires_an_image_frame(monkeypatch):
    monkeypatch.delenv("CUKEGUARD_MODEL_POSITIVE_LABEL", raising=False)

    with pytest.raises(RuntimeError, match="did not provide an RGB image"):
        vision_pipeline.detect_and_classify({"position_mm": 200})


def test_invalid_mapping_is_rejected(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "unknown")

    with pytest.raises(RuntimeError, match="verified Keras mapping"):
        vision_pipeline.classify_cucumber_crop({"_crop": np.zeros((8, 8, 3))})


def test_no_yolo_box_skips_keras_classification(monkeypatch):
    monkeypatch.setenv("CUKEGUARD_MODEL_POSITIVE_LABEL", "good")
    monkeypatch.setattr(yolo_detector, "detect_cucumbers", lambda image: [])
    monkeypatch.setattr(
        vision_pipeline.model_classifier,
        "predict_image",
        lambda *args, **kwargs: pytest.fail("Keras should not run without a cucumber box"),
    )

    assert vision_pipeline.detect_and_classify({
        "image": np.zeros((8, 8, 3), dtype=np.uint8),
    }) == []


def test_yolo_adapter_converts_rgb_to_bgr_and_returns_boxes(monkeypatch):
    class Coordinate:
        def detach(self):
            return self

        def cpu(self):
            return self

        def tolist(self):
            return [1.0, 2.0, 7.0, 8.0]

    class Scalar:
        def __init__(self, value):
            self.value = value

        def item(self):
            return self.value

    class Box:
        xyxy = [Coordinate()]
        cls = [Scalar(0)]
        conf = [Scalar(0.88)]

    class Model:
        names = {
            0: "developing_cucumber",
            1: "maturing_cucumber",
            2: "spotting_cucumber",
            3: "rotting_cucumber",
        }

        def predict(self, source, conf, verbose):
            assert source[0, 0].tolist() == [3, 2, 1]
            assert conf == 0.25
            assert verbose is False
            return [type("Result", (), {"boxes": [Box()]})()]

    monkeypatch.setattr(yolo_detector, "_load_model", lambda path: Model())
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    image[0, 0] = [1, 2, 3]

    result = yolo_detector.detect_cucumbers(image, model_path=str(MODEL_PATH))

    assert result == [{
        "bbox_x": 1.0,
        "bbox_y": 2.0,
        "bbox_x2": 7.0,
        "bbox_y2": 8.0,
        "yolo_confidence": 0.88,
        "class_name": "developing_cucumber",
    }]