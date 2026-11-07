"""
explore.py -- Phase 1: look at what the simulator really returns.

Nothing in this file detects anomalies. The only goal is to SEE the data and
the API contract before we design anything on top of it.

What it does, in order:
  1. GET /machines        machine list + nominal baselines
  2. GET /alerts          how many alerts exist before we touch anything
  3. GET /history/{id}    structure, data-quality checks, summary statistics
  4. GET /stream/{id}     a few live readings per machine (SSE), then disconnect
  5. Plots                7-day history per machine, saved as PNG in ./plots
  6. POST tests           ONLY if you pass --test-posts (see side effects below)

Usage:
  python explore.py                      read-only, safe to run any time
  python explore.py --stream-count 10    more live readings per machine
  python explore.py --test-posts         also send the clearly labelled test POSTs

Side effects of the POST tests (read from the simulator's server.js):
  * POST /alert stores the alert in server memory. It then shows up in
    GET /alerts (and probably the dashboard). There is no delete endpoint, so
    the only way to clear it is to restart the server (npm start). A restart
    also regenerates the 7-day history.
  * POST /schedule-maintenance stores a booking in server memory. There is NO
    endpoint to list or delete bookings, so a real booking would be invisible
    garbage. This script therefore only sends INVALID maintenance requests
    (they are rejected with HTTP 400 before anything is stored).
"""

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # draw straight to image files; no window needed
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "http://localhost:3000"
REQUEST_TIMEOUT = 10         # seconds to wait for a normal HTTP response
STREAM_CONNECT_TIMEOUT = 5   # seconds to wait for the stream to open
STREAM_READ_TIMEOUT = 10     # max silence on the stream (readings arrive every ~1 s)
PLOT_DIR = Path("plots")

# Field names as listed in the PDF and seen in server.js / real responses.
SENSOR_FIELDS = ("temperature_C", "vibration_mm_s", "rpm", "current_A")
ALL_FIELDS = ("machine_id", "timestamp") + SENSOR_FIELDS + ("status",)

# The /machines endpoint calls the sensors temp/vib/rpm/current; map to our names.
BASELINE_KEY = {
    "temperature_C": "temp",
    "vibration_mm_s": "vib",
    "rpm": "rpm",
    "current_A": "current",
}

TEST_REASON = "[TEST - IGNORE] explore.py API contract check, not a real alert"

# Every problem we hit is printed immediately AND remembered here, so the final
# summary can list them all. Nothing is silently swallowed.
PROBLEMS = []


