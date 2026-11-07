"""
agent.py -- Phase 4: the monitoring agent.

Two layers, kept apart on purpose:

  1. DECISION CORE (class MachineMonitor). Pure logic, no network, no threads. It takes
     readings in, runs the detectors, combines their evidence into a risk level, and decides
     whether to alert. It measures time by the READINGS' timestamps, not the wall clock, so
     the same code can be tested by replaying recorded data at full speed.

  2. I/O LAYER (run_machine, run_agent, post_alert). One thread per machine reads that
     machine's Server-Sent Events stream, reconnects with back-off if it drops, feeds the
     monitor, and POSTs an alert when the monitor says so.

How a decision is made (all numbers are settings below):
  * Evidence comes in three kinds: SPIKE (a sensor abnormal in at least half of the last
    60 s), DRIFT (a gradual trend over 5 min) and PATTERN (several sensors misbehaving
    together over 2 min).
  * Risk level: "none" (no evidence), "watch" (some evidence, not corroborated) or "high"
    (at least TWO kinds of evidence involving at least TWO different sensors). One noisy
    sensor, or one detector, can never reach "high". This is what keeps the healthy
    CONVEYOR_04 quiet: its random vibration bursts look like a spike on one sensor only.
  * "high" must hold for PERSISTENCE_S before an alert is sent (a brief dip is tolerated).
  * After an alert, that machine is silent for COOLDOWN_S.
  * If posting fails, no cooldown starts, so the alert is retried at the next evaluation.

Usage:
  python agent.py                      # monitor until Ctrl+C
  python agent.py --dry-run            # decide and log, but do NOT post alerts
  python agent.py --duration 300       # stop by itself after 5 minutes
Close the simulator's browser dashboard first: every extra stream connection to a machine
speeds that machine up (see Phase 1).
"""

import argparse
import json
import logging
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass

import requests

from baseline import build_from_server, load_baseline, save_baseline
from common import DEFAULT_BASE_URL, SimulatorError, check_reading, parse_timestamp
from detectors import detect_drift, detect_patterns, detect_spikes

log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Settings (these move into config.py in Phase 7)
# ---------------------------------------------------------------------------
EVAL_INTERVAL_S = 5          # run the detectors about every 5 seconds of data time
WINDOW_KEEP_S = 360          # readings kept per machine: the drift window (300 s) plus margin

# Spike evidence: a sensor counts as "sustained" if it spiked in at least this share of the
# readings of the last SPIKE_LOOKBACK_S seconds. Evidence for 0.5: the healthy conveyor's
# vibration bursts keep it above its spike line for up to 56 s in a row, so a spike on ONE
# sensor can look sustained; that is why a second kind of evidence is always required.
SPIKE_LOOKBACK_S = 60
SPIKE_SUSTAINED_SHARE = 0.5
SPIKE_MIN_READINGS = 20      # do not judge on fewer readings than this

# Persistence: how long "high" must hold before alerting. 60 s is just above the longest
# single-sensor run measured on the healthy conveyor (56 s), so even a corroboration glitch
# would have to outlast a whole burst episode. A dip shorter than
# PERSISTENCE_RESET_EVALS evaluations (about 10 s) does not restart the clock.
PERSISTENCE_S = 60
PERSISTENCE_RESET_EVALS = 2

# Cooldown per machine after an alert. CNC_02 re-heats every ~3 minutes, so without a long
# cooldown it would alert over and over about the same ongoing problem.
COOLDOWN_S = 600

# If the stream goes quiet for longer than this between two readings, the persistence clock
# restarts: evidence must be re-established after a gap rather than carried across it.
MAX_GAP_S = 15

# Reconnection: wait these many seconds before retry 1, 2, 3 ... (the last value repeats).
RECONNECT_DELAYS_S = (1, 2, 4, 8, 15, 30)
STREAM_CONNECT_TIMEOUT_S = 5
STREAM_READ_TIMEOUT_S = 10   # readings arrive every ~1 s, so 10 s of silence means a dead stream
POST_TIMEOUT_S = 10

SENSOR_UNITS = {"temperature_C": "C", "vibration_mm_s": "mm/s", "rpm": "rpm", "current_A": "A"}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Evidence:
    """One piece of active evidence behind a risk assessment."""
    detector: str      # "spike", "drift" or "pattern"
    name: str          # the sensor, or the pattern name
    sensors: tuple     # which sensors it involves
    score: float       # relative to that detector's threshold; at least 1.0 when active
    detail: dict       # the real numbers (for explanations and logs)

    def short(self):
        return f"{self.detector}:{self.name}={self.score:.1f}"


