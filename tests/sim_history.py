"""
A Python copy of the simulator's history generator (generate-history.js in the
Jnanik-AI/malendau-hackathon repository), used only by the tests: they can then build realistic
7-day histories for all four machines without Node.js. Same formulas and constants; Python's
seeded random module replaces Math.random, so single numbers differ but the statistics match
(the daily medians were compared with 12 runs of the real generator when this was written).

What the simulator puts into the history (dayOffset 6 = oldest day ... 0 = newest day):
  CNC_01       vibration +3.5 * d, temperature +12 * d, current +2.5 * d, d = max(0, 3 - dayOffset) / 3
  CNC_02       afternoon (local 12:00-18:00) +15..30 C and +2..5 A on the last two days; a fault
               episode on the second-newest day
  PUMP_03      cavitation bursts in 8% of the minutes; rpm lowered by (7 - dayOffset) / 7 * 180
  CONVEYOR_04  one 72-minute vibration excursion four days ago
Nothing here opens a connection or writes a file.
"""
import copy
import functools
import random
from datetime import datetime, timedelta, timezone

from tests.helpers import fixture_baseline, iso
import baseline as baseline_module
from common import NOMINAL_KEY, SENSOR_FIELDS, clean_readings

BASELINES = {
    "CNC_01": {"temp": 72, "vib": 1.8, "rpm": 1480, "current": 12.5},
    "CNC_02": {"temp": 68, "vib": 1.5, "rpm": 1490, "current": 11.8},
    "PUMP_03": {"temp": 55, "vib": 2.2, "rpm": 2950, "current": 18.0},
    "CONVEYOR_04": {"temp": 45, "vib": 0.9, "rpm": 720, "current": 8.5},
}
NOW = datetime(2026, 11, 7, 9, 0, 0, tzinfo=timezone.utc)   # when the simulated server started
LOCAL_OFFSET = timedelta(hours=5, minutes=30)                # the simulator uses LOCAL time of day
DAYS = 7
DAY = timedelta(days=1)


def _reading(rng, machine, moment, day_offset, progress):
    """generateReading() of generate-history.js."""
    b = BASELINES[machine]

    def rand(low, high):
        return rng.random() * (high - low) + low

    def noise(base, pct):
        return base + rand(-base * pct, base * pct)

    temp, vib, rpm, current = noise(b["temp"], 0.03), noise(b["vib"], 0.04), noise(b["rpm"], 0.015), noise(b["current"], 0.03)
    status = "running"

    if machine == "CNC_01":
        d = max(0, 3 - day_offset) / 3
        vib += d * 3.5
        temp += d * 12
        current += d * 2.5
        if d > 0.6:
            status = "warning"
        if d > 0.9 and progress > 0.8:
            status = "fault"

    if machine == "CNC_02":
        if 0.5 < progress < 0.75 and day_offset < 2:
            temp += rand(15, 30)
            current += rand(2, 5)
            if temp > 95:
                status = "warning"
        if day_offset == 1 and 0.60 < progress < 0.65:
            temp = 112 + rand(0, 8)
            current = 22 + rand(0, 3)
            status = "fault"

    if machine == "PUMP_03":
        if rng.random() < 0.08:
            vib += rand(2, 6)
            current += rand(1, 3)
            if vib > 6:
                status = "warning"
        rpm -= ((7 - day_offset) / 7) * 180
        if rpm < 2820:
            status = "warning"

    if machine == "CONVEYOR_04":
        if day_offset == 4 and 0.4 < progress < 0.45:
            vib += rand(1, 2)
            status = "warning"

    temp = max(20, min(130, temp))
    vib = max(0.1, min(12, vib))
    rpm = max(100, min(4000, rpm))
    current = max(1, min(30, current))
    return {"machine_id": machine, "timestamp": iso(moment), "temperature_C": round(temp, 2),
            "vibration_mm_s": round(vib, 2), "rpm": float(round(rpm)), "current_A": round(current, 2),
            "status": status}


def generate(machine, seed, now=NOW, local_offset=LOCAL_OFFSET):
    """generateAllHistory() for one machine: 7 days + 1 reading at one per minute, oldest first."""
    rng = random.Random(f"{seed}-{machine}")
    readings = []
    for minutes_back in range(DAYS * 1440, -1, -1):
        offset = timedelta(minutes=minutes_back)
        moment = now - offset
        local = moment + local_offset
        progress = (local.hour * 3600 + local.minute * 60 + local.second) / 86400
        readings.append(_reading(rng, machine, moment, offset // DAY, progress))
    return readings


def nominals():
    """{machine: {sensor: nominal}}, as GET /machines reports them, in our field names."""
    return {m: {field: BASELINES[m][NOMINAL_KEY[field]] for field in SENSOR_FIELDS} for m in BASELINES}


def histories(seed, machines=tuple(BASELINES)):
    """{machine: [(datetime, reading), ...]}: the format baseline.build_baseline takes."""
    result = {}
    for machine in machines:
        clean, n_bad = clean_readings(generate(machine, seed))
        assert n_bad == 0
        result[machine] = clean
    return result


@functools.lru_cache(maxsize=None)
def _built(seed):
    return baseline_module.build_baseline(histories(seed), nominals(), source=f"sim_history seed {seed}")


def simulated_baseline(seed):
    """The baseline that baseline.py builds from a simulated history (a fresh copy each call)."""
    return copy.deepcopy(_built(seed))


def fixture_baseline_with_history(seed=1):
    """
    The recordings' baseline (tests/fixtures/baseline.json) plus daily medians. That file was
    made before the history check existed, so it has none; the daily days and medians of a
    simulated history (same simulator formulas) are added, sensor by sensor. Everything the
    live detectors use stays exactly as recorded, so the live results do not change.
    """
    target = fixture_baseline()
    source = simulated_baseline(seed)
    for machine, info in target["machines"].items():
        for sensor, data in info["sensors"].items():
            simulated = source["machines"][machine]["sensors"][sensor]
            data["daily_days"] = simulated["daily_days"]
            data["daily_medians"] = simulated["daily_medians"]
    return target
