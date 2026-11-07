"""
baseline.py -- Phase 2: learn what "normal" looks like for each machine.

This file only LEARNS from history. It never reads the live stream and never decides
whether anything is anomalous. detector.py (Phase 3) uses the baseline built here
(agent.py rebuilds it at startup; this script writes baseline.json for inspection).

What Phase 1 taught us, and how it shapes this file:
  * The 7-day history is NOT a clean "normal week": faults are baked into the later days
    (CNC_01 from day 5, CNC_02 on days 6-7, PUMP_03's rpm slides from day 1). So we learn
    only from the OLDEST `reference_days` days (default 4).
  * Median and MAD (robust statistics) are stored next to mean and std, so a one-off
    event inside the window (the conveyor's 72-minute vibration excursion) cannot distort
    the baseline.
  * A sensor that is already moving INSIDE the window (PUMP_03 rpm) is detected
    automatically; its centre then falls back to the nominal value from /machines.
  * History noise is small and independent from reading to reading, while live noise is
    wider and smooth (Phase 1b). These spreads describe HISTORY only. Phase 3 decides how
    to scale thresholds for live data; this file does not guess.

How the baseline will be used (Phase 3): for a live reading of machine M, sensor S at hour
H (UTC), look up the stored centre/spread/percentiles, then compare. Hour-of-day statistics
are stored too. The assignment does not ask for them; they were added because CNC_02's fault
is described as an afternoon problem. The report shows how little they matter in this data.

The daily medians of ALL history days are stored as well. The agent's history health check
(detector.detect_history_shift) compares the newest day's median with normal, to find slow,
multi-day degradation that the live stream cannot show.

Usage:
  python baseline.py                       # fetch history, print report, write baseline.json
  python baseline.py --reference-days 3    # try a different window
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone

from common import (
    DEFAULT_BASE_URL, NOMINAL_KEY, SENSOR_FIELDS, SimulatorError, get_json, load_history,
)

# ---------------------------------------------------------------------------
# Settings (README.md, "Configuration and thresholds", lists every value and its reason)
# ---------------------------------------------------------------------------
# How many of the OLDEST days count as "healthy reference". Evidence: the report's daily
# table shows the first sensor to leave its normal level does so on day 5 (CNC_01).
REFERENCE_DAYS = 4

# Minimum readings before we trust a statistic. Each hour of a 4-day window has 240 readings.
MIN_SAMPLES = 30
# A day needs at least this many readings to count (drops the lone reading at day 7 + 0 s).
MIN_DAY_READINGS = 360

# Floor for the spread, as a fraction of the nominal value, so a perfectly flat sensor can
# never give a spread of 0 (division by zero later). The smallest healthy spread in this data
# is about 0.9% of nominal (rpm), so 0.1% is roughly 9x below it and never active in practice.
MIN_STD_FRACTION = 0.001
ABSOLUTE_MIN_STD = 1e-6          # used when the nominal value itself is 0

# Scale factor that turns MAD into a std-like number for bell-shaped data.
MAD_TO_STD = 1.4826

# "Already drifting" test: if the daily medians inside the window move by more than this
# many spreads, the machine was changing during the reference period.
DRIFT_RATIO_LIMIT = 1.0

# "Mean and median disagree" test for an hour: |mean - median| above this many spreads
# means a few extreme readings are pulling the mean (bursts or a one-off event).
TAIL_DISAGREEMENT = 0.5

# A day counts as "departed from baseline" when its mean is this many spreads from the centre.
DEPARTURE_SPREADS = 3.0


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------
def sig(x, digits=6):
    """
    Round to `digits` SIGNIFICANT figures (not decimal places). Plain round(x, 4) would turn a
    tiny but valid spread such as 1e-06 into 0.0 and bring back the division by zero we are
    protecting against.
    """
    return float(f"{x:.{digits}g}")


def robust_std(values):
    """
    Spread that ignores outliers: 1.4826 * MAD, where MAD = median(|x - median|).
    Up to ~50% of the readings can be extreme before it moves. (For the simulator's flat,
    uniform noise it comes out about 1.3x the ordinary std; that is fine, it is only a scale.)
    """
    centre = statistics.median(values)
    mad = statistics.median(abs(v - centre) for v in values)
    return MAD_TO_STD * mad


def describe(values):
    """
    Summary statistics for a list of numbers, or None if there are too few to trust.
    Percentiles use the 'inclusive' method, i.e. interpolation between actual readings.
    """
    if len(values) < MIN_SAMPLES:
        return None
    cuts = statistics.quantiles(values, n=100, method="inclusive")  # 99 cut points
    stats = {
        "n": len(values),
        "mean": statistics.mean(values),
        "std": statistics.stdev(values),
        "median": statistics.median(values),
        "robust_std": robust_std(values),
        "p01": cuts[0],
        "p05": cuts[4],
        "p95": cuts[94],
        "p99": cuts[98],
    }
    return {key: (value if key == "n" else sig(value)) for key, value in stats.items()}


def day_index(t, start):
    """Which 24-hour block (0, 1, 2, ...) counted from the first reading does `t` fall in?"""
    return int((t - start).total_seconds() // 86400)


# ---------------------------------------------------------------------------
# Building the baseline
# ---------------------------------------------------------------------------
def build_sensor_baseline(field, readings, start, reference_days, nominal):
    """
    Baseline for ONE sensor of ONE machine.
      readings : ALL history as (datetime, reading) pairs, oldest first
      start    : timestamp of the first reading
      nominal  : the machine's nominal value for this sensor, from GET /machines
    """
    window_end = start + timedelta(days=reference_days)
    window = [(t, r[field]) for t, r in readings if t < window_end]
    values = [v for _, v in window]
    if len(values) < MIN_SAMPLES:
        raise ValueError(f"only {len(values)} readings in the reference window for {field}")

    # Group ALL history by day (for the daily table) and the window by hour of day (UTC).
    day_values = {}
    for t, r in readings:
        day_values.setdefault(day_index(t, start), []).append(r[field])
    day_values = {d: v for d, v in day_values.items() if len(v) >= MIN_DAY_READINGS}

    hour_values = {}
    for t, v in window:
        hour_values.setdefault(t.hour, []).append(v)

    # --- spread and floor ----------------------------------------------------
    # Spread = typical WITHIN-day robust spread, so slow changes between days (drift)
    # cannot inflate it.
    window_days = [d for d in sorted(day_values) if d < reference_days]
    within_day_spread = statistics.median(robust_std(day_values[d]) for d in window_days)
    std_floor = max(MIN_STD_FRACTION * abs(nominal), ABSOLUTE_MIN_STD)
    spread = max(within_day_spread, std_floor)  # never zero, so dividing by it is safe

    # --- was the sensor already drifting inside the window? -------------------
    daily_medians = [statistics.median(day_values[d]) for d in window_days]
    drift_ratio = (max(daily_medians) - min(daily_medians)) / spread
    drifting = drift_ratio > DRIFT_RATIO_LIMIT
    # A drifting sensor has no trustworthy "healthy level" in the history, so use nominal.
    centre = nominal if drifting else statistics.median(values)

    # --- hour-of-day statistics -------------------------------------------------
    hourly = {}
    for hour in range(24):
        stats = describe(hour_values.get(hour, []))
        if stats is not None:
            hourly[str(hour)] = stats

    hourly_medians = [s["median"] for s in hourly.values()]
    hourly_median_range = (max(hourly_medians) - min(hourly_medians)) / spread

    # Hours where a few extreme readings drag the mean away from the median.
    tail_hours = [int(h) for h, s in hourly.items() if abs(s["mean"] - s["median"]) > TAIL_DISAGREEMENT * spread]

    # --- first day (1-based) whose mean is far from the window's own mean -----------
    # We compare means with means (not with the median), so a sensor that is permanently
    # bursty (PUMP_03 vibration: mean always above median) is not flagged on every day.
    window_mean = statistics.mean(values)
    first_departure_day = None
    for d in sorted(day_values):
        if abs(statistics.mean(day_values[d]) - window_mean) > DEPARTURE_SPREADS * spread:
            first_departure_day = d + 1
            break

    return {
        "nominal": nominal,
        "reference_center": sig(centre),
        "reference_spread": sig(spread),
        "std_floor": sig(std_floor),
        "drifting_in_window": drifting,
        "drift_ratio": round(drift_ratio, 3),
        "hourly_median_range": round(hourly_median_range, 3),
        "tail_hours": tail_hours,
        "first_departure_day": first_departure_day,
        "daily_means": [sig(statistics.mean(day_values[d])) for d in sorted(day_values)],
        # For the history health check: medians, because bursts and one-off events move a
        # daily mean (PUMP_03 vibration: +3 to +5 spreads every day) but not a daily median.
        "daily_days": [d + 1 for d in sorted(day_values)],     # 1-based, aligned with the lists
        "daily_medians": [sig(statistics.median(day_values[d])) for d in sorted(day_values)],
        "overall": describe(values),
        "hourly": hourly,
    }


def build_baseline(histories, nominals, reference_days=REFERENCE_DAYS, source="unknown"):
    """
    histories : {machine_id: [(datetime, reading), ...]}   (oldest first)
    nominals  : {machine_id: {"temperature_C": 72, ...}}   (from GET /machines)
    Returns a plain dict that can be written straight to JSON.
    """
    machines = {}
    first_start = None
    for machine_id, readings in histories.items():
        start = readings[0][0]
        first_start = start if first_start is None else min(first_start, start)
        sensors = {
            field: build_sensor_baseline(field, readings, start, reference_days, nominals[machine_id][field])
            for field in SENSOR_FIELDS
        }
        window_count = sum(1 for t, _ in readings if t < start + timedelta(days=reference_days))
        machines[machine_id] = {
            "reference_start": start.isoformat(),
            "reference_end": (start + timedelta(days=reference_days)).isoformat(),
            "readings_in_window": window_count,
            "sensors": sensors,
        }
    return {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "reference_days": reference_days,
            "hour_of_day_timezone": "UTC",
            "note": "History statistics only. Live noise is wider and smoother; see Phase 1b.",
        },
        "machines": machines,
    }


def build_from_server(base_url, reference_days=REFERENCE_DAYS, progress=None):
    """
    Download /machines and every machine's 7-day history from the simulator, then build the
    baseline. Used by this script's main() and by agent.py at startup, so the agent always
    learns from the same server it is about to monitor.
    progress: optional function that receives short status messages (e.g. print).
    Raises SimulatorError (cannot reach or bad answer), KeyError/TypeError (unexpected shape)
    or ValueError (too little data) with a readable message.
    """
    machines_data = get_json(base_url, "/machines")
    histories = {}
    nominals = {}
    for machine_id in machines_data["machines"]:
        # /machines names sensors temp/vib/rpm/current; translate to our field names.
        nominals[machine_id] = {
            field: machines_data["baselines"][machine_id][NOMINAL_KEY[field]] for field in SENSOR_FIELDS
        }
        readings, n_bad = load_history(base_url, machine_id)
        if n_bad and progress:
            progress(f"WARNING: {machine_id}: {n_bad} malformed history readings were skipped")
        if not readings:
            raise SimulatorError(f"{machine_id}: no usable history readings")
        histories[machine_id] = readings
        if progress:
            progress(f"loaded {len(readings)} readings for {machine_id}")
    return build_baseline(histories, nominals, reference_days, source=base_url)


# ---------------------------------------------------------------------------
# Saving, loading, and the lookup detectors will use
# ---------------------------------------------------------------------------
def save_baseline(baseline, path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(baseline, handle, indent=1)


def load_baseline(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_reference(baseline, machine_id, sensor, hour):
    """
    Stats for one machine/sensor/UTC hour. Falls back to the all-hours stats if that
    hour has too little data. Raises KeyError for an unknown machine or sensor.
    """
    sensor_data = baseline["machines"][machine_id]["sensors"][sensor]
    return sensor_data["hourly"].get(str(hour)) or sensor_data["overall"]


# ---------------------------------------------------------------------------
# Report (so the numbers can be inspected and explained)
# ---------------------------------------------------------------------------
def format_hours(hours):
    """[4, 5, 9, 10, 11] -> '4-5,9-11' (compact ranges); all 24 -> 'all 24'."""
    if not hours:
        return "-"
    if len(hours) >= 24:
        return "all 24"
    hours = sorted(hours)
    parts = []
    first = last = hours[0]
    for h in hours[1:]:
        if h == last + 1:
            last = h
            continue
        parts.append(f"{first}-{last}" if last > first else str(first))
        first = last = h
    parts.append(f"{first}-{last}" if last > first else str(first))
    return ",".join(parts)


def print_machine_report(machine_id, machine, reference_days):
    sensors = machine["sensors"]
    print("\n" + "-" * 78)
    print(f"{machine_id}   reference window {machine['reference_start'][:16]} -> "
          f"{machine['reference_end'][:16]} UTC  ({machine['readings_in_window']} readings)")

    print(f"\n  {'sensor':<16}{'nominal':>9}{'centre':>10}{'spread':>9}{'p05':>10}{'p95':>10}{'p99':>10}")
    for field in SENSOR_FIELDS:
        s = sensors[field]
        o = s["overall"]
        print(f"  {field:<16}{s['nominal']:>9.2f}{s['reference_center']:>10.2f}{s['reference_spread']:>9.3f}"
              f"{o['p05']:>10.2f}{o['p95']:>10.2f}{o['p99']:>10.2f}")

    print(f"\n  {'sensor':<16}{'drift ratio':>12}{'hour range':>12}  {'mean != median hours':<22}{'first departure':>16}")
    for field in SENSOR_FIELDS:
        s = sensors[field]
        flag = " DRIFTING" if s["drifting_in_window"] else ""
        departure = f"day {s['first_departure_day']}" if s["first_departure_day"] else "none"
        print(f"  {field:<16}{s['drift_ratio']:>12.2f}{s['hourly_median_range']:>12.2f}  "
              f"{format_hours(s['tail_hours']):<22}{departure:>16}{flag}")

    print("\n  daily means ('*' = day is inside the reference window)")
    print(f"  {'day':<7}" + "".join(f"{field:>16}" for field in SENSOR_FIELDS))
    n_days = len(sensors[SENSOR_FIELDS[0]]["daily_means"])
    for d in range(n_days):
        mark = "*" if d < reference_days else " "
        print(f"  {d + 1:<3}{mark:<4}" + "".join(f"{sensors[f]['daily_means'][d]:>16.2f}" for f in SENSOR_FIELDS))


def print_findings(baseline):
    """Plain-language summary computed from the numbers above."""
    reference_days = baseline["meta"]["reference_days"]
    print("\n" + "=" * 78)
    print("Findings")
    print("=" * 78)

    departures = []    # (day, machine, sensor) for sensors that were NOT already drifting
    drifting = []
    swings = []        # (hourly_median_range, machine, sensor)
    for machine_id, machine in baseline["machines"].items():
        for field, s in machine["sensors"].items():
            swings.append((s["hourly_median_range"], machine_id, field))
            if s["drifting_in_window"]:
                drifting.append(f"{machine_id} {field} (drift ratio {s['drift_ratio']:.1f}, centre set to nominal {s['nominal']})")
            elif s["first_departure_day"]:
                departures.append((s["first_departure_day"], machine_id, field))

    if drifting:
        print("Already drifting inside the window (centre taken from /machines instead):")
        for line in drifting:
            print(f"  - {line}")

    if departures:
        day, machine_id, field = min(departures)
        print(f"Earliest departure from baseline among the other sensors: day {day} ({machine_id} {field}).")
        if day <= reference_days:
            print(f"  !! WARNING: the reference window ({reference_days} days) reaches that day, "
                  f"so it may contain faulty readings. Use a shorter --reference-days.")
        else:
            print(f"  The {reference_days}-day reference window ends before that day, as intended.")

    for machine_id, machine in baseline["machines"].items():
        for field, s in machine["sensors"].items():
            if s["tail_hours"]:
                kind = "every hour (bursts, or a window that includes faults)" if len(s["tail_hours"]) >= 20 else "one-off event"
                print(f"Mean and median disagree: {machine_id} {field} in hours {format_hours(s['tail_hours'])} UTC -> {kind}; "
                      f"use median/robust spread there.")

    biggest = max(swings)
    print(f"Time-of-day effect in the reference window: the largest hour-to-hour swing in medians is "
          f"{biggest[0]:.2f} spreads ({biggest[1]} {biggest[2]}).")
    print("  Values this small mean per-hour baselines barely differ from the all-hours baseline here.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Phase 2: build the historical baseline.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--reference-days", type=int, default=REFERENCE_DAYS,
                        help="how many of the oldest days count as healthy reference")
    parser.add_argument("--out", default="baseline.json", help="where to write the baseline")
    return parser.parse_args()


def main():
    args = parse_args()
    base_url = args.base_url.rstrip("/")

    try:
        baseline = build_from_server(base_url, args.reference_days, progress=print)
    except (SimulatorError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1

    for machine_id, machine in baseline["machines"].items():
        print_machine_report(machine_id, machine, args.reference_days)
    print_findings(baseline)

    save_baseline(baseline, args.out)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())