@dataclass(frozen=True)
class Assessment:
    machine_id: str
    time: object       # datetime of the newest reading
    level: str         # "none", "watch" or "high"
    risk_score: float
    evidence: tuple    # of Evidence
    latest_reading: dict


@dataclass(frozen=True)
class Decision:
    action: str        # "none", "waiting", "alert" or "cooldown"
    note: str


# ---------------------------------------------------------------------------
# Combining evidence into a risk level
# ---------------------------------------------------------------------------
def combine(evidence):
    """
    Turn a list of Evidence into (level, risk_score).
      none  : no evidence
      watch : some evidence, but from one kind of detector or one sensor only
      high  : at least two kinds of detector AND at least two different sensors involved
    risk_score adds up the strongest score of each kind of detector (1.0 = at threshold).
    """
    if not evidence:
        return "none", 0.0
    strongest = {}
    sensors = set()
    for item in evidence:
        strongest[item.detector] = max(strongest.get(item.detector, 0.0), item.score)
        sensors.update(item.sensors)
    level = "high" if len(strongest) >= 2 and len(sensors) >= 2 else "watch"
    return level, sum(strongest.values())


# ---------------------------------------------------------------------------
# The decision core (no network, no threads)
# ---------------------------------------------------------------------------
class MachineMonitor:
    """All the state and logic for ONE machine. Used by exactly one thread."""

    def __init__(self, machine_id, baseline):
        self.machine_id = machine_id
        self.baseline = baseline
        self.window = deque()       # (datetime, reading), oldest first
        self.spike_log = deque()    # (datetime, {sensor: (triggered, score)}) for the last 60 s
        self.latest_spikes = {}     # sensor -> latest spike Signal
        self.last_time = None       # timestamp of the newest accepted reading
        self.last_eval = None       # timestamp at the last evaluation
        self.high_since = None      # since when the level has been "high"
        self.low_streak = 0         # consecutive evaluations below "high"
        self.last_alert_time = None
        self.stats = Counter()

    # -- input -------------------------------------------------------------
    def add_reading(self, reading):
        """
        Validate and store one reading. Returns True if accepted. Bad input is counted and
        logged, never silently dropped and never allowed to crash the agent:
        malformed readings, readings for another machine, and readings that are not newer
        than the previous one (the detectors need time order).
        """
        issues = check_reading(reading)
        if issues:
            self.stats["malformed"] += 1
            log.warning("machine=%s dropped malformed reading: %s", self.machine_id, issues)
            return False
        if reading["machine_id"] != self.machine_id:
            self.stats["malformed"] += 1
            log.warning("machine=%s dropped reading for %s", self.machine_id, reading["machine_id"])
            return False

        t = parse_timestamp(reading["timestamp"])
        if self.last_time is not None:
            gap = (t - self.last_time).total_seconds()
            if gap <= 0:
                self.stats["out_of_order"] += 1
                log.warning("machine=%s dropped out-of-order reading %s", self.machine_id, reading["timestamp"])
                return False
            if gap > MAX_GAP_S:
                self.stats["gaps"] += 1
                self.high_since = None
                self.low_streak = 0
                log.warning("machine=%s no data for %.0f s: persistence restarted", self.machine_id, gap)

        signals = detect_spikes(self.machine_id, reading, self.baseline)
        self.latest_spikes = {s.sensor: s for s in signals}
        self.spike_log.append((t, {s.sensor: (s.triggered, s.score) for s in signals}))
        self.window.append((t, reading))
        self.last_time = t
        self.stats["readings"] += 1

        while self.window and (t - self.window[0][0]).total_seconds() > WINDOW_KEEP_S:
            self.window.popleft()
        while self.spike_log and (t - self.spike_log[0][0]).total_seconds() > SPIKE_LOOKBACK_S:
            self.spike_log.popleft()
        return True

    # -- evaluation ----------------------------------------------------------
    def evaluate_if_due(self):
        """Evaluate if EVAL_INTERVAL_S of data time passed since the last time. Else None."""
        if self.last_time is None:
            return None
        if self.last_eval is not None and (self.last_time - self.last_eval).total_seconds() < EVAL_INTERVAL_S:
            return None
        self.last_eval = self.last_time
        return self.evaluate()

    def evaluate(self):
        """Run all detectors on the current window. Returns (Assessment, Decision)."""
        evidence = []
        evidence.extend(self._spike_evidence())
        window = list(self.window)
        for signal in detect_drift(self.machine_id, window, self.baseline):
            if signal.triggered:
                evidence.append(Evidence("drift", signal.sensor, (signal.sensor,), signal.score, signal.evidence))
        for signal in detect_patterns(self.machine_id, window, self.baseline):
            if signal.triggered:
                name = signal.evidence["pattern"]
                evidence.append(Evidence("pattern", name, self._pattern_sensors(signal), signal.score, signal.evidence))

        level, risk = combine(evidence)
        assessment = Assessment(self.machine_id, self.last_time, level, risk, tuple(evidence), window[-1][1])
        self.stats["evaluations"] += 1
        return assessment, self._decide(level, self.last_time)

    def _spike_evidence(self):
        """A sensor is sustained-spiking if it spiked in >= SPIKE_SUSTAINED_SHARE of the last 60 s."""
        n = len(self.spike_log)
        if n < SPIKE_MIN_READINGS:
            return []
        items = []
        for sensor, latest in self.latest_spikes.items():
            hits = sum(1 for _, flags in self.spike_log if flags[sensor][0])
            share = hits / n
            if share >= SPIKE_SUSTAINED_SHARE:
                peak = max(flags[sensor][1] for _, flags in self.spike_log)
                detail = dict(latest.evidence, share=share, hits=hits, n_readings=n,
                              lookback_s=SPIKE_LOOKBACK_S, peak_score=peak)
                items.append(Evidence("spike", sensor, (sensor,), share / SPIKE_SUSTAINED_SHARE, detail))
        return items

    @staticmethod
    def _pattern_sensors(signal):
        """Which sensors a pattern signal involves."""
        name = signal.evidence["pattern"]
        if name == "vibration_variability":
            return ("vibration_mm_s",)
        if name == "rpm_decline_with_current":
            return ("rpm", "current_A")
        pair = signal.evidence.get("dominant_pair")
        return tuple(pair.split("+")) if pair else ()

    # -- persistence and cooldown ----------------------------------------------
    def _decide(self, level, now):
        if level != "high":
            if self.high_since is not None:
                self.low_streak += 1
                if self.low_streak >= PERSISTENCE_RESET_EVALS:
                    self.high_since = None
                    self.low_streak = 0
            return Decision("none", f"level {level}")

        self.low_streak = 0
        if self.high_since is None:
            self.high_since = now
        held = (now - self.high_since).total_seconds()
        if held < PERSISTENCE_S:
            return Decision("waiting", f"high for {held:.0f} s of {PERSISTENCE_S} s required")
        if self.last_alert_time is not None:
            since = (now - self.last_alert_time).total_seconds()
            if since < COOLDOWN_S:
                return Decision("cooldown", f"alerted {since:.0f} s ago; cooldown is {COOLDOWN_S} s")
        return Decision("alert", f"high for {held:.0f} s")

    def confirm_alert(self, now):
        """Call after an alert was POSTed successfully: starts the cooldown."""
        self.last_alert_time = now
        self.stats["alerts"] += 1


