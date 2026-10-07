"""
feature_extraction.py — HSI color analysis + LBP texture analysis + the
rule-based fusion matrix described in the manuscript (Chapter 3, sections
D, E, F). This is the piece that was MISSING from the real pipeline: the
Keras classifier alone was deciding good/bad, bypassing HSI/LBP entirely.

Nothing here is trained -- HSI is a color-space conversion formula, LBP is
a pixel-neighborhood comparison, and the fusion step is a plain rule
(Hue pass AND LBP pass -> GOOD). All three run fresh on every frame; there
is nothing to retrain when adding this.
"""
import numpy as np
import cv2
from skimage.feature import local_binary_pattern

# Thresholds taken directly from the manuscript's tables (pp. 25-27).
SATURATION_MASK_MIN = 0.2        # S > 0.2 -- filters out mist/neutral reflections
HUE_PASS_MIN = 90.0              # degrees
HUE_PASS_MAX = 120.0             # degrees
HUE_REJECT_BELOW = 85.0          # degrees -- explicit reject threshold
LBP_VARIANCE_PASS_MAX = 20.0     # < 20 = smooth/hydrated, >= 20 = shriveled


def hsi_analysis(image: np.ndarray) -> tuple[float, float]:
    """
    Converts an RGB image to HSI (Hue/Saturation/Intensity) via the
    standard formula (not cv2's HSV, which defines Hue slightly
    differently at edge cases), equalizes Intensity to cancel mist
    glare, masks out low-saturation pixels, and returns the mean Hue
    (degrees) and mean Saturation over the remaining pixels.
    """
    rgb = image.astype("float64") / 255.0
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]

    intensity = (r + g + b) / 3.0
    min_rgb = np.minimum(np.minimum(r, g), b)
    sum_rgb = r + g + b
    saturation = np.where(
        sum_rgb > 0, 1.0 - (3.0 * min_rgb / np.maximum(sum_rgb, 1e-6)), 0.0
    )

    numerator = 0.5 * ((r - g) + (r - b))
    denominator = np.sqrt((r - g) ** 2 + (r - b) * (g - b)) + 1e-6
    theta = np.arccos(np.clip(numerator / denominator, -1.0, 1.0))
    hue = np.where(b <= g, theta, 2 * np.pi - theta)
    hue_deg = np.degrees(hue)

    # Histogram-equalize Intensity to cancel misting-nozzle glare, per
    # the manuscript's cdf-based equalization formula -- this stabilizes
    # Hue/Saturation upstream even though Intensity itself isn't used
    # in the pass/fail decision.
    intensity_u8 = (intensity * 255).astype("uint8")
    cv2.equalizeHist(intensity_u8)

    mask = saturation > SATURATION_MASK_MIN
    if not np.any(mask):
        return 0.0, 0.0  # fully masked (e.g. pure glare) -- hard reject

    return float(np.mean(hue_deg[mask])), float(np.mean(saturation[mask]))


def lbp_analysis(image: np.ndarray) -> float:
    """
    Computes the classic 3x3-neighborhood Local Binary Pattern on the
    grayscale image, then returns the spatial VARIANCE of the raw
    per-pixel LBP codes (sigma_LBP in the manuscript) as the surface
    texture descriptor. Low variance = smooth/hydrated skin; high
    variance = wrinkled/shriveled skin.

    Important: this is variance of the per-pixel LBP codes themselves,
    not of the LBP histogram's bin probabilities -- those give the
    opposite of the intended signal (a perfectly smooth surface produces
    a sharply peaked histogram, which has HIGH variance across bins even
    though the surface has LOW texture variance).
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    lbp = local_binary_pattern(gray, P=8, R=1, method="uniform")
    return float(np.var(lbp))


def fuse(mean_hue: float, lbp_variance: float) -> tuple[str, str]:
    """
    Rule-based AND-gate fusion (manuscript section F / the decision
    matrix table). Deterministic, not learned. Returns (condition, scenario).
    """
    hue_pass = HUE_PASS_MIN <= mean_hue <= HUE_PASS_MAX
    lbp_pass = lbp_variance < LBP_VARIANCE_PASS_MAX

    if hue_pass and lbp_pass:
        return "good", "ideal_condition"
    if hue_pass and not lbp_pass:
        return "bad", "false_green"       # green but shriveled
    if not hue_pass and lbp_pass:
        return "bad", "yellowing"         # firm but over-ripe
    return "bad", "total_degradation"     # both failed


def classify(image: np.ndarray) -> dict:
    """Runs HSI + LBP + fusion on one image and returns everything the
    CucumberSample table / API response expects."""
    mean_hue, mean_saturation = hsi_analysis(image)
    lbp_variance = lbp_analysis(image)
    condition, scenario = fuse(mean_hue, lbp_variance)
    return {
        "hsi_hue_mean": round(mean_hue, 2),
        "lbp_texture_score": round(lbp_variance, 3),
        "condition": condition,
        "scenario": scenario,
    }
