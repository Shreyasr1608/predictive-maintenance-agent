"""
Shared test helpers. Nothing here opens a network connection or writes into the project.

tests/fixtures/ holds frozen copies of the recorded live captures (data/live_<machine>.csv)
and of the baseline built from the same simulator run (baseline.json), so the tests give the
same answers even after capture_live.py or baseline.py has been run again.
"""
import csv
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from baseline import load_baseline  # noqa: E402  (needs the project folder on sys.path)
from common import SENSOR_FIELDS  # noqa: E402

# The agent logs warnings about bad input on purpose; keep the test output readable.
# Tests that check those messages capture them with assertLogs(), which still works.
logging.getLogger("agent").addHandler(logging.NullHandler())

MACHINES = ("CNC_01", "CNC_02", "PUMP_03", "CONVEYOR_04")
TEST_MACHINE = "TEST_01"
START = datetime(2026, 11, 5, 5, 0, 0, tzinfo=timezone.utc)   # 05:00 UTC
CENTRES = {"temperature_C": 70.0, "vibration_mm_s": 2.0, "rpm": 1500.0, "current_A": 12.0}


def at(seconds):
    """START plus some seconds."""
    return START + timedelta(seconds=seconds)


def iso(moment):
    """datetime -> '2026-11-05T05:00:00.000Z', the simulator's timestamp format."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def make_baseline(centres=None, spreads=None, medians=None, drifting=(), hourly=None, machine=TEST_MACHINE):
    """
    A hand-made baseline holding only the keys the detectors and the agent read, so a test
    knows the exact centre and spread. The history spread defaults to a tiny value, so the
    live-noise spread (detector.LIVE_NOISE_SD) is the one in use.
      centres  : {sensor: value} changes to CENTRES (also used as the nominal value)
      spreads  : {sensor: history spread}
      medians  : {sensor: all-hours median} (defaults to the centre)
      drifting : sensors marked as drifting in the reference window
      hourly   : {sensor: {"5": {"median": value}}} hour-of-day statistics
    """
    centres = dict(CENTRES, **(centres or {}))
    sensors = {}
    for sensor in SENSOR_FIELDS:
        sensors[sensor] = {
            "nominal": centres[sensor],
            "reference_center": centres[sensor],
            "reference_spread": (spreads or {}).get(sensor, 0.001),
            "drifting_in_window": sensor in drifting,
            "overall": {"median": (medians or {}).get(sensor, centres[sensor])},
            "hourly": (hourly or {}).get(sensor, {}),
        }
    return {"meta": {"source": "tests"}, "machines": {machine: {"sensors": sensors}}}


def make_history_baseline(medians=None, centres=None, spreads=None, drifting=(), reference_days=4,
                          machine=TEST_MACHINE):
    """
    make_baseline() plus the daily statistics the history check reads.
      medians        : {sensor: one daily median per day, oldest first}; sensors not given sit at
                       their centre on every day (7 days, or as many as the given lists have)
      spreads        : history spreads; 1.0 unless given, so centre + 3.5 is a 3.5-spread shift
      reference_days : how many of the first days formed the reference window
    """
    medians = medians or {}
    spreads = dict({sensor: 1.0 for sensor in SENSOR_FIELDS}, **(spreads or {}))
    base = make_baseline(centres=centres, spreads=spreads, drifting=drifting, machine=machine)
    base["meta"]["reference_days"] = reference_days
    n_days = len(next(iter(medians.values()))) if medians else 7
    for sensor, info in base["machines"][machine]["sensors"].items():
        series = list(medians.get(sensor, [info["reference_center"]] * n_days))
        info["daily_days"] = list(range(1, len(series) + 1))
        info["daily_medians"] = series
    return base


def reading(moment, machine=TEST_MACHINE, **values):
    """A valid reading at `moment`; sensors that are not given sit at their CENTRES value."""
    result = {"machine_id": machine, "timestamp": iso(moment), "status": "running"}
    for sensor in SENSOR_FIELDS:
        result[sensor] = values.get(sensor, CENTRES[sensor])
    return result


def window(seconds, values=None, step=1.0, machine=TEST_MACHINE):
    """
    (datetime, reading) pairs from START, one every `step` seconds, `seconds` long (both ends
    included): the window format the detectors take. values(t) returns {sensor: value}
    overrides for the reading t seconds after START.
    """
    pairs = []
    for i in range(int(round(seconds / step)) + 1):
        t = i * step
        pairs.append((at(t), reading(at(t), machine, **(values(t) if values else {}))))
    return pairs


def fixture_baseline():
    """The baseline built from the same simulator run as the recorded captures."""
    return load_baseline(FIXTURES / "baseline.json")


def fixture_capture(machine):
    """The 360 recorded live readings of one machine (CSV text turned back into numbers)."""
    with open(FIXTURES / f"live_{machine}.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for sensor in SENSOR_FIELDS:
            row[sensor] = float(row[sensor])
    return rows
