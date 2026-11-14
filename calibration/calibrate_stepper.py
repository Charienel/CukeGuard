"""
calibrate_stepper.py - carriage calibration: limit switches, homing, steps per mm
and the capture position of each cucumber slot.

Run from the repo root, in this order, with a ruler or tape measure along the track:
    python -m calibration.calibrate_stepper --limit-test
    python -m calibration.calibrate_stepper --steps-per-mm --test-steps 2000
    python -m calibration.calibrate_stepper --positions --slots 7 --first-mm 50 --spacing-mm 100

SAFETY: keep the power switch within reach. Moves stop if the far limit trips.
Start with the carriage in the middle of the track, away from both ends.
STEP_STYLE in hw.py must match what the production code uses, because
steps-per-mm is only valid for that style.
"""
import argparse
import statistics
import time

from . import common, hw


def steps_per_mm(steps_commanded: int, measured_mm: list) -> float:
    """Steps per millimetre from repeated runs of the same commanded move."""
    return steps_commanded / statistics.fmean(measured_mm)


def slot_positions(slots: int, first_mm: float, spacing_mm: float) -> list:
    return [round(first_mm + i * spacing_mm, 2) for i in range(slots)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit-test", action="store_true")
    ap.add_argument("--steps-per-mm", action="store_true")
    ap.add_argument("--positions", action="store_true")
    ap.add_argument("--test-steps", type=int, default=2000)
    ap.add_argument("--delay", type=float, default=0.005, help="seconds between steps (slower = safer)")
    ap.add_argument("--slots", type=int, default=7)
    ap.add_argument("--first-mm", type=float, default=50.0, help="distance from home to the first cucumber")
    ap.add_argument("--spacing-mm", type=float, default=100.0)
    ap.add_argument("--max-home-steps", type=int, default=20000)
    args = ap.parse_args()

    limits = hw.Limits()

    if args.limit_test:
        print("Press each limit switch by hand. Ctrl+C to stop.")
        try:
            while True:
                print(f"\rhome: {'PRESSED' if limits.home.is_pressed else 'open   '}   "
                      f"far: {'PRESSED' if limits.far.is_pressed else 'open   '}", end="", flush=True)
                time.sleep(0.1)
        except KeyboardInterrupt:
            print()
        return

    stepper = hw.Stepper()
    away = hw.HOME_IS_BACKWARD   # direction flag that moves AWAY from home
    toward = not away
    try:
        if args.steps_per_mm:
            runs, runs_fast = [], []
            for i in range(3):
                input(f"\nRun {i + 1}/3: mark the carriage position A, then press Enter to move {args.test_steps} steps forward... ")
                taken = stepper.step(args.test_steps, away, args.delay, stop_if=lambda: limits.far.is_pressed)
                if taken < args.test_steps:
                    print("Far limit tripped; move the carriage back toward home and repeat.")
                    return
                runs.append(float(input("Measured travel in mm: ")))
                stepper.step(args.test_steps, toward, args.delay, stop_if=lambda: limits.home.is_pressed)
            spm = steps_per_mm(args.test_steps, runs)
            spread = (max(runs) - min(runs)) / statistics.fmean(runs) * 100
            print(f"\nSteps per mm: {spm:.3f} (spread between runs {spread:.1f} %)")
            input(f"Speed check: mark position A, press Enter to repeat at HALF the delay ({args.delay / 2:.4f} s)... ")
            stepper.step(args.test_steps, away, args.delay / 2, stop_if=lambda: limits.far.is_pressed)
            fast = float(input("Measured travel in mm: "))
            loss = (statistics.fmean(runs) - fast) / statistics.fmean(runs) * 100
            if abs(loss) > 1.0:
                print(f"WARNING: travel changed {loss:.1f} % at the higher speed: steps are being missed. "
                      "Use the slower delay, or raise the motor current limit.")
            stepper.step(args.test_steps, toward, args.delay, stop_if=lambda: limits.home.is_pressed)
            common.update("carriage", {"steps_per_mm": round(spm, 4), "step_style": hw.STEP_STYLE,
                                       "step_delay_s": args.delay, "run_spread_pct": round(spread, 2)})

        if args.positions:
            cal = common.load().get("carriage", {})
            if "steps_per_mm" not in cal:
                print("Run --steps-per-mm first.")
                return
            print("Homing toward the home switch...")
            taken = stepper.step(args.max_home_steps, toward, args.delay, stop_if=lambda: limits.home.is_pressed)
            if not limits.home.is_pressed:
                print(f"Home switch not reached in {taken} steps. Check the switch and direction (HOME_IS_BACKWARD).")
                return
            mm = slot_positions(args.slots, args.first_mm, args.spacing_mm)
            steps = [round(p * cal["steps_per_mm"]) for p in mm]
            print("Slot positions from home (mm):", mm)
            for i, n in enumerate(steps, 1):
                input(f"Press Enter to move to slot {i} ({mm[i - 1]} mm); check the camera is centred on the cucumber... ")
                stepper.step(n - (steps[i - 2] if i > 1 else 0), away, args.delay,
                             stop_if=lambda: limits.far.is_pressed)
            common.update("carriage", {"slots": args.slots, "positions_mm": mm, "positions_steps": steps,
                                       "home_direction": "backward" if hw.HOME_IS_BACKWARD else "forward"})
    finally:
        stepper.release()


if __name__ == "__main__":
    main()