# ---------------------------------------------------------------------------
# Alert text (Phase 5 replaces this with a richer explain.py; the hook stays the same)
# ---------------------------------------------------------------------------
def _describe(item):
    d = item.detail
    unit = SENSOR_UNITS.get(item.name, "")
    if item.detector == "spike":
        return (f"{item.name} was abnormal in {d['share']:.0%} of the last {d['lookback_s']} s "
                f"(latest {d['value']:.1f} {unit} vs normal {d['baseline']:.1f}).")
    if item.detector == "drift":
        return (f"{item.name} is trending {d['direction']}: {d['change']:+.2f} {unit} over "
                f"{d['window_s']:.0f} s ({d['slope_per_min']:+.3f} {unit}/min).")
    if item.name == "vibration_variability":
        return (f"vibration is {d['ratio']:.1f}x more variable than normal "
                f"(std {d['std']:.2f} mm/s over {d['window_s']:.0f} s).")
    if item.name == "co_elevation":
        return (f"{' and '.join(item.sensors)} were abnormal at the same time in {d['share']:.0%} "
                f"of the last {d['window_s']:.0f} s.")
    return (f"rpm {d['mean_rpm']:.0f} vs normal {d['baseline_rpm']:.0f} with current "
            f"{d['mean_current']:.1f} A vs normal {d['baseline_current']:.1f} A.")


