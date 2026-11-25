"""Tests for the calibration maths. Run from the repo root: pytest calibration/ -q
(No hardware needed; hardware modules are not imported here.)"""
import math
import random
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from calibration import common
from calibration import hw
from calibration.calibrate_camera import balance_error_pct
from calibration.calibrate_cooling import analyze_cooling
from calibration.calibrate_mist import analyze_pulse, recommend
from calibration.calibrate_settle import pick_settle
from calibration.calibrate_stepper import slot_positions, steps_per_mm
from calibration.calibrate_thresholds import rates, search_thresholds, split_indices


def test_linear_fit_recovers_gain_and_offset():
    raw = [0.4, 12.0, 25.3, 30.1]
    ref = [1.02 * x - 0.6 for x in raw]
    gain, offset = common.linear_fit(raw, ref)
    assert gain == pytest.approx(1.02, abs=1e-9)
    assert offset == pytest.approx(-0.6, abs=1e-9)


def test_single_point_gives_offset_only():
    assert common.linear_fit([24.0], [23.5]) == (1.0, -0.5)


def test_identical_raw_points_are_rejected():
    with pytest.raises(ValueError):
        common.linear_fit([20.0, 20.0], [19.0, 25.0])


def test_apply_sensor_cal():
    cal = {"sensors": {"k": {"gain": 1.1, "offset": -2.0}}}
    assert common.apply_sensor_cal(cal, "k", 10.0) == pytest.approx(9.0)
    assert common.apply_sensor_cal(cal, "missing", 10.0) == 10.0
    assert common.apply_sensor_cal({}, "k", 10.0) == 10.0


def test_balance_error():
    assert balance_error_pct((100, 100, 100)) == 0
    assert balance_error_pct((90, 100, 110)) == pytest.approx(20.0)


def _mist_trace(rise, lag, peak_after, pre=60, pulse=6, total=400, seed=0):
    rng = random.Random(seed)
    t = list(range(total))
    rh = []
    for s in t:
        x = s - pre
        if x < lag:
            v = 85.0
        elif x < peak_after:
            v = 85.0 + rise * (x - lag) / (peak_after - lag)
        else:
            v = 85.0 + rise - 0.002 * (x - peak_after)
        rh.append(v + rng.uniform(-0.05, 0.05))
    return t, rh


def test_mist_pulse_analysis_and_recommendation():
    t, rh = _mist_trace(rise=0.9, lag=15, peak_after=60)
    r = analyze_pulse(t, rh, pre_s=60, pulse_s=6)
    assert r["rise"] == pytest.approx(0.9, abs=0.15)
    assert r["lag_s"] == pytest.approx(30, abs=3)      # 15 s dead time + ~15 s to rise 0.3 %RH
    assert r["time_to_peak_s"] == pytest.approx(60, abs=3)
    t2, rh2 = _mist_trace(rise=2.6, lag=15, peak_after=70, pulse=10)
    r2 = analyze_pulse(t2, rh2, 60, 10)
    rec = recommend([r, r2], band_pct=2.0)
    assert rec["pulse_on_s"] == 6 and rec["all_within_band"] is True
    assert rec["min_off_s"] >= 60 - 6


def test_mist_no_response_is_reported():
    t = list(range(300))
    r = analyze_pulse(t, [85.0] * 300, 60, 6)
    assert r["lag_s"] is None
    with pytest.raises(ValueError):
        recommend([r], 2.0)


def test_cooling_analysis():
    t_on, t_off = 60.0, 600.0
    times, temps = [], []
    for s in range(0, 1100, 2):
        if s < t_on:
            v = 13.0
        elif s < t_on + 30:
            v = 13.0
        elif s <= t_off:
            v = 13.0 - 0.01 * (s - t_on - 30)
        elif s < t_off + 120:
            v = 13.0 - 0.01 * (t_off - t_on - 30) - 0.3 * (1 - math.exp(-(s - t_off) / 40))
        else:
            v = (13.0 - 0.01 * (t_off - t_on - 30) - 0.3) + 0.002 * (s - t_off - 120)
        times.append(float(s))
        temps.append(v)
    r = analyze_cooling(times, temps, t_on, t_off)
    assert r["cool_rate_c_per_min"] == pytest.approx(0.6, abs=0.05)       # 0.01 C/s
    assert r["lag_s"] == pytest.approx(50, abs=6)                          # 30 s dead time + 0.2 C
    assert r["coast_drop_c"] == pytest.approx(0.3, abs=0.06)
    assert r["warm_rate_c_per_min"] == pytest.approx(0.12, abs=0.03)       # 0.002 C/s


