"""
calibrate_settle.py - how long the carriage must wait after it stops before the
camera captures a sharp image (vibration blurs early frames).

Run from the repo root after the stepper and camera are calibrated:
    python -m calibration.calibrate_settle --move-mm 100

It moves one slot, waits each delay, captures frames and measures sharpness. The
recommended wait is the first delay that reaches 95 % of the best sharpness, plus
a 0.5 s margin. Put a printed text target (or a cucumber) in view.
"""
import argparse
import time

from . import common, hw


def pick_settle(delays, sharpness_values, fraction: float = 0.95, margin_s: float = 0.5) -> float:
    """First delay whose sharpness reaches `fraction` of the best, plus a margin."""
    best = max(sharpness_values)
    for d, s in zip(delays, sharpness_values):
        if s >= fraction * best:
            return round(d + margin_s, 2)
    return round(max(delays) + margin_s, 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--move-mm", type=float, default=100.0)
    ap.add_argument("--delays", default="0,0.25,0.5,1,1.5,2,3")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()
    delays = [float(d) for d in args.delays.split(",")]

    cal = common.load()
    carriage = cal.get("carriage", {})
    if "steps_per_mm" not in carriage:
        print("Run calibrate_stepper --steps-per-mm first.")
        return
    n = round(args.move_mm * carriage["steps_per_mm"])
    delay_s = carriage.get("step_delay_s", 0.005)
    limits = hw.Limits()
    stepper = hw.Stepper()
    results = {d: [] for d in delays}

    with hw.outputs() as out:
        out.led(True)
        cam = hw.open_camera()
        try:
            away = hw.HOME_IS_BACKWARD
            for _ in range(args.rounds):
                for d in delays:
                    for direction in (away, not away):
                        stepper.step(n, direction, delay_s,
                                     stop_if=lambda: limits.far.is_pressed if direction == away else limits.home.is_pressed)
                        time.sleep(d)
                        results[d].append(sum(common.sharpness(cam.capture_array()) for _ in range(3)) / 3)
                        time.sleep(2.0)    # let it fully settle before the next trial
        finally:
            stepper.release()
            cam.close()

    mean = [sum(results[d]) / len(results[d]) for d in delays]
    for d, s in zip(delays, mean):
        print(f"wait {d:4.2f} s -> sharpness {s:9.1f}")
    settle = pick_settle(delays, mean)
    print(f"\nRecommended settle time after each move: {settle} s")
    common.update("carriage", {"settle_s": settle})


if __name__ == "__main__":
    main()
