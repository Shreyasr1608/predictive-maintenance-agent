"""
try_detector.py -- replay the recorded live CSVs through the detectors.

A quick look at the detectors on real data, NOT the test suite (that is tests/). It needs:
  * baseline.json         (from `python baseline.py`)
  * data/live_<machine>.csv   (from `python capture_live.py`)

It prints, per machine:
  * how often each sensor produced a spike signal,
  * how many 5-minute drift windows triggered, and the largest change seen (in spreads),
  * how many 2-minute pattern windows triggered, per pattern (and which sensor pair, if any).
"""

import argparse
import csv
import sys
from pathlib import Path

from baseline import load_baseline
from common import SENSOR_FIELDS, clean_readings
from detector import (
    DRIFT_WINDOW_S, PATTERN_WINDOW_S, detect_drift, detect_patterns, detect_spikes,
)

EVALUATE_EVERY = 5  # evaluate drift every 5 readings (about every 5 seconds)


def load_csv(path):
    """Read a capture file. CSV gives text, so convert the sensor columns back to numbers."""
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for field in SENSOR_FIELDS:
            row[field] = float(row[field])
    return rows


def main():
    parser = argparse.ArgumentParser(description="Replay live CSVs through the detectors.")
    parser.add_argument("--baseline", default="baseline.json")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    try:
        baseline = load_baseline(args.baseline)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read {args.baseline}: {exc}")
        return 1

    for machine_id in baseline["machines"]:
        path = Path(args.data_dir) / f"live_{machine_id}.csv"
        if not path.exists():
            print(f"\n{machine_id}: {path} not found, skipping")
            continue
        pairs, n_bad = clean_readings(load_csv(path))
        if n_bad:
            print(f"\n{machine_id}: WARNING {n_bad} malformed rows skipped")

        # --- spike detector: one reading at a time ---
        spike_hits = {sensor: 0 for sensor in SENSOR_FIELDS}
        for _, reading in pairs:
            for signal in detect_spikes(machine_id, reading, baseline):
                if signal.triggered:
                    spike_hits[signal.sensor] += 1

        # --- drift detector: a growing window, evaluated every few seconds ---
        windows = triggered = 0
        largest = {sensor: float("-inf") for sensor in SENSOR_FIELDS}
        for end in range(0, len(pairs), EVALUATE_EVERY):
            signals = detect_drift(machine_id, pairs[: end + 1], baseline)
            if not signals:
                continue  # not enough data yet (an empty list means "not evaluated")
            windows += 1
            triggered += any(s.triggered for s in signals)
            for s in signals:
                largest[s.sensor] = max(largest[s.sensor], s.evidence["change_spreads"])

        # --- pattern detector: a growing window, evaluated every few seconds ---
        pattern_windows = 0
        pattern_hits = {}
        pair_hits = {}
        for end in range(0, len(pairs), EVALUATE_EVERY):
            signals = detect_patterns(machine_id, pairs[: end + 1], baseline)
            if not signals:
                continue
            pattern_windows += 1
            for s in signals:
                name = s.evidence["pattern"]
                pattern_hits[name] = pattern_hits.get(name, 0) + s.triggered
                if s.triggered and name == "co_elevation":
                    pair = s.evidence["dominant_pair"]
                    pair_hits[pair] = pair_hits.get(pair, 0) + 1

        n = len(pairs)
        print(f"\n{machine_id}: {n} readings")
        print("  spike signals (% of readings): "
              + "  ".join(f"{s[:11]}={100 * spike_hits[s] / n:.0f}%" for s in SENSOR_FIELDS))
        if windows:
            print(f"  drift: {triggered} of {windows} windows triggered ({DRIFT_WINDOW_S} s windows); "
                  "largest change in spreads: "
                  + "  ".join(f"{s[:11]}={largest[s]:.1f}" for s in SENSOR_FIELDS))
        else:
            print(f"  drift: capture shorter than {DRIFT_WINDOW_S} s, nothing evaluated")
        if pattern_windows:
            print(f"  patterns ({PATTERN_WINDOW_S} s windows, {pattern_windows} evaluated): "
                  + "  ".join(f"{name}={hits}" for name, hits in pattern_hits.items())
                  + (f"   co-elevation pairs: {pair_hits}" if pair_hits else ""))
        else:
            print(f"  patterns: capture shorter than {PATTERN_WINDOW_S} s, nothing evaluated")
    return 0


if __name__ == "__main__":
    sys.exit(main())