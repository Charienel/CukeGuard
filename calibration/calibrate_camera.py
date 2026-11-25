"""Measure image quality from the USB/UVC webcam without assuming camera controls.

Run from the repo root:
    python -m calibration.calibrate_camera --check-channels
    python -m calibration.calibrate_camera --green-ref

Set focus and any supported exposure/white-balance controls using the webcam's
own controls before running this diagnostic. The script never opens the
browser camera or applies device-specific webcam controls.
"""
import argparse
import time

from . import common, hw


def balance_error_pct(rgb_means) -> float:
    """How far the channels of a grey card differ, in % of their mean."""
    mean = sum(rgb_means) / 3.0
    return (max(rgb_means) - min(rgb_means)) / mean * 100.0 if mean else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-channels", action="store_true")
    ap.add_argument("--green-ref", action="store_true", help="also measure hue repeatability on a green reference")
    ap.add_argument("--frames", type=int, default=10)
    args = ap.parse_args()
    if args.frames < 1:
        ap.error("--frames must be greater than zero")

    with hw.outputs() as out:
        out.led(True)
        time.sleep(2)
        cam = hw.open_camera()
        try:
            if args.check_channels:
                input("Show a clearly RED object to the USB webcam, then press Enter... ")
                img = common.roi(cam.capture_array())
                r, g, b = img.reshape(-1, 3).mean(axis=0)
                print(f"mean R={r:.0f} G={g:.0f} B={b:.0f}; largest channel: {'RGB'[[r, g, b].index(max(r, g, b))]}")
                print("The largest channel should be R. Correct RGB/BGR conversion before scanning if it is not.")
                return

            input(
                "Adjust the USB webcam focus/exposure if needed, place a grey/white "
                "card at the cucumber position, then press Enter... "
            )

            means, brights = [], []
            for _ in range(args.frames):
                r, g, b = common.roi(cam.capture_array()).reshape(-1, 3).mean(axis=0)
                means.append((r, g, b))
                brights.append((r + g + b) / 3)
                time.sleep(0.3)
            avg = tuple(sum(m[i] for m in means) / len(means) for i in range(3))
            bal = balance_error_pct(avg)
            mean_b = sum(brights) / len(brights)
            var_pct = (max(brights) - min(brights)) / mean_b * 100.0 if mean_b else 0.0

            height, width = cam.capture_array().shape[:2]
            print(f"\nUSB/UVC webcam: {width}x{height}, RGB")
            print(f"Card mean RGB: {avg[0]:.0f}, {avg[1]:.0f}, {avg[2]:.0f}")
            print(f"Grey-balance error: {bal:.1f} %   (aim for under 3 %)")
            print(f"Frame-to-frame brightness variation: {var_pct:.1f} %   (aim for under 2 %)")
            if not 90 <= mean_b <= 230:
                print("WARNING: card brightness is poor (too dark or clipped). Adjust the LED distance/brightness and repeat.")

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
                "interface": "USB/UVC", "device": "/dev/video0",
                "resolution": [int(width), int(height)], "channel_order": "RGB",
                "grey_balance_error_pct": round(bal, 2), "brightness_variation_pct": round(var_pct, 2),
                "green_ref_hue_deg": None if hue_mean is None else round(hue_mean, 2),
                "green_ref_hue_sd_deg": None if hue_sd is None else round(hue_sd, 3),
            })
        finally:
            cam.close()


if __name__ == "__main__":
    main()
