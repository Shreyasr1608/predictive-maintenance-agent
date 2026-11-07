"""
detectors.py -- Phase 3: turn readings into evidence.

Built one detector at a time:
  3A  spike detection     (done)
  3B  drift detection     (done)
  3C  pattern detection   <- added in this version
  3D  Isolation Forest    (optional, last)

Every detector returns Signal objects with the same shape, so the agent (Phase 4) can combine
them. A detector only produces evidence. It never posts alerts and never decides on its own
that a machine is at risk; that is the agent's job.

Design notes (from Phase 1 and 1b):
  * The history is quiet; the live stream is noisier and moves smoothly. If we divided by the
    history spread alone, a healthy conveyor would be flagged on most readings. So every
    comparison uses an EFFECTIVE spread: whichever is larger, the history spread or the
    measured live noise for that sensor (LIVE_NOISE_SD below).
  * Thresholds are sensor-specific and one-directional where that makes sense: high
    temperature, vibration and current are bad; rpm moving either way is suspicious.
"""

import math
import statistics
from dataclasses import dataclass, field

from common import SENSOR_FIELDS, check_reading, parse_timestamp

# ---------------------------------------------------------------------------
# Settings (these move into config.py in Phase 7)
# ---------------------------------------------------------------------------
# Typical live wobble of each sensor, in the sensor's own units. It is the same for every
# machine, because the simulator adds the same absolute noise to all of them.
# Evidence (Phase 1b, 6-minute captures on the near-healthy CNC_01 and CONVEYOR_04):
#   rpm 17.4-17.6, temperature 0.73-1.11, current 0.32-0.40, vibration 0.25 (slow wander).
# The values below are slightly higher than those short-sample numbers, because a 6-minute
# sample underestimates long-run noise when readings move smoothly. They also agree with the
# long-run values implied by the simulator's source code (1.16, 0.145, 23.2, 0.435).
LIVE_NOISE_SD = {
    "temperature_C": 1.2,
    "vibration_mm_s": 0.15,
    "rpm": 23.0,
    "current_A": 0.45,
}

# How many effective spreads away from normal counts as a spike. Chosen above the largest
# deviation seen on the near-healthy CNC_01 live capture (temperature 1.4, rpm 2.2, current 2.7,
# vibration 3.5 spreads), so that machine stays quiet. CAVEAT, measured on the 6-minute
# CONVEYOR_04 capture: its random vibration bursts DO cross the vibration threshold (up to 14.7
# spreads; 22.8% of readings; up to 56 s in a row). So a vibration spike on its own must never
# raise an alert; the agent needs persistence plus a second signal (see Phase 4).
SPIKE_THRESHOLD_Z = {
    "temperature_C": 4.0,
    "vibration_mm_s": 5.0,
    "rpm": 4.0,
    "current_A": 4.0,
}

# Which direction is suspicious: "high", "low" or "both".
SPIKE_DIRECTION = {
    "temperature_C": "high",
    "vibration_mm_s": "high",
    "rpm": "both",
    "current_A": "high",
}

# --- Drift detection settings -------------------------------------------------------------
# Seconds of recent readings the trend is fitted on. The thresholds below were checked by
# simulation for a 300 s window of ~1 reading per second. For a different window length the
# false-alarm rate must be re-checked (longer windows average out more noise).
DRIFT_WINDOW_S = 300
# The window must be at least this full (as a share of DRIFT_WINDOW_S in time) and hold at
# least this many readings before we trust a trend.
DRIFT_MIN_COVERAGE = 0.9
DRIFT_MIN_POINTS = 30
DRIFT_MIN_HALF_POINTS = 10        # readings needed in each half for the consistency check

# The fitted change over the window must be at least this many effective spreads.
# Evidence (simulation of smooth live-like noise, lag-1 autocorrelation 0.98, matching the
# 0.95-1.00 measured in Phase 1b; 10 simulated days, a window evaluated every 5 s):
#   pure noise passes this rule in 0.012% of windows = about 0.3 false episodes per sensor per day;
#   a real ramp is detected in 53% of windows at 6 spreads per 5 min, 81% at 8, 91% at 10.
# Raising it to 6 would remove the false episodes but weaken detection of ramps below 8 spreads.
# A standard "t-test on the slope" is NOT used: it assumes independent readings and would
# call 70% of noise-only windows a trend (t > 4).
DRIFT_MIN_CHANGE_SPREADS = 5.0

