"""
calibrate_sensors.py - two-point (or more) calibration of the SHT41 and DS18B20
against a trusted reference. Writes gain/offset per sensor to calibration.json.

Run from the repo root:
    python -m calibration.calibrate_sensors --quantity temp
    python -m calibration.calibrate_sensors --quantity rh --settle 0

Your calibration is only as good as the reference. Use a reference thermometer
and hygrometer with a known accuracy and write that accuracy in the thesis.

TEMPERATURE: use at least two well-separated points, e.g. an ice-water slurry
(0 C, stirred, probe suspended and not touching the glass) and room temperature
or a ~30 C water bath. Only the WATERPROOF DS18B20 may go in the water. Do NOT
put the SHT31-D board in water: compare it in still air next to the reference
thermometer instead (use an insulated box so the air is stable).

HUMIDITY: the usual low-cost method is a sealed jar over a saturated salt
slurry, which holds a known RH (about 75 % for sodium chloride and about 97 %
for potassium sulfate at 25 C; check a published table for your temperature).
Keep the sensor above the slurry, never touching it. Equilibrium takes hours,
so wait first and then run with --settle 0.
"""
import argparse
import time

from . import common, hw


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quantity", choices=["temp", "rh"], required=True)
    ap.add_argument("--settle", type=int, default=300, help="seconds to wait at each point (default 300)")
    ap.add_argument("--average", type=int, default=60, help="seconds to average at each point (default 60)")
    args = ap.parse_args()

    sensors = hw.Sensors()
    readers = {}
    if args.quantity == "temp":
        for i in range(sensors.sht_count):
            readers[f"sht41_{i}_temp"] = lambda i=i: sensors.sht_temp(i)
        if sensors.ds is not None:
            readers["ds18b20_temp"] = sensors.ds_temp
        unit, max_sd, max_offset = "C", 0.15, 2.0
    else:
        for i in range(sensors.sht_count):
            readers[f"sht41_{i}_rh"] = lambda i=i: sensors.sht_rh(i)
        unit, max_sd, max_offset = "%RH", 0.8, 8.0

    points = {key: ([], []) for key in readers}
    print(f"Sensors found: {', '.join(readers)}")
    while True:
        text = input(f"\nReference value in {unit} (blank = finish): ").strip()
        if not text:
            break
        ref = float(text)
        if args.settle:
            print(f"Waiting {args.settle} s for the sensors to settle...")
            time.sleep(args.settle)
        for key, read in readers.items():
            mean, sd, n = common.average_readings(read, args.average)
            stable = sd <= max_sd
            note = "" if stable else "   <-- unstable, NOT recorded; wait longer and repeat this point"
            print(f"  {key}: raw {mean:.2f} (sd {sd:.2f}, n={n}) vs reference {ref:.2f}{note}")
            if stable:
                points[key][0].append(mean)
                points[key][1].append(ref)

    results = {}
    for key, (raw, ref) in points.items():
        if not raw:
            continue
        gain, offset = common.linear_fit(raw, ref)
        warn = ""
        if len(raw) == 1:
            warn = "  (single point: offset only; add a second point to fit the gain)"
        if not 0.9 <= gain <= 1.1 or abs(offset) > max_offset:
            warn += "  <-- SUSPICIOUS: check wiring, the reference, and the settling time"
        print(f"{key}: corrected = {gain:.4f} * raw + {offset:.3f}  from {len(raw)} point(s){warn}")
        results[key] = {"gain": round(gain, 5), "offset": round(offset, 4), "points": len(raw)}

    if results:
        common.update("sensors", results)
    else:
        print("Nothing recorded; calibration.json not changed.")


if __name__ == "__main__":
    main()
