"""
calibrate_thresholds.py - re-derive the HSI hue range and the LBP variance cutoff
from YOUR labeled photos, instead of the manuscript's values, and test them on
photos the search never saw.

Run from the repo root (no hardware needed):
    python -m calibration.calibrate_thresholds verification_samples --max-bad-miss 0.05 --save

Expects  <folder>/good/*.jpg  and  <folder>/bad/*.jpg  (label them with the
Department of Agriculture's help if you can). Photos must be taken with the
camera calibration in place, because hue depends on white balance and lighting.

HOW IT WORKS
 * Images are split in two: half is used to search for thresholds, the other
   half is only used to report the result. Reporting on the images used for the
   search would overstate the accuracy.
 * Rule searched: GOOD if  hue_min <= hue <= hue_max  AND  LBP variance < lbp_max
   (the same AND gate as the manuscript).
 * Objective: keep the share of BAD cucumbers missed at or below --max-bad-miss,
   and among those settings keep as many GOOD cucumbers as possible. If no setting
   can meet the cap, it falls back to the best balanced accuracy and warns you.
 * With few images the result is unstable. Treat it as provisional until it holds
   on a new, independent set.
"""
import argparse
import random
from pathlib import Path

import numpy as np

from . import common


def split_indices(labels, seed: int = 1):
    """Stratified 50/50 split into (fit_idx, test_idx)."""
    rng = random.Random(seed)
    fit, test = [], []
    for cls in (True, False):
        idx = [i for i, l in enumerate(labels) if l == cls]
        rng.shuffle(idx)
        half = len(idx) // 2
        fit += idx[:half]
        test += idx[half:]
    return sorted(fit), sorted(test)


def rates(pred_good, is_good):
    """Good kept as good, bad missed (called good), as fractions."""
    n_good, n_bad = is_good.sum(), (~is_good).sum()
    good_kept = (pred_good & is_good).sum() / max(n_good, 1)
    bad_missed = (pred_good & ~is_good).sum() / max(n_bad, 1)
    return float(good_kept), float(bad_missed)


def search_thresholds(hue, lbp, is_good, max_bad_miss=0.05, hue_step=2, n_lbp=60):
    """Grid search for (hue_min, hue_max, lbp_max). Returns a dict."""
    hue, lbp, is_good = np.asarray(hue, float), np.asarray(lbp, float), np.asarray(is_good, bool)
    lbp_grid = np.unique(np.quantile(lbp, np.linspace(0, 1, n_lbp)))
    lbp_grid = np.append(lbp_grid, lbp.max() + 1.0)        # allow "no texture limit"
    best_ok, best_any = None, None
    for hmin in range(0, 181, hue_step):
        for hmax in range(hmin, 361, hue_step):
            hue_ok = (hue >= hmin) & (hue <= hmax)
            pred = hue_ok[:, None] & (lbp[:, None] < lbp_grid[None, :])      # (n, L)
            good_kept = (pred & is_good[:, None]).sum(0) / max(is_good.sum(), 1)
            bad_missed = (pred & ~is_good[:, None]).sum(0) / max((~is_good).sum(), 1)
            bal = (good_kept + (1 - bad_missed)) / 2
            for j in range(len(lbp_grid)):
                cand = (float(good_kept[j]), float(bad_missed[j]), float(bal[j]), hmin, hmax, float(lbp_grid[j]))
                if best_any is None or cand[2] > best_any[2]:
                    best_any = cand
                if bad_missed[j] <= max_bad_miss and (best_ok is None or (cand[0], cand[2]) > (best_ok[0], best_ok[2])):
                    best_ok = cand
    met = best_ok is not None
    g, m, b, hmin, hmax, lmax = best_ok if met else best_any
    return {"hue_min": hmin, "hue_max": hmax, "lbp_max": round(lmax, 3),
            "fit_good_kept": g, "fit_bad_missed": m, "cap_met": met}


def load_features(folder: Path):
    import cv2
    hue, lbp, labels, names = [], [], [], []
    for label, name in ((True, "good"), (False, "bad")):
        for path in sorted((folder / name).glob("*")):
            if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            bgr = cv2.imread(str(path))
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            h, _ = common.hsi_mean_hue(rgb)
            if h is None:
                print(f"skipped (all pixels masked): {path.name}")
                continue
            hue.append(h)
            lbp.append(common.lbp_variance(rgb))
            labels.append(label)
            names.append(path.name)
    return np.array(hue), np.array(lbp), np.array(labels), names


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--max-bad-miss", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--save", action="store_true", help="write the thresholds to calibration.json")
    args = ap.parse_args()

    hue, lbp, labels, _ = load_features(Path(args.folder))
    n_good, n_bad = int(labels.sum()), int((~labels).sum())
    print(f"{len(labels)} images: {n_good} good, {n_bad} bad")
    if min(n_good, n_bad) < 20:
        print("WARNING: fewer than 20 images in a class; results will be unstable.")

    for name, arr in (("hue (deg)", hue), ("LBP variance", lbp)):
        for cls, tag in ((True, "good"), (False, "bad ")):
            q = np.percentile(arr[labels == cls], [5, 25, 50, 75, 95])
            print(f"  {name:13s} {tag}: p5 {q[0]:7.2f}  p25 {q[1]:7.2f}  median {q[2]:7.2f}  p75 {q[3]:7.2f}  p95 {q[4]:7.2f}")

    fit_idx, test_idx = split_indices(labels.tolist(), args.seed)
    res = search_thresholds(hue[fit_idx], lbp[fit_idx], labels[fit_idx], args.max_bad_miss)
    pred = (hue[test_idx] >= res["hue_min"]) & (hue[test_idx] <= res["hue_max"]) & (lbp[test_idx] < res["lbp_max"])
    good_kept, bad_missed = rates(pred, labels[test_idx])

    print(f"\nChosen on the search half: hue {res['hue_min']} to {res['hue_max']} deg, LBP variance < {res['lbp_max']}")
    if not res["cap_met"]:
        print(f"WARNING: no setting kept missed-bad at or below {args.max_bad_miss:.0%}; using best balanced accuracy.")
    print(f"HELD-OUT half ({len(test_idx)} images): good kept {good_kept:.1%}, bad missed {bad_missed:.1%}")
    if args.save:
        if bad_missed > args.max_bad_miss:
            print("Not saved: the held-out bad-miss rate is above your cap. Add more labeled photos and repeat.")
        else:
            common.update("thresholds", {"hue_min": res["hue_min"], "hue_max": res["hue_max"], "lbp_max": res["lbp_max"],
                                         "heldout_good_kept": round(good_kept, 4), "heldout_bad_missed": round(bad_missed, 4),
                                         "n_images": len(labels)})


if __name__ == "__main__":
    main()