# "Gradual" means the change shows up in BOTH halves of the window: each half must contain at
# least this share of the total change. This rejects abrupt jumps (those are the spike
# detector's job): only 3.7% of 10-spread step changes pass, 0.1% of heat-spike shapes.
DRIFT_HALF_SHARE = 0.25

# Suspicious direction of a trend per sensor.
DRIFT_DIRECTION = {
    "temperature_C": "high",
    "vibration_mm_s": "high",
    "rpm": "low",
    "current_A": "high",
}

# --- Pattern detection settings -------------------------------------------------------------
# Patterns look at the last couple of minutes and at SEVERAL sensors together, so one noisy
# reading can never trigger them. All numbers below come from the 6-minute live captures
# (120 s windows evaluated every 5 s) and are in effective spreads, like the spike detector.
PATTERN_WINDOW_S = 120
PATTERN_MIN_POINTS = 30
PATTERN_MIN_COVERAGE = 0.9
PATTERN_DIRECTION = DRIFT_DIRECTION   # same suspicious directions as for drift

# (a) Vibration variability: std of vibration in the window divided by its normal spread.
# Measured ratio: CNC_01 0.6-1.2, CNC_02 0.5-1.3, CONVEYOR_04 0.7-3.9 (random bursts),
# PUMP_03 4.4-21. A threshold of 5 triggers on 88% of PUMP_03 windows and 0% on the others;
# 4 would leave only a 3% margin over the conveyor's maximum.
PATTERN_VIB_STD_RATIO = 5.0

# (b) Co-elevation: the share of readings in the window where at least TWO of temperature,
# vibration and current are abnormal at the same moment. Per-sensor "abnormal moment" levels:
PATTERN_MOMENT_Z = {"temperature_C": 3.0, "vibration_mm_s": 3.0, "current_A": 2.0}
# Measured: the healthy machines (CNC_01, CONVEYOR_04) never reached even 5% of a window, the
# pump never fell below 20% (median 41%: its vibration bursts come with current bumps, while
# the conveyor's bursts raise vibration alone), CNC_02 reached 100% (temperature + current).
PATTERN_CO_ELEVATION_SHARE = 0.20

# (c) Rpm decline with a current change: window-mean rpm at least this many spreads LOW, and
# window-mean current at least this many spreads away from normal in EITHER direction
# (overload or loss of load). Not seen in the live captures (live rpm never declines), so
# these are plain judgement values, checked only on synthetic data.
PATTERN_RPM_LOW_Z = 3.0
PATTERN_CURRENT_CHANGE_Z = 2.0


# ---------------------------------------------------------------------------
# The common result type
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Signal:
    """
    One piece of evidence from one detector about one sensor of one machine.

    machine_id : which machine
    detector   : "spike", "drift", "pattern", ...
    sensor     : the sensor name (or "multi" for detectors that combine sensors)
    triggered  : True if the detector's own threshold was crossed
    score      : strength relative to the threshold. 1.0 means exactly at the threshold, 2.0
                 means twice as far past it, 0.0 means nothing unusual. Lets the agent add
                 up evidence from different detectors on one scale.
    timestamp  : timestamp of the reading that produced the signal
    evidence   : the actual numbers behind the decision (value, baseline, spread, z, ...),
                 so explain.py can quote real measurements instead of guessing.
    """
    machine_id: str
    detector: str
    sensor: str
    triggered: bool
    score: float
    timestamp: str
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Spike detection (3A)
# ---------------------------------------------------------------------------
def reference_centre(sensor_info, hour):
    """
    What value is 'normal' for this sensor at this UTC hour?
      * sensor already drifting in the reference window (PUMP_03 rpm): use the nominal value
        from /machines, because the window has no trustworthy healthy level;
      * otherwise: the hourly median, falling back to the all-hours median if that hour has no
        statistics. (Median, not mean, so a few extreme readings in the history do not pull it.)
    """
    if sensor_info["drifting_in_window"]:
        return sensor_info["reference_center"]
    stats = sensor_info["hourly"].get(str(hour)) or sensor_info["overall"]
    return stats["median"]


def effective_spread(sensor_info, sensor):
    """
    The 'one spread' we measure deviations in: the larger of the history spread and the live
    noise for this sensor. Both are positive (baseline.py floors the history spread), so
    dividing by this can never divide by zero.
    """
    spread = max(sensor_info["reference_spread"], LIVE_NOISE_SD[sensor])
    if spread <= 0:
        raise ValueError(f"non-positive spread for {sensor}; the baseline file is damaged")
    return spread