def test_stepper_helpers():
    assert steps_per_mm(2000, [49.8, 50.1, 50.1]) == pytest.approx(40.0, abs=0.1)
    assert slot_positions(3, 50, 100) == [50, 150, 250]


def test_nc_limit_switch_is_open_when_gpio_button_reads_pressed(monkeypatch):
    buttons = []

    class FakeButton:
        def __init__(self, pin, pull_up, bounce_time):
            self.pin = pin
            self.is_pressed = True
            self.closed = False
            buttons.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "gpiozero", SimpleNamespace(Button=FakeButton))

    switch = hw._NCSwitch(5)

    assert switch.is_pressed is False
    buttons[0].is_pressed = False
    assert switch.is_pressed is True
    switch.close()
    assert buttons[0].closed is True


def test_pick_settle():
    delays = [0, 0.25, 0.5, 1, 2]
    sharp = [20, 60, 130, 148, 150]
    assert pick_settle(delays, sharp) == 1.5      # first >= 95 % of best is 1 s, plus 0.5 s
    assert pick_settle([0, 1], [10, 10]) == 0.5


def test_split_is_stratified_and_disjoint():
    labels = [True] * 20 + [False] * 40
    fit, test = split_indices(labels, seed=3)
    assert not set(fit) & set(test)
    assert sum(labels[i] for i in fit) == 10 and sum(not labels[i] for i in fit) == 20


def test_threshold_search_finds_separating_rule_and_generalises():
    rng = np.random.default_rng(0)
    # good: hue about 75 (not the manuscript's 90-120), smooth; bad: yellowing or wrinkled
    good_h, good_l = rng.normal(75, 4, 80), rng.normal(5, 1.5, 80)
    bad_h = np.concatenate([rng.normal(50, 6, 70), rng.normal(75, 4, 70)])
    bad_l = np.concatenate([rng.normal(5, 1.5, 70), rng.normal(18, 3, 70)])
    hue = np.concatenate([good_h, bad_h])
    lbp = np.concatenate([good_l, bad_l])
    labels = np.array([True] * 80 + [False] * 140)
    fit, test = split_indices(labels.tolist(), seed=1)
    res = search_thresholds(hue[fit], lbp[fit], labels[fit], max_bad_miss=0.05)
    assert res["cap_met"]
    assert 55 < res["hue_min"] < 75 and 75 < res["hue_max"] < 130 and 5 < res["lbp_max"] < 18
    pred = (hue[test] >= res["hue_min"]) & (hue[test] <= res["hue_max"]) & (lbp[test] < res["lbp_max"])
    kept, missed = rates(pred, labels[test])
    assert kept > 0.85 and missed < 0.10


def test_threshold_search_reports_when_cap_cannot_be_met():
    rng = np.random.default_rng(1)
    hue = rng.normal(80, 10, 200)            # no separation at all
    lbp = rng.normal(10, 3, 200)
    labels = np.array([True] * 100 + [False] * 100)
    res = search_thresholds(hue, lbp, labels, max_bad_miss=0.0)
    assert res["cap_met"] is False or res["fit_bad_missed"] == 0.0


def test_image_features_respond_in_the_right_direction():
    green = np.full((120, 120, 3), [60, 160, 60], dtype="uint8")
    noisy = np.clip(green + np.random.default_rng(0).normal(0, 40, green.shape), 0, 255).astype("uint8")
    yellow = np.full((120, 120, 3), [200, 190, 60], dtype="uint8")
    hg, _ = common.hsi_mean_hue(green)
    hy, _ = common.hsi_mean_hue(yellow)
    assert hy < hg
    assert common.lbp_variance(noisy) > common.lbp_variance(green)
    assert common.sharpness(noisy) > common.sharpness(green)
    grey = np.full((50, 50, 3), 128, dtype="uint8")
    assert common.hsi_mean_hue(grey)[0] is None      # fully masked