def simple_reason(assessment):
    """A short, factual explanation built from the real measurements. No claims of certain failure."""
    parts = [_describe(item) for item in sorted(assessment.evidence, key=lambda e: -e.score)]
    return (f"{assessment.machine_id}: sustained abnormal behaviour (risk score {assessment.risk_score:.1f}). "
            + " ".join(parts) + " Inspection recommended.")


# ---------------------------------------------------------------------------
# I/O: posting alerts
# ---------------------------------------------------------------------------
def post_alert(base_url, machine_id, reason, reading):
    """
    POST /alert. Returns the stored alert (a dict) on success, or None on any failure.
    Contract (read from server.js in Phase 1): body {machine_id, reason, reading?},
    answer HTTP 201 {success: true, alert: {...}}.
    """
    try:
        response = requests.post(
            base_url + "/alert",
            json={"machine_id": machine_id, "reason": reason, "reading": reading},
            timeout=POST_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        log.error("machine=%s POST /alert failed: %s", machine_id, exc)
        return None
    if response.status_code != 201:
        log.error("machine=%s POST /alert returned HTTP %s: %s", machine_id, response.status_code, response.text[:200])
        return None
    try:
        return response.json().get("alert", {})
    except ValueError:
        log.error("machine=%s POST /alert: answer was not valid JSON", machine_id)
        return None


# ---------------------------------------------------------------------------
# I/O: streaming with reconnection
# ---------------------------------------------------------------------------
def read_stream_lines(base_url, machine_id, stop_event):
    """
    Open the SSE stream and yield the raw text after each 'data:' line, until the stream ends,
    breaks (an exception from requests) or stop_event is set.
    On the wire: ': connected ...' comment, blank lines, and 'data: {json}' lines.
    """
    url = f"{base_url}/stream/{machine_id}"
    with requests.get(url, stream=True, timeout=(STREAM_CONNECT_TIMEOUT_S, STREAM_READ_TIMEOUT_S)) as response:
        if response.status_code != 200:
            raise requests.exceptions.RequestException(f"stream returned HTTP {response.status_code}")
        log.info("machine=%s stream connected", machine_id)
        for raw_line in response.iter_lines():
            if stop_event.is_set():
                return
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                yield line[len("data:"):].strip()


def handle_result(monitor, result, base_url, dry_run, explain):
    """Log one evaluation, and send the alert if the decision says so."""
    assessment, decision = result
    evidence_text = " ".join(item.short() for item in assessment.evidence) or "-"
    log.info("machine=%s level=%s risk=%.1f decision=%s (%s) evidence=[%s]",
             assessment.machine_id, assessment.level, assessment.risk_score,
             decision.action, decision.note, evidence_text)
    if assessment.evidence and (decision.action == "alert" or log.isEnabledFor(logging.DEBUG)):
        for item in assessment.evidence:
            log.debug("machine=%s detector=%s name=%s score=%.2f evidence=%s",
                      assessment.machine_id, item.detector, item.name, item.score,
                      json.dumps(item.detail, default=str))

    if decision.action != "alert":
        return
    reason = explain(assessment)
    if dry_run:
        log.warning("machine=%s DRY RUN, would alert: %s", assessment.machine_id, reason)
        monitor.confirm_alert(assessment.time)
        return
    stored = post_alert(base_url, assessment.machine_id, reason, assessment.latest_reading)
    if stored is None:
        log.error("machine=%s alert NOT sent; will retry at the next evaluation", assessment.machine_id)
        return
    monitor.confirm_alert(assessment.time)
    log.warning("machine=%s ALERT SENT id=%s reason=%s", assessment.machine_id, stored.get("id"), reason)


def run_machine(base_url, monitor, stop_event, dry_run, explain):
    """Thread body: stream one machine until stopped, reconnecting whenever the stream drops."""
    attempt = 0
    while not stop_event.is_set():
        try:
            for text in read_stream_lines(base_url, monitor.machine_id, stop_event):
                try:
                    reading = json.loads(text)
                except ValueError:
                    monitor.stats["malformed"] += 1
                    log.warning("machine=%s dropped stream line that is not JSON: %r", monitor.machine_id, text[:100])
                    continue
                attempt = 0  # data is flowing again: forget earlier failures
                if monitor.add_reading(reading):
                    result = monitor.evaluate_if_due()
                    if result is not None:
                        handle_result(monitor, result, base_url, dry_run, explain)
            if not stop_event.is_set():
                log.warning("machine=%s stream closed by the server", monitor.machine_id)
        except requests.exceptions.RequestException as exc:
            if not stop_event.is_set():
                log.warning("machine=