def detect_spikes(machine_id, reading, baseline):
    """
    Compare one live reading with the historical baseline, sensor by sensor.

    z = (value - normal) / effective_spread, then it is checked against that sensor's
    threshold and direction. Returns one Signal per sensor (triggered or not), so callers
    can log all of them.

    Raises ValueError for a malformed reading (missing field, non-numeric value, bad
    timestamp); the caller must decide what to do with it. We never guess at missing data.
    Raises KeyError if the baseline has no entry for this machine.
    """
    issues = check_reading(reading)
    if issues:
        raise ValueError(f"cannot run spike detection on a bad reading: {issues}")

    hour = parse_timestamp(reading["timestamp"]).hour  # UTC, matching baseline.py
    sensors = baseline["machines"][machine_id]["sensors"]

    signals = []
    for sensor in SENSOR_FIELDS:
        info = sensors[sensor]
        value = reading[sensor]
        centre = reference_centre(info, hour)
        spread = effective_spread(info, sensor)
        z = (value - centre) / spread

        direction = SPIKE_DIRECTION[sensor]
        if direction == "high":
            z_bad = z            # positive = deviation in the suspicious direction
        elif direction == "low":
            z_bad = -z
        else:
            z_bad = abs(z)

        threshold = SPIKE_THRESHOLD_Z[sensor]
        signals.append(Signal(
            machine_id=machine_id,
            detector="spike",
            sensor=sensor,
            triggered=z_bad > threshold,
            score=max(0.0, z_bad) / threshold,
            timestamp=reading["timestamp"],
            evidence={
                "value": value,
                "baseline": centre,
                "spread": spread,
                "spread_source": "live noise" if LIVE_NOISE_SD[sensor] >= info["reference_spread"] else "history",
                "z": z,
                "threshold": threshold,
                "direction": direction,
                "hour_utc": hour,
            },
        ))
    return signals


# ---------------------------------------------------------------------------
# Helpers shared by the window-based detectors (drift and pattern)
# ---------------------------------------------------------------------------
def select_window(window, window_s, min_points, min_coverage):
    """
    Pick the newest `window_s` seconds from a (datetime, reading) window, oldest first.
    Returns (points, span_seconds), or None if there is not enough data yet. None means
    "not evaluated", NOT "all clear". Raises ValueError if the window is out of time order.
    """
    if not window:
        return None
    newest_time = window[-1][0]
    points = [(t, r) for t, r in window if (newest_time - t).total_seconds() <= window_s]
    times = [t for t, _ in points]
    if times != sorted(times):
        raise ValueError("window must be in time order, oldest first")
    span = (newest_time - times[0]).total_seconds()
    if len(points) < min_points or span < min_coverage * window_s:
        return None
    return points, span


def sensor_values(points, sensor):
    """The numbers of one sensor from the window; ValueError if any is not a finite number."""
    values = [r[sensor] for _, r in points]
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"non-numeric {sensor} value in window: {value!r}")
    return values


def signed_z(sensor_info, sensor, value, hour, direction):
    """
    z-score of one value in the suspicious direction (positive = abnormal), using the same
    centre and effective spread as the spike detector. direction: "high", "low" or "both".
    Returns (z_in_suspicious_direction, centre).
    """
    centre = reference_centre(sensor_info, hour)
    z = (value - centre) / effective_spread(sensor_info, sensor)
    if direction == "high":
        return z, centre
    if direction == "low":
        return -z, centre
    return abs(z), centre


# ---------------------------------------------------------------------------
# Drift detection (3B)
# ---------------------------------------------------------------------------
def fit_line(xs, ys):
    """
    Ordinary least-squares line y = intercept + slope * x, written out by hand.
    xs are SECONDS since the start of the window (not reading numbers), so unevenly spaced
    or missing readings do not distort the slope. Returns (slope_per_second, intercept).
    """
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("cannot fit a trend: all readings have the same timestamp")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


