"""
calibrate_cooling.py - step-response test of the Peltier: how fast the chamber
cools, how long before it responds, how far it keeps cooling after switch-off
("coasting") and how fast it warms back up. These numbers set the controller's
switch-off margin so it does not overshoot below the 10 C chilling-injury limit.

Run from the repo root (chamber closed, EMPTY of cucumbers, door shut):
    python -m calibration.calibrate_cooling --duty 60 --drop 2.0

SAFETY: the hot-side fans start before the Peltier and run on after it stops.
Watch the heatsink; stop (Ctrl+C) if it gets too hot to touch comfortably. The
script cuts cooling if the chamber falls below --min-temp (never below 8 C),
after --max-minutes, or when it has dropped --drop degrees.
"""
import argparse
import csv
import time
from pathlib import Path

from . import common, hw


def _slope_per_min(times, values) -> float:
    """Least-squares slope in units per minute."""
    n = len(times)
    if n < 2:
        return 0.0
    mt, mv = sum(times) / n, sum(values) / n
    den = sum((t - mt) ** 2 for t in times)
    return 0.0 if den == 0 else sum((t - mt) * (v - mv) for t, v in zip(times, values)) / den * 60.0


def analyze_cooling(times, temps, t_on: float, t_off: float) -> dict:
    """Response numbers from one step test (Peltier on at t_on, off at t_off)."""
    base = [v for t, v in zip(times, temps) if t < t_on]
    if len(base) < 3:
        raise ValueError("not enough baseline samples")
    baseline = sum(base) / len(base)
    on_phase = [(t, v) for t, v in zip(times, temps) if t_on <= t <= t_off]
    lag = next((t - t_on for t, v in on_phase if v <= baseline - 0.2), None)
    start = t_on + (lag or 0.0)
    fit = [(t, v) for t, v in on_phase if t >= start]
    cool_rate = -_slope_per_min([t for t, _ in fit], [v for _, v in fit]) if len(fit) > 2 else 0.0
    temp_at_off = min(on_phase, key=lambda p: abs(p[0] - t_off))[1]
    after = [(t, v) for t, v in zip(times, temps) if t > t_off]
    coast = max(0.0, temp_at_off - min(v for _, v in after)) if after else 0.0
    tail = [(t, v) for t, v in after if t >= times[-1] - 120]
    warm_rate = _slope_per_min([t for t, _ in tail], [v for _, v in tail]) if len(tail) > 2 else 0.0
    return {"baseline_c": baseline, "lag_s": lag, "cool_rate_c_per_min": cool_rate,
            "coast_drop_c": coast, "warm_rate_c_per_min": warm_rate}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duty", type=float, default=60.0, help="Peltier duty cycle %% for the test")
    ap.add_argument("--drop", type=float, default=2.0, help="stop cooling after this many degrees of drop")
    ap.add_argument("--max-minutes", type=float, default=15.0)
    ap.add_argument("--observe-minutes", type=float, default=8.0, help="how long to watch after switch-off")
    ap.add_argument("--min-temp", type=float, default=10.0, help="cut cooling below this (>= 8)")
    ap.add_argument("--pre", type=int, default=60)
    ap.add_argument("--target-min", type=float, default=10.0, help="bottom of your temperature target range")
    args = ap.parse_args()
    floor = max(args.min_temp, hw.ABS_MIN_TEMP_C)

    cal = common.load()
    sensors = hw.Sensors()
    times, temps = [], []
    t0 = time.monotonic()
    t_on = float(args.pre)
    t_off = None
    why = "completed"

    with hw.outputs() as out:
        baseline = None
        while True:
            t = time.monotonic() - t0
            temp = sensors.mean_temp(cal)
            times.append(t)
            temps.append(temp)
            if t < t_on:
                pass
            elif t_off is None:
                if baseline is None:
                    baseline = sum(temps) / len(temps)
                    out.peltier(args.duty)
                    print(f"Peltier ON at {args.duty:g} % (baseline {baseline:.2f} C)")
                if temp <= floor:
                    why = f"reached floor {floor} C"
                elif baseline - temp >= args.drop:
                    why = f"dropped {args.drop} C"
                elif t - t_on >= args.max_minutes * 60:
                    why = "time limit"
                else:
                    why = None
                if why:
                    out.peltier(0)
                    t_off = t
                    print(f"Peltier OFF at {temp:.2f} C ({why}); watching the recovery...")
            elif t - t_off >= args.observe_minutes * 60:
                break
            time.sleep(2)

    logdir = Path(__file__).with_name("logs")
    logdir.mkdir(exist_ok=True)
    with open(logdir / f"cooling_{int(time.time())}.csv", "w", newline="") as f:
        csv.writer(f).writerows([("t_s", "temp_c"), *zip(times, temps)])

    r = analyze_cooling(times, temps, t_on, t_off)
    margin = round(r["coast_drop_c"] + 0.2, 2)
    print(f"\nLag before cooling starts: {r['lag_s'] and round(r['lag_s'])} s")
    print(f"Cooling rate: {r['cool_rate_c_per_min']:.3f} C/min")
    print(f"Coasting after switch-off: {r['coast_drop_c']:.2f} C further drop")
    print(f"Warm-up rate (Peltier off): {r['warm_rate_c_per_min']:.3f} C/min")
    print(f"Recommended: switch the Peltier OFF when temp <= target_min + {margin} C "
          f"(= {args.target_min + margin:.2f} C) so coasting stays above {args.target_min} C.")
    common.update("cooling", {
        "test_duty_pct": args.duty,
        "lag_s": None if r["lag_s"] is None else round(r["lag_s"], 1),
        "cool_rate_c_per_min": round(r["cool_rate_c_per_min"], 4),
        "coast_drop_c": round(r["coast_drop_c"], 3),
        "warm_rate_c_per_min": round(r["warm_rate_c_per_min"], 4),
        "off_margin_above_min_c": margin,
    })


if __name__ == "__main__":
    main()
