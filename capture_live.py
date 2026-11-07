"""
capture_live.py -- Phase 1b: record a few minutes of the LIVE streams to CSV.

Why: the 7-day history and the live stream come from DIFFERENT code in the
simulator, so we must measure what live data really looks like before we build
baselines or detectors. This script only records and summarises. No detection.

IMPORTANT: the simulator advances a machine's state once for every connected
client, so extra connections (e.g. the browser dashboard tab) speed the machine
up and split the readings between clients. Close the dashboard tab and any
other stream before running this. This script opens exactly ONE connection
per machine.

Usage:
  python capture_live.py                  # 360 seconds, all four machines at once
  python capture_live.py --seconds 30     # quick trial run

Output: data/live_<machine_id>.csv (one row per reading) plus a summary printout.
Needs explore.py in the same folder (we reuse its stream reader and validation).
"""

import argparse
import csv
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from explore import (
    ALL_FIELDS, DEFAULT_BASE_URL, PROBLEMS, SENSOR_FIELDS,
    get_json, header, read_stream,
)


def save_csv(path, readings):
    """Write readings to a CSV file with one column per field."""
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALL_FIELDS)
        writer.writeheader()
        for reading in readings:
            writer.writerow({field: reading[field] for field in ALL_FIELDS})


def print_summary(machine_id, readings):
    """Print min / mean / stdev / max per sensor for the captured readings."""
    print(f"\n  {machine_id}: {len(readings)} readings, "
          f"{readings[0]['timestamp']} -> {readings[-1]['timestamp']}")
    print(f"  status counts: {dict(Counter(r['status'] for r in readings))}")
    print(f"  {'sensor':<16}{'min':>10}{'mean':>10}{'stdev':>10}{'max':>10}")
    for field in SENSOR_FIELDS:
        values = [r[field] for r in readings]
        spread = statistics.stdev(values) if len(values) > 1 else 0.0
        print(f"  {field:<16}{min(values):>10.2f}{statistics.mean(values):>10.2f}"
              f"{spread:>10.2f}{max(values):>10.2f}")


def main():
    parser = argparse.ArgumentParser(description="Record live streams to CSV.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--seconds", type=int, default=360,
                        help="readings to record per machine (about 1 per second)")
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    machines_data = get_json(base_url, "/machines")
    if machines_data is None or not machines_data.get("machines"):
        print("Cannot get the machine list. Is the simulator running?")
        return 1
    machines = machines_data["machines"]

    header(f"Recording {args.seconds} readings from {len(machines)} machines at once")
    print("Close the browser dashboard first. Ctrl+C may take a few seconds to stop.")

    # One thread per machine: each thread holds its own stream connection, so the
    # four streams are read at the same time instead of one after another.
    with ThreadPoolExecutor(max_workers=len(machines)) as pool:
        futures = {m: pool.submit(read_stream, base_url, m, args.seconds) for m in machines}
        results = {m: future.result() for m, future in futures.items()}

    header("Summary of the live capture")
    for machine_id in machines:
        readings = results[machine_id]
        if not readings:
            print(f"\n  {machine_id}: no valid readings captured")
            continue
        path = Path(args.out_dir) / f"live_{machine_id}.csv"
        save_csv(path, readings)
        print_summary(machine_id, readings)
        print(f"  saved {path}")

    if PROBLEMS:
        print(f"\n{len(PROBLEMS)} problem(s):")
        for problem in PROBLEMS:
            print(f"  - {problem}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())