# ---------------------------------------------------------------------------
# Small printing helpers
# ---------------------------------------------------------------------------
def header(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def report_problem(message):
    """Print a problem loudly and remember it for the final summary."""
    print(f"  !! PROBLEM: {message}")
    PROBLEMS.append(message)


def shorten(obj, max_items=2):
    """Return a copy of obj with long lists cut down so printing stays readable."""
    if isinstance(obj, list):
        shown = [shorten(item, max_items) for item in obj[:max_items]]
        if len(obj) > max_items:
            shown.append(f"... ({len(obj) - max_items} more items)")
        return shown
    if isinstance(obj, dict):
        return {key: shorten(value, max_items) for key, value in obj.items()}
    return obj


def pretty(obj, max_items=2):
    """Print JSON-like data indented, with long lists shortened."""
    print(json.dumps(shorten(obj, max_items), indent=2))


def format_reading(reading):
    """One-line view of a reading, for the stream printout."""
    return (
        f"{reading['timestamp']}  "
        f"temp={reading['temperature_C']:7.2f}  "
        f"vib={reading['vibration_mm_s']:6.2f}  "
        f"rpm={reading['rpm']:7.0f}  "
        f"current={reading['current_A']:6.2f}  "
        f"status={reading['status']}"
    )


# ---------------------------------------------------------------------------
# HTTP helpers (these never raise: failures are reported, not hidden)
# ---------------------------------------------------------------------------
def request_json(method, base_url, path, body=None):
    """
    Send one HTTP request. Returns (status_code, parsed_json).
      (None, None)   -> the request itself failed (server down, timeout, ...)
      (status, None) -> the server answered but the body was not valid JSON
    """
    url = base_url + path
    try:
        response = requests.request(method, url, json=body, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.ConnectionError:
        report_problem(f"{method} {path}: cannot connect to {base_url}. Is `npm start` running?")
        return None, None
    except requests.exceptions.Timeout:
        report_problem(f"{method} {path}: no response within {REQUEST_TIMEOUT}s")
        return None, None
    except requests.exceptions.RequestException as exc:
        report_problem(f"{method} {path}: request failed: {exc}")
        return None, None

    try:
        return response.status_code, response.json()
    except ValueError:
        report_problem(
            f"{method} {path}: HTTP {response.status_code} but the body is not valid JSON: "
            f"{response.text[:200]!r}"
        )
        return response.status_code, None


def get_json(base_url, path):
    """GET a path and return the parsed JSON only if the status is 200, else None."""
    status, data = request_json("GET", base_url, path)
    if status is None or data is None:
        return None  # request_json already reported why
    if status != 200:
        report_problem(f"GET {path}: expected HTTP 200, got {status}: {data}")
        return None
    return data


# ---------------------------------------------------------------------------
# Reading validation
# ---------------------------------------------------------------------------
def parse_timestamp(text):
    """
    '2026-10-28T13:20:42.477Z' -> timezone-aware datetime in UTC.
    Python's fromisoformat() wants '+00:00' instead of 'Z', so we swap it.
    Raises ValueError if the text cannot be parsed.
    """
    if not isinstance(text, str):
        raise ValueError(f"timestamp is {type(text).__name__}, not text")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def check_reading(reading):
    """
    Return a list of problems with ONE reading (empty list = looks fine).
    Checks: it is an object, every expected field is present, sensor values are
    real finite numbers, and the timestamp parses.
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


# ---------------------------------------------------------------------------
# Step 3: history
# ---------------------------------------------------------------------------
def fetch_history(base_url, machine_id):
    """
    GET /history/{id}. Real shape (from the live response and server.js):
        {"machine_id": ..., "count": N, "readings": [ {...}, {...}, ... ]}
    Returns the list of raw readings, or None on failure.
    """
    data = get_json(base_url, f"/history/{machine_id}")
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("readings"), list):
        report_problem(f"{machine_id}: history response has no 'readings' list")
        return None

    pretty(data)  # shows the first 2 readings, then "... N more items"
    readings = data["readings"]
    if data.get("count") != len(readings):
        report_problem(
            f"{machine_id}: 'count' says {data.get('count')} but the list has {len(readings)} readings"
        )
    return readings


def summarize_readings(machine_id, readings):
    """
    Data-quality checks and simple statistics for one machine's history.
    Returns the VALID readings as a list of (datetime, reading) pairs.
    """
    clean = []
    bad = 0
    for reading in readings:
        issues = check_reading(reading)
        if issues:
            bad += 1
            if bad <= 3:  # show only the first few offenders
                print(f"  malformed reading skipped: {issues} -> {reading}")
            continue
        clean.append((parse_timestamp(reading["timestamp"]), reading))

    if bad:
        report_problem(f"{machine_id}: {bad} malformed history readings were skipped")
    if not clean:
        report_problem(f"{machine_id}: no usable history readings")
        return []

    print(f"\n  Usable readings : {len(clean)} of {len(readings)}")
    print(f"  First timestamp : {clean[0][0].isoformat()}")
    print(f"  Last timestamp  : {clean[-1][0].isoformat()}")
    print(f"  Last reading    : {json.dumps(clean[-1][1])}")

    # Spacing between consecutive timestamps (the PDF says 1 per minute).
    times = [t for t, _ in clean]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    if gaps:
        print(
            f"  Gap between readings (seconds): min={min(gaps):.0f} "
            f"median={statistics.median(gaps):.0f} max={max(gaps):.0f}"
        )
        if min(gaps) <= 0:
            report_problem(f"{machine_id}: timestamps are not strictly increasing")

    # Which status values actually occur?
    status_counts = Counter(r["status"] for _, r in clean)
    print(f"  Status counts   : {dict(status_counts)}")

    # Per-sensor summary over the whole week (per-hour statistics come in Phase 2).
    print(f"\n  {'sensor':<16}{'min':>10}{'mean':>10}{'stdev':>10}{'max':>10}")
    for field in SENSOR_FIELDS:
        values = [r[field] for _, r in clean]
        spread = statistics.stdev(values) if len(values) > 1 else 0.0
        print(
            f"  {field:<16}{min(values):>10.2f}{statistics.mean(values):>10.2f}"
            f"{spread:>10.2f}{max(values):>10.2f}"
        )
    return clean


# ---------------------------------------------------------------------------
# Step 4: live stream (Server-Sent Events)
# ---------------------------------------------------------------------------
def read_stream(base_url, machine_id, count):
    """
    Open the SSE stream, collect `count` readings, then disconnect.

    What SSE looks like on the wire (taken from the real curl output):
        : connected to CNC_01 stream     <- a comment line (starts with ':'), ignore
        <blank line>
        data: {"machine_id": ...}        <- one reading as JSON
        <blank line>                     <- a blank line ends each event
    """
    url = f"{base_url}/stream/{machine_id}"
    readings = []
    give_up_at = time.time() + count * 3 + 10  # overall safety cap
    try:
        # stream=True hands us data as it arrives instead of waiting for the end
        # (which never comes). Leaving the `with` block closes the connection,
        # and that is how we "disconnect" once we have enough readings.
        with requests.get(
            url, stream=True, timeout=(STREAM_CONNECT_TIMEOUT, STREAM_READ_TIMEOUT)
        ) as response:
            if response.status_code != 200:
                report_problem(f"{machine_id}: stream returned HTTP {response.status_code}")
                return readings

            for raw_line in response.iter_lines():
                if time.time() > give_up_at:
                    report_problem(f"{machine_id}: stream too slow, stopping early")
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue  # blank separator or comment
                if not line.startswith("data:"):
                    print(f"    (ignoring unexpected stream line: {line[:80]!r})")
                    continue

                try:
                    reading = json.loads(line[len("data:"):].strip())
                except ValueError:
                    report_problem(f"{machine_id}: stream sent malformed JSON: {line[:120]!r}")
                    continue

                issues = check_reading(reading)
                if issues:
                    report_problem(f"{machine_id}: bad stream reading {issues}: {reading}")
                    continue

                readings.append(reading)
                if len(readings) >= count:
                    break
    except requests.exceptions.RequestException as exc:
        # Covers connection refused, timeouts and a connection dropped mid-stream.
        report_problem(f"{machine_id}: stream error: {exc}")
    return readings


# ---------------------------------------------------------------------------
# Step 5: plots
# ---------------------------------------------------------------------------
def plot_history(machine_id, clean, baseline, out_dir):
    """One PNG per machine: four stacked panels (temp, vibration, rpm, current)."""
    times = [t for t, _ in clean]
    flagged = [(t, r) for t, r in clean if r["status"] != "running"]

    fig, axes = plt.subplots(len(SENSOR_FIELDS), 1, sharex=True, figsize=(12, 9))
    fig.suptitle(f"{machine_id} - 7-day history (one reading per minute)")

    for ax, field in zip(axes, SENSOR_FIELDS):
        ax.plot(times, [r[field] for _, r in clean], linewidth=0.6)

        # Red dots: moments the simulator itself labelled as warning/fault.
        if flagged:
            ax.scatter(
                [t for t, _ in flagged], [r[field] for _, r in flagged],
                s=6, color="red", zorder=3, label="status is not 'running'",
            )
        # Dashed line: the nominal value reported by GET /machines.
        key = BASELINE_KEY[field]
        if baseline and key in baseline:
            ax.axhline(baseline[key], linestyle="--", color="gray", label="nominal baseline")

        ax.set_ylabel(field)
        ax.legend(loc="upper left", fontsize=7)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M"))
    fig.autofmt_xdate()

    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{machine_id}_history.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)  # free memory
    return path


# ---------------------------------------------------------------------------
# Step 6: POST tests (opt-in)
# ---------------------------------------------------------------------------
def test_post_endpoints(base_url, machine_id, sample_reading):
    print("Side effects: see the notes at the top of explore.py.")
    print("Part A sends INVALID requests; the server rejects them before storing anything.\n")

    invalid_cases = [
        ("alert: missing reason", "/alert", {"machine_id": machine_id}),
        ("alert: missing machine_id", "/alert", {"reason": TEST_REASON}),
        ("alert: unknown machine", "/alert", {"machine_id": "NOPE_99", "reason": TEST_REASON}),
        ("maintenance: missing machine_id", "/schedule-maintenance", {}),
        ("maintenance: unknown machine", "/schedule-maintenance", {"machine_id": "NOPE_99"}),
    ]
    for name, path, body in invalid_cases:
        status, data = request_json("POST", base_url, path, body)
        print(f"  {name:<34} -> HTTP {status}  {data}")
        if status != 400:
            report_problem(f"'{name}': expected HTTP 400, got {status}")

    print("\nPart B sends ONE valid, clearly labelled test alert (this IS stored on the server):")
    body = {"machine_id": machine_id, "reason": TEST_REASON}
    if sample_reading:
        body["reading"] = sample_reading  # 'reading' is optional; check it is echoed back
    status, data = request_json("POST", base_url, "/alert", body)
    if status == 201 and isinstance(data, dict) and data.get("success") is True:
        pretty(data)
    else:
        report_problem(f"valid test alert: expected HTTP 201 with success=true, got {status}: {data}")

    print("\nRead back with GET /alerts to confirm it was stored:")
    alerts = get_json(base_url, "/alerts")
    if alerts is not None:
        print(f"  alert count now: {alerts.get('count')}")

    print("\nTo clear the test alert: stop the simulator (Ctrl+C) and run `npm start` again.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Phase 1: explore the simulator API and data.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="simulator address")
    parser.add_argument("--stream-count", type=int, default=5,
                        help="live readings to print per machine (default 5, about 5 s each)")
    parser.add_argument("--test-posts", action="store_true",
                        help="also send the test POST requests (stores one test alert)")
    return parser.parse_args()


def main():
    args = parse_args()
    base_url = args.base_url.rstrip("/")

    # 1. /machines -----------------------------------------------------------
    header("1. GET /machines")
    machines_data = get_json(base_url, "/machines")
    if machines_data is None:
        print("\nCannot continue without /machines. Fix the problem above and run again.")
        return 1
    pretty(machines_data)
    machines = machines_data.get("machines")
    baselines = machines_data.get("baselines", {})
    if not isinstance(machines, list) or not machines:
        report_problem("/machines has no 'machines' list")
        return 1

    # 2. /alerts before we touch anything -------------------------------------
    header("2. GET /alerts (before any test requests)")
    alerts = get_json(base_url, "/alerts")
    if alerts is not None:
        pretty(alerts)

    # 3. history ---------------------------------------------------------------
    histories = {}  # machine_id -> list of (datetime, reading)
    for machine_id in machines:
        header(f"3. GET /history/{machine_id}")
        raw = fetch_history(base_url, machine_id)
        if raw is not None:
            histories[machine_id] = summarize_readings(machine_id, raw)

    # 4. live streams (one machine at a time) -----------------------------------
    header(f"4. GET /stream/{{id}} - {args.stream_count} live readings per machine")
    print("(takes about one second per reading; one connection per machine at a time)")
    for machine_id in machines:
        print(f"\n  {machine_id}:")
        live = read_stream(base_url, machine_id, args.stream_count)
        for reading in live:
            print("   ", format_reading(reading))
        if not live:
            print("    (no valid readings received)")

    # 5. plots ---------------------------------------------------------------------
    header("5. Plots of the 7-day history")
    for machine_id, clean in histories.items():
        if not clean:
            continue
        path = plot_history(machine_id, clean, baselines.get(machine_id), PLOT_DIR)
        print(f"  saved {path}")

    # 6. POST tests ------------------------------------------------------------------
    header("6. POST tests")
    if args.test_posts:
        first = machines[0]
        history = histories.get(first)
        sample = history[-1][1] if history else None
        test_post_endpoints(base_url, first, sample)
    else:
        print("  skipped (read-only run). Re-run with --test-posts to include them.")

    # Summary -------------------------------------------------------------------------
    header("Summary")
    if PROBLEMS:
        print(f"{len(PROBLEMS)} problem(s) found:")
        for problem in PROBLEMS:
            print(f"  - {problem}")
        return 1
    print("No problems found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())