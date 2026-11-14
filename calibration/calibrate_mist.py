"""
calibrate_mist.py - measure how much one mist pulse raises humidity and how long
the sensor takes to show it, then recommend a pulse length and a minimum pause.

Run from the repo root (chamber closed, empty of cucumbers, circulation fans as in use):
    python -m calibration.calibrate_mist --pulses 3,6,10

WHY: the humidity reading lags behind the mister. If the controller keeps misting
while it waits for the reading to catch up, humidity overshoots and the cucumbers
get wet. The pause between pulses must be longer than the lag.

Your chamber has no dehumidifier, so RH cannot be lowered quickly. The script
asks you to vent the chamber to below --start-below before each pulse, so the
test happens near the operating range but below saturation. Calibrate sensors first.
"""
import argparse
import csv
import time
from pathlib import Path

from . import common, hw


def analyze_pulse(times, rh, pre_s: float, pulse_s: float) -> dict:
    """Baseline, rise, lag and time-to-peak of one mist pulse (mist on at t = pre_s).

    lag_s = seconds until the reading has risen by max(0.3 %RH, 3 noise standard deviations),
    i.e. when the controller would first SEE the pulse working (None if it never does).
    """
    base = [v for t, v in zip(times, rh) if t < pre_s]
    if len(base) < 3:
        raise ValueError("not enough baseline samples")
    baseline = sum(base) / len(base)
    sd = (sum((v - baseline) ** 2 for v in base) / len(base)) ** 0.5
    threshold = baseline + max(0.3, 3 * sd)
    after = [(t, v) for t, v in zip(times, rh) if t >= pre_s]
    lag = next((t - pre_s for t, v in after if v >= threshold), None)
    peak_t, peak = max(after, key=lambda p: p[1])
    return {"pulse_s": pulse_s, "baseline": baseline, "peak": peak, "rise": peak - baseline,
            "lag_s": lag, "time_to_peak_s": peak_t - pre_s}


def recommend(results, band_pct: float, max_rise_frac: float = 0.5) -> dict:
    """Pick the longest pulse whose rise is at most half the control band, and a pause
    long enough for the reading to reach its peak."""
    usable = [r for r in results if r["lag_s"] is not None]
    if not usable:
        raise ValueError("no pulse produced a measurable rise; check the mister and water level")
    ok = [r for r in usable if r["rise"] <= band_pct * max_rise_frac]
    chosen = max(ok, key=lambda r: r["pulse_s"]) if ok else min(usable, key=lambda r: r["pulse_s"])
    return {"pulse_on_s": chosen["pulse_s"],
            "min_off_s": max(20, round(chosen["time_to_peak_s"] - chosen["pulse_s"] + 15)),
            "lag_s": round(chosen["lag_s"], 1),
            "rise_pct_at_chosen_pulse": round(chosen["rise"], 2),
            "band_pct": band_pct,
            "all_within_band": bool(ok)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pulses", default="3,6,10", help="pulse lengths in seconds, comma separated")
    ap.add_argument("--pre", type=int, default=60, help="baseline seconds before each pulse")
    ap.add_argument("--observe", type=int, default=300, help="seconds to watch after each pulse")
    ap.add_argument("--band", type=float, default=2.0, help="width of your humidity control band in %RH")
    ap.add_argument("--start-below", type=float, default=88.0, help="vent the chamber below this RH before each pulse")
    args = ap.parse_args()
    pulses = [float(p) for p in args.pulses.split(",")]

    cal = common.load()
    sensors = hw.Sensors()
    logdir = Path(__file__).with_name("logs")
    logdir.mkdir(exist_ok=True)
    results = []

    with hw.outputs() as out:
        for pulse in pulses:
            while sensors.mean_rh(cal) > args.start_below:
                input(f"RH is {sensors.mean_rh(cal):.1f} % (above {args.start_below}). Vent the chamber, wait, press Enter... ")
            times, rh = [], []
            t0 = time.monotonic()
            mist_on = False
            aborted = False
            total = args.pre + pulse + args.observe
            while (t := time.monotonic() - t0) < total:
                value = sensors.mean_rh(cal)
                times.append(t)
                rh.append(value)
                if value >= hw.ABS_MAX_RH:
                    print(f"ABORT: RH {value:.1f} % reached the safety limit.")
                    aborted = True
                    break
                should_mist = args.pre <= t < args.pre + pulse
                if should_mist != mist_on:
                    out.mist(should_mist)
                    mist_on = should_mist
                time.sleep(1)
            out.mist(False)
            with open(logdir / f"mist_{int(time.time())}_{pulse:g}s.csv", "w", newline="") as f:
                csv.writer(f).writerows([("t_s", "rh_pct"), *zip(times, rh)])
            if aborted:
                break
            r = analyze_pulse(times, rh, args.pre, pulse)
            results.append(r)
            lag = "none" if r["lag_s"] is None else f"{r['lag_s']:.0f} s"
            print(f"{pulse:g} s pulse: baseline {r['baseline']:.1f} %, rise {r['rise']:.2f} %, "
                  f"lag {lag}, peak after {r['time_to_peak_s']:.0f} s")

    if not results:
        print("No completed pulses; nothing saved.")
        return
    rec = recommend(results, args.band)
    print(f"\nRecommended: pulse {rec['pulse_on_s']:g} s, then pause at least {rec['min_off_s']} s "
          f"(reading lags the mister by about {rec['lag_s']} s).")
    if not rec["all_within_band"]:
        print("Even the shortest pulse raised RH by more than half your band: use a shorter pulse or lower mist output.")
    common.update("mist", {**rec, "rise_by_pulse_s": {f"{r['pulse_s']:g}": round(r["rise"], 2) for r in results}})


if __name__ == "__main__":
    main()