def detect_drift(machine_id, window, baseline, window_s=DRIFT_WINDOW_S):
    """
    Look for a gradual, sustained trend in the suspicious direction, sensor by sensor.

    window   : recent readings as (datetime, reading) pairs, oldest first (the format that
               common.clean_readings returns). It may hold more than window_s seconds; only
               the newest window_s seconds are used.
    Returns one Signal per sensor, or an EMPTY list if there is not enough data yet. An empty
    list means "not evaluated", NOT "all clear"; the agent should log it that way.

    A sensor triggers when ALL of these hold:
      1. direction: it moves the suspicious way (temperature, vibration, current up; rpm down);
      2. magnitude: the fitted change over the window is at least DRIFT_MIN_CHANGE_SPREADS
         effective spreads (same spread as the spike detector, so the units match);
      3. gradual: both halves of the window each contain at least DRIFT_HALF_SHARE of that
         change, so a sudden jump does not count.
    Raises ValueError for readings out of time order or with non-numeric sensor values.
    """
    selected = select_window(window, window_s, DRIFT_MIN_POINTS, DRIFT_MIN_COVERAGE)
    if selected is None:
        return []  # not enough data yet
    points, span = selected

    first_time = points[0][0]
    xs = [(t - first_time).total_seconds() for t, _ in points]
    midpoint = span / 2
    sensors = baseline["machines"][machine_id]["sensors"]
    hour = points[-1][0].hour

    signals = []
    for sensor in SENSOR_FIELDS:
        ys = sensor_values(points, sensor)

        slope, intercept = fit_line(xs, ys)
        change = slope * span                      # fitted change across the whole window

        # Fit each half separately (split by TIME, not by reading count).
        half_changes = None
        first_half = [(x, y) for x, y in zip(xs, ys) if x < midpoint]
        second_half = [(x, y) for x, y in zip(xs, ys) if x >= midpoint]
        if len(first_half) >= DRIFT_MIN_HALF_POINTS and len(second_half) >= DRIFT_MIN_HALF_POINTS:
            slope1, _ = fit_line([x for x, _ in first_half], [y for _, y in first_half])
            slope2, _ = fit_line([x for x, _ in second_half], [y for _, y in second_half])
            half_changes = (slope1 * midpoint, slope2 * midpoint)

        info = sensors[sensor]
        spread = effective_spread(info, sensor)
        sign = 1.0 if DRIFT_DIRECTION[sensor] == "high" else -1.0
        bad_change = sign * change                 # positive = moving the suspicious way
        magnitude = bad_change / spread            # in effective spreads (can be negative)

        gradual = (
            half_changes is not None
            and bad_change > 0
            and all(sign * c >= DRIFT_HALF_SHARE * bad_change for c in half_changes)
        )
        triggered = gradual and magnitude >= DRIFT_MIN_CHANGE_SPREADS
        # An abrupt jump scores 0 here: it is the spike detector's evidence, not drift.
        score = max(0.0, magnitude) / DRIFT_MIN_CHANGE_SPREADS if gradual else 0.0

        signals.append(Signal(
            machine_id=machine_id,
            detector="drift",
            sensor=sensor,
            triggered=triggered,
            score=score,
            timestamp=points[-1][1]["timestamp"],
            evidence={
                "slope_per_min": slope * 60,
                "change": change,
                "change_spreads": magnitude,
                "threshold_spreads": DRIFT_MIN_CHANGE_SPREADS,
                "spread": spread,
                "fitted_start": intercept,
                "fitted_end": intercept + slope * span,
                "baseline": reference_centre(info, hour),
                "window_s": span,
                "n_points": len(points),
                "gradual": gradual,
                "first_half_change": half_changes[0] if half_changes else None,
                "second_half_change": half_changes[1] if half_changes else None,
                "direction": DRIFT_DIRECTION[sensor],
            },
        ))
    return signals


