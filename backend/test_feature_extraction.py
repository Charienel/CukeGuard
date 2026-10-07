import numpy as np

from backend import feature_extraction


def test_hsi_analysis_detects_green_and_rejects_desaturated_pixels():
    green = np.zeros((64, 64, 3), dtype=np.uint8)
    green[:, :, 1] = 255

    hue_deg, saturation = feature_extraction.hsi_analysis(green)

    assert 90.0 <= hue_deg <= 130.0
    assert saturation > 0.8

    gray = np.full((64, 64, 3), 128, dtype=np.uint8)
    assert feature_extraction.hsi_analysis(gray) == (0.0, 0.0)


def test_lbp_analysis_distinguishes_flat_and_noisy_images():
    flat = np.full((64, 64, 3), 128, dtype=np.uint8)
    flat_variance = feature_extraction.lbp_analysis(flat)

    rng = np.random.default_rng(0)
    noisy = rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
    noisy_variance = feature_extraction.lbp_analysis(noisy)

    assert flat_variance < 1.0
    assert noisy_variance > flat_variance * 10


def test_fuse_handles_each_rule_quadrant_and_boundary_values():
    assert feature_extraction.fuse(90.0, 19.0) == ("good", "ideal_condition")
    assert feature_extraction.fuse(120.0, 0.0) == ("good", "ideal_condition")
    assert feature_extraction.fuse(90.0, 20.0) == ("bad", "false_green")
    assert feature_extraction.fuse(85.0, 10.0) == ("bad", "yellowing")
    assert feature_extraction.fuse(85.0, 20.0) == ("bad", "total_degradation")


def test_classify_returns_expected_keys_and_types():
    green = np.zeros((64, 64, 3), dtype=np.uint8)
    green[:, :, 1] = 255

    result = feature_extraction.classify(green)

    assert set(result) == {"hsi_hue_mean", "lbp_texture_score", "condition", "scenario"}
    assert isinstance(result["hsi_hue_mean"], float)
    assert isinstance(result["lbp_texture_score"], float)
    assert result["condition"] in {"good", "bad"}
    assert isinstance(result["scenario"], str)
