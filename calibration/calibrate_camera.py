"""
calibrate_camera.py - lock white balance, exposure and focus so every capture
looks the same. Your HSI hue stage depends on colour, so auto white balance and
auto exposure must be OFF in production.

Run from the repo root:
    python -m calibration.calibrate_camera --check-channels          # once, first
    python -m calibration.calibrate_camera --distance-cm 25          # Camera Module 3
    python -m calibration.calibrate_camera --focus-sweep             # Camera Module 3, find best focus
    python -m calibration.calibrate_camera --green-ref               # add a hue repeatability check

Procedure:
 1. --check-channels: hold a clearly RED object in front of the camera. The
    largest channel must be R. If it says B, the array is BGR and hue would be wrong.
 2. Place a neutral GREY (or white) card at the cucumber position so it fills the
    centre of the frame. The LED strip is switched on by the script.
 3. The script lets auto white balance and exposure converge on the card, then
    locks them and saves the values.
 4. It re-captures the card and reports how neutral and how repeatable it is.
Use the saved values in production with common.camera_controls_from().
"""
import argparse
import time

from . import common, hw


def balance_error_pct(rgb_means) -> float:
    """How far the three channels of a grey card differ, in % of their mean (0 = perfectly neutral)."""
    mean = sum(rgb_means) / 3.0
    return (max(rgb_means) - min(rgb_means)) / mean * 100.0 if mean else 0.0


def focus_sweep(cam) -> float:
    """Camera Module 3 only: try lens positions and return the one with the sharpest image."""
    print("Focus sweep: keep a printed text/chart target at the cucumber distance.")
    best_pos, best_sharp = None, -1.0
    for pos in [x / 2 for x in range(1, 21)]:   # 0.5 .. 10 dioptres
        cam.set_controls({"AfMode": 0, "LensPosition": pos})
        time.sleep(0.6)
        s = sum(common.sharpness(cam.capture_array()) for _ in range(3)) / 3
        print(f"  lens position {pos:4.1f}: sharpness {s:9.1f}")
        if s > best_sharp:
            best_pos, best_sharp = pos, s
    print(f"Sharpest at lens position {best_pos} (about {100.0 / best_pos:.0f} cm).")
    return best_pos


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-channels", action="store_true")
    ap.add_argument("--distance-cm", type=float, help="camera-to-cucumber distance (Camera Module 3)")
    ap.add_argument("--focus-sweep", action="store_true")
    ap.add_argument("--green-ref", action="store_true", help="also measure hue repeatability on a green reference")
    ap.add_argument("--frames", type=int, default=10)
    args = ap.parse_args()

    with hw.outputs() as out:
        out.led(True)
        time.sleep(2)
        cam = hw.open_camera()
        try:
            if args.check_channels:
                img = common.roi(cam.capture_array())
                r, g, b = img.reshape(-1, 3).mean(axis=0)
                print(f"mean R={r:.0f} G={g:.0f} B={b:.0f}; largest channel: {'RGB'[[r, g, b].index(max(r, g, b))]}")
                print("If you showed a RED object and the largest is not R, fix the channel order before anything else.")
                return

            input("Place the grey/white card at the cucumber position, then press Enter... ")
            for _ in range(20):                       # let auto white balance / exposure converge
                md = cam.capture_metadata()
                time.sleep(0.15)
            gains = tuple(float(g) for g in md["ColourGains"])
            exposure, again = int(md["ExposureTime"]), float(md["AnalogueGain"])
            cam.set_controls({"AwbEnable": False, "ColourGains": gains, "AeEnable": False,
                              "ExposureTime": exposure, "AnalogueGain": again})
            time.sleep(1.0)

            lens = None
            if "LensPosition" in cam.camera_controls:
                if args.focus_sweep:
                    lens = focus_sweep(cam)
                elif args.distance_cm:
                    lens = round(100.0 / args.distance_cm, 3)   # dioptres = 1 / metres
                if lens is not None:
                    cam.set_controls({"AfMode": 0, "LensPosition": lens})
                    time.sleep(1.0)
            else:
                print("No LensPosition control: fixed-focus camera. Set the focus by hand at the lens.")

            means, brights = [], []
            for _ in range(args.frames):
                r, g, b = common.roi(cam.capture_array()).reshape(-1, 3).mean(axis=0)
                means.append((r, g, b))
                brights.append((r + g + b) / 3)
                time.sleep(0.3)
            avg = tuple(sum(m[i] for m in means) / len(means) for i in range(3))
            bal = balance_error_pct(avg)
            mean_b = sum(brights) / len(brights)
            var_pct = (max(brights) - min(brights)) / mean_b * 100.0

            print(f"\nLocked: colour gains {gains}, exposure {exposure} us, analogue gain {again:.2f}, lens {lens}")
            print(f"Card mean RGB: {avg[0]:.0f}, {avg[1]:.0f}, {avg[2]:.0f}")
            print(f"Grey-balance error: {bal:.1f} %   (aim for under 3 %)")
            print(f"Frame-to-frame brightness variation: {var_pct:.1f} %   (aim for under 2 %)")
            if not 90 <= mean_b <= 230:
                print("WARNING: card brightness is poor (too dark or clipped). Adjust the LED distance/brightness and repeat.")
            if exposure > 20000:
                print("WARNING: exposure over 20 ms; vibration will blur images. Add more light.")

            hue_mean = hue_sd = None
            if args.green_ref:
                input("Place the GREEN reference at the cucumber position, press Enter... ")
                hues = []
                for _ in range(args.frames):
                    h, _s = common.hsi_mean_hue(common.roi(cam.capture_array()))
                    if h is not None:
                        hues.append(h)
                    time.sleep(0.3)
                if hues:
                    hue_mean = sum(hues) / len(hues)
                    hue_sd = (sum((h - hue_mean) ** 2 for h in hues) / len(hues)) ** 0.5
                    print(f"Green reference hue: {hue_mean:.1f} deg, frame-to-frame sd {hue_sd:.2f} deg (aim for under 2)")

            common.update("camera", {
                "colour_gains": list(gains), "exposure_time_us": exposure, "analogue_gain": again,
                "lens_position": lens, "format": "BGR888",
                "grey_balance_error_pct": round(bal, 2), "brightness_variation_pct": round(var_pct, 2),
                "green_ref_hue_deg": None if hue_mean is None else round(hue_mean, 2),
                "green_ref_hue_sd_deg": None if hue_sd is None else round(hue_sd, 3),
            })
        finally:
            cam.stop()
            cam.close()


if __name__ == "__main__":
    main()