# ---------------------------------------------------------------------------
# Pattern detection (3C)
# ---------------------------------------------------------------------------
def detect_patterns(machine_id, window, baseline, window_s=PATTERN_WINDOW_S):
    """
    Look for combinations of behaviour over the last couple of minutes. Returns exactly three
    Signals (detector="pattern"), or an EMPTY list if there is not enough data yet ("not
    evaluated", not "all clear"):

      "vibration_variability"  vibration is far more jumpy than normal
      "multi"                  at least two of temperature / vibration / current are abnormal
                               at the SAME moment, for a large share of the window
      "rpm+current"            rpm is sustained low AND current has moved (either way)

    Each one looks at the whole window, so a single noisy reading cannot trigger it. About
    "increasing" variability: we measure how jumpy vibration is compared with normal; if it is
    getting worse, the ratio rises over successive evaluations, and the evidence also shows
    the first-half and second-half spread.
    """
    selected = select_window(window, window_s, PATTERN_MIN_POINTS, PATTERN_MIN_COVERAGE)
    if selected is None:
        return []
    points, span = selected
    sensors = baseline["machines"][machine_id]["sensors"]
    timestamp = points[-1][1]["timestamp"]
    n = len(points)

    # Per-reading z-scores in the suspicious direction, using each reading's own UTC hour.
    z_dir = {}
    z_signed = {}      # plain (value - normal) / spread, either sign
    centres = {}
    values = {}
    for sensor in SENSOR_FIELDS:
        values[sensor] = sensor_values(points, sensor)
        info = sensors[sensor]
        z_dir[sensor], centres[sensor], z_signed[sensor] = [], [], []
        for (t, _), value in zip(points, values[sensor]):
            z, centre = signed_z(info, sensor, value, t.hour, PATTERN_DIRECTION[sensor])
            z_dir[sensor].append(z)
            centres[sensor].append(centre)
            z_signed[sensor].append(signed_z(info, sensor, value, t.hour, "high")[0])

    signals = []

    # --- (a) vibration variability -------------------------------------------------------
    vib = values["vibration_mm_s"]
    expected_spread = effective_spread(sensors["vibration_mm_s"], "vibration_mm_s")
    std_all = statistics.stdev(vib)
    half = n // 2
    std_first = statistics.stdev(vib[:half]) if half >= 2 else None
    std_second = statistics.stdev(vib[half:]) if n - half >= 2 else None
    ratio = std_all / expected_spread
    signals.append(Signal(
        machine_id=machine_id, detector="pattern", sensor="vibration_mm_s",
        triggered=ratio >= PATTERN_VIB_STD_RATIO,
        score=ratio / PATTERN_VIB_STD_RATIO, timestamp=timestamp,
        evidence={"pattern": "vibration_variability", "std": std_all, "expected_spread": expected_spread,
                  "ratio": ratio, "threshold_ratio": PATTERN_VIB_STD_RATIO,
                  "std_first_half": std_first, "std_second_half": std_second,
                  "mean": statistics.mean(vib), "baseline": statistics.mean(centres["vibration_mm_s"]),
                  "window_s": span, "n_points": n},
    ))

    # --- (b) co-elevation of two or more sensors at the same moment ---------------------------
    monitored = tuple(PATTERN_MOMENT_Z)
    abnormal = {s: [z >= PATTERN_MOMENT_Z[s] for z in z_dir[s]] for s in monitored}
    both = sum(1 for i in range(n) if sum(abnormal[s][i] for s in monitored) >= 2)
    share = both / n
    pairs = {}
    for i, a in enumerate(monitored):
        for b in monitored[i + 1:]:
            pairs[f"{a}+{b}"] = sum(1 for k in range(n) if abnormal[a][k] and abnormal[b][k]) / n
    dominant = max(pairs, key=pairs.get)
    signals.append(Signal(
        machine_id=machine_id, detector="pattern", sensor="multi",
        triggered=share >= PATTERN_CO_ELEVATION_SHARE,
        score=share / PATTERN_CO_ELEVATION_SHARE, timestamp=timestamp,
        evidence={"pattern": "co_elevation", "share": share, "threshold_share": PATTERN_CO_ELEVATION_SHARE,
                  "pair_shares": pairs, "dominant_pair": dominant if pairs[dominant] > 0 else None,
                  "moment_thresholds": dict(PATTERN_MOMENT_Z),
                  "mean_values": {s: statistics.mean(values[s]) for s in monitored},
                  "baselines": {s: statistics.mean(centres[s]) for s in monitored},
                  "window_s": span, "n_points": n},
    ))

    # --- (c) rpm decline together with a current change ------------------------------------------
    rpm_z = statistics.mean(z_dir["rpm"])                          # positive = rpm below normal
    current_z = statistics.mean(z_signed["current_A"])             # signed: + overload, - loss of load
    rpm_ratio = max(0.0, rpm_z) / PATTERN_RPM_LOW_Z
    current_ratio = abs(current_z) / PATTERN_CURRENT_CHANGE_Z
    signals.append(Signal(
        machine_id=machine_id, detector="pattern", sensor="rpm+current",
        triggered=rpm_ratio >= 1.0 and current_ratio >= 1.0,
        score=min(rpm_ratio, current_ratio), timestamp=timestamp,   # both must be strong for a high score
        evidence={"pattern": "rpm_decline_with_current", "rpm_z": rpm_z, "current_z": current_z,
                  "rpm_threshold": PATTERN_RPM_LOW_Z, "current_threshold": PATTERN_CURRENT_CHANGE_Z,
                  "mean_rpm": statistics.mean(values["rpm"]), "baseline_rpm": statistics.mean(centres["rpm"]),
                  "mean_current": statistics.mean(values["current_A"]),
                  "baseline_current": statistics.mean(centres["current_A"]),
                  "window_s": span, "n_points": n},
    ))
    return signals