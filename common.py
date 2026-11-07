"""
common.py -- small helpers shared by baseline.py, detector.py, agent.py, ...

Deliberately light: only `requests` is imported, so the agent never needs matplotlib.

Note: explore.py (Phase 1) has its own older copies of parse_timestamp / check_reading.
It was verified against the real simulator as it is, so it was left unchanged.
"""

import math
from datetime import datetime, timezone

import requests

DEFAULT_BASE_URL = "http://localhost:3000"
REQUEST_TIMEOUT = 10  # seconds to wait for an HTTP response

# Field names as they appear in every reading (checked against the real simulator).
SENSOR_FIELDS = ("temperature_C", "vibration_mm_s", "rpm", "current_A")
ALL_FIELDS = ("machine_id", "timestamp") + SENSOR_FIELDS + ("status",)

# GET /machines reports nominal values under shorter names; this maps ours to theirs.
NOMINAL_KEY = {
    "temperature_C": "temp",
    "vibration_mm_s": "vib",
    "rpm": "rpm",
    "current_A": "current",
}


class SimulatorError(Exception):
    """Raised when the simulator cannot be reached or answers with something unusable."""


def get_json(base_url, path):
    """
    GET base_url + path and return the parsed JSON.
    Raises SimulatorError with a readable message on ANY failure, so callers cannot
    accidentally carry on with missing data.
    """
    url = base_url.rstrip("/") + path
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.ConnectionError as exc:
        raise SimulatorError(f"cannot connect to {url} - is the simulator running?") from exc
    except requests.exceptions.Timeout as exc:
        raise SimulatorError(f"no response from {url} within {REQUEST_TIMEOUT}s") from exc
    except requests.exceptions.RequestException as exc:
        raise SimulatorError(f"request to {url} failed: {exc}") from exc

    if response.status_code != 200:
        raise SimulatorError(f"GET {path} returned HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as exc:
        raise SimulatorError(f"GET {path}: response body is not valid JSON") from exc


def parse_timestamp(text):
    """
    '2026-10-28T13:20:42.477Z' -> timezone-aware datetime in UTC.
    fromisoformat() wants '+00:00' instead of 'Z', so we swap it. Any other UTC offset is
    converted to UTC, because the hour-of-day baselines are in UTC.
    Raises ValueError if the text cannot be parsed or carries no time zone at all (such a
    "naive" time cannot be compared with the others and would crash the comparison later).
    """
    if not isinstance(text, str):
        raise ValueError(f"timestamp is {type(text).__name__}, not text")
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"timestamp has no time zone: {text!r}")
    return moment.astimezone(timezone.utc)


def check_reading(reading):
    """
    Return a list of problems with ONE reading (an empty list means it looks fine).
    Checks: it is an object, all expected fields exist, sensor values are finite
    numbers, and the timestamp parses.
    """
    if not isinstance(reading, dict):
        return [f"reading is {type(reading).__name__}, not an object"]

    issues = []
    for field in ALL_FIELDS:
        if reading.get(field) is None:
            issues.append(f"missing or null: {field}")

    for field in SENSOR_FIELDS:
        value = reading.get(field)
        if value is None:
            continue  # already reported as missing
        # bool is a subclass of int in Python, so exclude it explicitly
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(f"{field} is not a number: {value!r}")
        elif not math.isfinite(value):
            issues.append(f"{field} is not finite: {value!r}")

    if reading.get("timestamp") is not None:
        try:
            parse_timestamp(reading["timestamp"])
        except ValueError:
            issues.append(f"unparseable timestamp: {reading['timestamp']!r}")
    return issues


def clean_readings(raw_readings):
    """
    Keep only valid readings. Returns (clean, n_bad) where `clean` is a list of
    (datetime, reading) pairs sorted by time and `n_bad` counts what was dropped.
    The caller decides how loudly to report n_bad; nothing is dropped silently.
    """
    clean = []
    n_bad = 0
    for reading in raw_readings:
        if check_reading(reading):
            n_bad += 1
        else:
            clean.append((parse_timestamp(reading["timestamp"]), reading))
    clean.sort(key=lambda pair: pair[0])
    return clean, n_bad


def load_history(base_url, machine_id):
    """
    Fetch /history/{machine_id} and return (clean_readings, n_bad).
    Real response shape (seen in Phase 1): {"machine_id", "count", "readings": [...]}.
    """
    data = get_json(base_url, f"/history/{machine_id}")
    if not isinstance(data, dict) or not isinstance(data.get("readings"), list):
        raise SimulatorError(f"/history/{machine_id}: response has no 'readings' list")
    return clean_readings(data["readings"])