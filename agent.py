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

History check (once, at startup): detector.detect_history_shift compares each sensor's median
on the newest full day of the 7-day history with normal. A shift of HISTORY_SHIFT_SPREADS or
more in the harmful direction is slow, multi-day degradation the live stream cannot show
(CNC_01's bearing wear, PUMP_03's falling rpm). It produces at most ONE alert per machine per
run, sent before streaming starts and retried until a POST succeeds. It neither uses nor starts
the live cooldown: it is about days, the live rules are about minutes.

Every alert names a likely cause (chosen from the evidence, never from the machine's name), the
measured evidence and a suggested check.

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
from detector import detect_drift, detect_history_shift, detect_patterns, detect_spikes

log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Settings (README.md, "Configuration and thresholds", lists every value and its reason)
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
SENSOR_NAMES = {"temperature_C": "temperature", "vibration_mm_s": "vibration", "rpm": "speed",
                "current_A": "motor current"}
DECIMALS = {"temperature_C": 1, "vibration_mm_s": 2, "rpm": 0, "current_A": 1}   # in alert texts


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
        # History check: computed once from the baseline's daily statistics (no network).
        self.history_signals, self.history_note = self._check_history()
        self.history_evidence = tuple(
            Evidence("history", s.sensor, (s.sensor,), s.score, s.evidence)
            for s in self.history_signals if s.triggered)
        self.history_alert_sent = False

    def _check_history(self):
        """Run the history check. Returns (signals, note). Never raises: a damaged history part
        of the baseline must not stop the live monitoring."""
        try:
            signals = detect_history_shift(self.machine_id, self.baseline)
        except (KeyError, TypeError, ValueError) as exc:
            return (), f"not evaluated: damaged daily statistics in the baseline ({type(exc).__name__}: {exc})"
        if not signals:
            return (), ("not evaluated: the baseline has no daily medians (a file saved before this check "
                        "existed?) or no full day after the reference window")
        return tuple(signals), "evaluated"

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

    # -- the history alert (at most once per run) --------------------------------
    def history_alert_due(self):
        """True while the history check found a shift and no alert about it has been sent yet."""
        return bool(self.history_evidence) and not self.history_alert_sent

    def confirm_history_alert(self):
        """Call after the history alert was POSTed successfully (or logged in a dry run).
        Deliberately leaves the live cooldown alone."""
        self.history_alert_sent = True
        self.stats["history_alerts"] += 1


# ---------------------------------------------------------------------------
# Alert text, built only from the measured evidence (run_agent's explain= and explain_history=
# arguments can swap it). No claims of certain failure. Never put "<" in it: the simulator's
# dashboard inserts the reason into its page as HTML.
# ---------------------------------------------------------------------------
# Likely causes: key -> (cause, what to check). The wording starts from the assignment's own
# sensor table (rising temperature = thermal stress or cooling failure, increasing vibration =
# bearing wear, rpm drift = load or mechanical issue, high current = motor overload) and adds
# common combinations of those signs. A cause is chosen from the EVIDENCE, never from the
# machine's name. It is a hint for the person who inspects the machine, not a diagnosis: the
# agent sees four sensors only.
LIKELY_CAUSES = {
    "thermal_runaway": ("overheating under load (thermal runaway)", "cooling (coolant flow, fans) and machine load"),
    "cavitation": ("cavitation or intermittent mechanical shocks", "inlet side (blockage, trapped air) and mountings"),
    "friction": ("friction heating, such as a failing bearing or poor lubrication", "lubrication and bearings"),
    "bearing_wear": ("bearing wear", "bearing condition and lubrication"),
    "wear_or_imbalance": ("bearing wear or imbalance", "bearings and balance of rotating parts"),
    "clog": ("a growing restriction or load slowing the machine, such as a developing clog",
             "blockage at the inlet, filter or impeller, or a dragging drive"),
    "cooling": ("thermal stress or a cooling failure", "cooling system"),
    "overload": ("motor overload", "machine load and motor"),
    "speed": ("speed instability (a load or drive problem)", "drive and load"),
    "shocks": ("mechanical shocks or looseness", "mountings and rotating parts"),
    "unknown": ("unclear; several sensors are abnormal", "the whole machine"),
}
MAX_CAUSES = 2


def live_causes(evidence):
    """Likely-cause keys (LIKELY_CAUSES) for live evidence, most specific first."""
    names = {(item.detector, item.name) for item in evidence}
    pairs = {item.detail.get("dominant_pair") for item in evidence if item.name == "co_elevation"}

    def on(sensor):
        return any(sensor in item.sensors for item in evidence)

    keys = []
    if on("temperature_C") and on("current_A"):
        keys.append("thermal_runaway")
    if ("pattern", "vibration_variability") in names:
        keys.append("cavitation")
    if "temperature_C+vibration_mm_s" in pairs:
        keys.append("friction")
    if ("pattern", "rpm_decline_with_current") in names or ("drift", "rpm") in names:
        keys.append("clog")
    if ("drift", "vibration_mm_s") in names:
        keys.append("wear_or_imbalance")
    # The single-sensor causes only when nothing more specific already explains that sensor.
    if on("temperature_C") and not {"thermal_runaway", "friction"} & set(keys):
        keys.append("cooling")
    if on("current_A") and not {"thermal_runaway", "cavitation", "clog"} & set(keys):
        keys.append("overload")
    if ("spike", "rpm") in names and "clog" not in keys:
        keys.append("speed")
    if on("vibration_mm_s") and not {"cavitation", "friction", "wear_or_imbalance"} & set(keys):
        keys.append("shocks")
    return keys[:MAX_CAUSES] or ["unknown"]


def history_causes(evidence):
    """Likely-cause keys for history evidence (the sensors whose daily level has shifted)."""
    shifted = {item.name for item in evidence}
    keys = []
    if "vibration_mm_s" in shifted:
        keys.append("bearing_wear" if shifted & {"temperature_C", "current_A"} else "wear_or_imbalance")
    if "rpm" in shifted:
        keys.append("clog")
    if "vibration_mm_s" not in shifted:
        if {"temperature_C", "current_A"} <= shifted:
            keys.append("thermal_runaway")
        elif "temperature_C" in shifted:
            keys.append("cooling")
        elif "current_A" in shifted and "rpm" not in shifted:
            keys.append("overload")
    return keys[:MAX_CAUSES] or ["unknown"]


def _causes_text(keys):
    """('cause A; or cause B', 'check A; check B') for the alert text."""
    return ("; or ".join(LIKELY_CAUSES[k][0] for k in keys), "; ".join(LIKELY_CAUSES[k][1] for k in keys))


def _value(sensor, value):
    return f"{value:.{DECIMALS[sensor]}f}"


def _describe(item):
    """One piece of live evidence in plain words, with the measured numbers."""
    d = item.detail
    if item.detector == "spike":
        side = {"both": "outside", "low": "below"}.get(d.get("direction"), "above")
        return (f"{SENSOR_NAMES[item.name]} {side} its normal range in {d['share']:.0%} of the last "
                f"{d['lookback_s']} s (latest {_value(item.name, d['value'])} {SENSOR_UNITS[item.name]}, "
                f"normal {_value(item.name, d['baseline'])})")
    if item.detector == "drift":
        return (f"{SENSOR_NAMES[item.name]} {'rising' if d['change'] > 0 else 'falling'} steadily: "
                f"{d['change']:+.{DECIMALS[item.name]}f} {SENSOR_UNITS[item.name]} over the last {d['window_s']:.0f} s")
    if item.name == "vibration_variability":
        return (f"vibration {d['ratio']:.1f}x more erratic than normal "
                f"(std {d['std']:.2f} mm/s over the last {d['window_s']:.0f} s)")
    if item.name == "co_elevation":
        sensors = " and ".join(SENSOR_NAMES[s] for s in item.sensors) or "two or more sensors"
        return f"{sensors} high at the same moment in {d['share']:.0%} of the last {d['window_s']:.0f} s"
    return (f"speed {d['mean_rpm']:.0f} rpm vs normal {d['baseline_rpm']:.0f} while motor current is "
            f"{d['mean_current']:.1f} A vs normal {d['baseline_current']:.1f}")


def simple_reason(assessment):
    """Live alert text: likely cause, the measured evidence (strongest first), what to check."""
    causes, checks = _causes_text(live_causes(assessment.evidence))
    facts = "; ".join(_describe(item) for item in sorted(assessment.evidence, key=lambda e: -e.score))
    return (f"{assessment.machine_id}: abnormal behaviour in the live stream. Likely cause: {causes}. "
            f"Evidence: {facts}. Suggested check: {checks}.")


def _describe_history(item, with_trend):
    """One shifted sensor from the history check. with_trend (used for the strongest sensor)
    adds the day-by-day medians and since when the shift has held without a break."""
    d, sensor = item.detail, item.name
    unit = SENSOR_UNITS[sensor]
    text = (f"{SENSOR_NAMES[sensor]} {_value(sensor, d['median'])} {unit} vs "
            f"{'nominal' if d['normal_source'] == 'nominal' else 'normal'} {_value(sensor, d['normal'])}")
    if d["change_pct"] is not None:
        text += f" ({d['change_pct']:+.0f}%)"
    if not with_trend:
        return text
    if len(d["series"]) >= 2:
        text += (f", day {d['series_days'][0]} to {d['series_days'][-1]}: "
                 + " -> ".join(_value(sensor, v) for v in d["series"]))
        if d["slope_per_day"] is not None:
            text += f" (about {d['slope_per_day']:+.{DECIMALS[sensor]}f} {unit} per day)"
    if d["since_day"] is not None:
        text += f", {'above' if d['direction'] == 'high' else 'below'} normal since day {d['since_day']}"
    return text


def history_reason(machine_id, evidence):
    """History alert text: likely cause, the shifted daily levels (strongest first), what to check."""
    causes, checks = _causes_text(history_causes(evidence))
    ordered = sorted(evidence, key=lambda e: -e.score)
    facts = "; ".join(_describe_history(item, with_trend=(i == 0)) for i, item in enumerate(ordered))
    days = max(item.detail["day"] for item in evidence)
    return (f"{machine_id}: warning from the {days}-day history. Likely cause: {causes}. "
            f"Evidence (daily medians): {facts}. Suggested check: {checks}.")


# ---------------------------------------------------------------------------
# I/O: posting alerts
# ---------------------------------------------------------------------------
def post_alert(base_url, machine_id, reason, reading):
    """
    POST /alert. Returns the stored alert (a dict) on success, or None on failure; after a
    failure no cooldown starts, so the agent tries again at the next evaluation.
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
    # HTTP 201 means the server stored the alert. An unexpected answer body must not make the
    # agent send it again (that would store a duplicate), so it only costs us the alert id.
    try:
        answer = response.json()
    except ValueError:
        answer = None
    alert = answer.get("alert") if isinstance(answer, dict) else None
    if not isinstance(alert, dict):
        log.warning("machine=%s POST /alert: HTTP 201 but unexpected answer %r; counting the alert as sent",
                    machine_id, response.text[:200])
        return {}
    return alert


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


def send_history_alert(monitor, base_url, dry_run, explain_history):
    """
    Send this machine's history alert if one is due: at most once per run, retried at the next
    call until a POST succeeds. A dry run only logs it. No live reading caused it, so no reading
    is attached (the field is optional). The live cooldown is neither checked nor started.
    """
    if not monitor.history_alert_due():
        return
    reason = explain_history(monitor.machine_id, monitor.history_evidence)
    if dry_run:
        log.warning("machine=%s DRY RUN, would alert (history): %s", monitor.machine_id, reason)
        monitor.confirm_history_alert()
        return
    stored = post_alert(base_url, monitor.machine_id, reason, None)
    if stored is None:
        log.error("machine=%s history alert NOT sent; will retry", monitor.machine_id)
        return
    monitor.confirm_history_alert()
    log.warning("machine=%s ALERT SENT (history) id=%s reason=%s", monitor.machine_id, stored.get("id"), reason)


def run_machine(base_url, monitor, stop_event, dry_run, explain, explain_history=history_reason):
    """Thread body: send the history alert if due, then stream one machine until stopped,
    reconnecting whenever the stream drops."""
    attempt = 0
    while not stop_event.is_set():
        try:
            # First thing on every (re)connection; does nothing once the history alert is sent.
            send_history_alert(monitor, base_url, dry_run, explain_history)
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
                        # Retry a history alert whose POST failed, at the live retry rhythm.
                        send_history_alert(monitor, base_url, dry_run, explain_history)
            if not stop_event.is_set():
                log.warning("machine=%s stream closed by the server", monitor.machine_id)
        except requests.exceptions.RequestException as exc:
            if not stop_event.is_set():
                log.warning("machine=%s stream error: %s", monitor.machine_id, exc)
                monitor.stats["stream_errors"] += 1
        except Exception:
            # Last resort. A bug, or an answer we did not expect, must never kill this machine's
            # thread (or the other machines). Log the full traceback, then reconnect as usual.
            monitor.stats["unexpected_errors"] += 1
            log.exception("machine=%s unexpected error; will reconnect", monitor.machine_id)

        if stop_event.is_set():
            break
        # Wait longer after each failure in a row: 1, 2, 4, 8, 15, then 30 s again and again.
        # `attempt` goes back to 0 as soon as data flows again (see above).
        delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
        attempt += 1
        monitor.stats["reconnects"] += 1
        log.info("machine=%s reconnecting in %s s (attempt %d)", monitor.machine_id, delay, attempt)
        stop_event.wait(delay)  # returns at once if the agent is stopping
    log.info("machine=%s monitor stopped", monitor.machine_id)


# ---------------------------------------------------------------------------
# I/O: running all machines together
# ---------------------------------------------------------------------------
# When stopping, how long to wait for the machine threads in total. A thread can be waiting up
# to STREAM_READ_TIMEOUT_S (10 s) for the next stream line, or finishing a POST that may take up
# to POST_TIMEOUT_S (10 s).
SHUTDOWN_WAIT_S = 15
# How often the main thread wakes up to check the --duration deadline. Short naps also make
# Ctrl+C work promptly on Windows, where a long blocking wait can delay it.
MAIN_LOOP_NAP_S = 0.5


def log_history_check(monitor):
    """One startup line per machine with the verdict of the history check."""
    machine_id = monitor.machine_id
    if not monitor.history_signals:
        log.warning("machine=%s history check %s", machine_id, monitor.history_note)
    elif monitor.history_evidence:
        found = "; ".join(
            f"{item.name} {item.detail['shift_spreads']:+.1f} spreads (day {item.detail['day']} median "
            f"{item.detail['median']:g} vs normal {item.detail['normal']:g})" for item in monitor.history_evidence)
        log.warning("machine=%s history check: SHIFT found: %s", machine_id, found)
    else:
        top = max(monitor.history_signals, key=lambda s: s.evidence["shift_spreads"])
        log.info("machine=%s history check: no sustained shift (largest: %s %+.1f spreads; alert at %.1f)",
                 machine_id, top.sensor, top.evidence["shift_spreads"], top.evidence["threshold_spreads"])


def run_agent(base_url, baseline, dry_run=False, duration=None, explain=simple_reason,
              explain_history=history_reason):
    """
    Monitor every machine in the baseline, one thread per machine, until Ctrl+C, until
    `duration` seconds (wall clock) have passed, or until every thread has stopped.
    explain(assessment) -> str builds the live alert text (default: simple_reason);
    explain_history(machine_id, evidence) -> str the history alert text (default: history_reason).
    Returns {machine_id: MachineMonitor}, so the caller can print a summary (and tests can
    look at the counters).
    """
    stop_event = threading.Event()  # the shared "please stop" flag that every thread checks
    monitors = {machine_id: MachineMonitor(machine_id, baseline) for machine_id in baseline["machines"]}
    for monitor in monitors.values():
        log_history_check(monitor)
    threads = []
    try:
        for machine_id, monitor in monitors.items():
            # daemon=True: a thread that is still stuck when the program ends cannot keep the
            # program alive.
            thread = threading.Thread(target=run_machine, name=machine_id, daemon=True,
                                      args=(base_url, monitor, stop_event, dry_run, explain, explain_history))
            thread.start()
            threads.append(thread)
        log.info("monitoring %d machines: %s", len(threads), ", ".join(monitors))

        deadline = None if duration is None else time.monotonic() + duration
        while True:
            if not any(thread.is_alive() for thread in threads):
                log.error("all machine threads have stopped; ending the run")
                break
            if deadline is not None and time.monotonic() >= deadline:
                log.info("duration of %g s reached; stopping", duration)
                break
            time.sleep(MAIN_LOOP_NAP_S)
    except KeyboardInterrupt:
        # Python delivers Ctrl+C to the main thread only, so it always arrives here.
        log.info("Ctrl+C received; stopping (this can take a few seconds)")
    finally:
        stop_event.set()
        give_up_at = time.monotonic() + SHUTDOWN_WAIT_S
        for thread in threads:
            thread.join(max(0.0, give_up_at - time.monotonic()))
        still_busy = [thread.name for thread in threads if thread.is_alive()]
        if still_busy:
            log.warning("threads still busy after %s s, ending without them: %s",
                        SHUTDOWN_WAIT_S, ", ".join(still_busy))
    return monitors


def print_summary(monitors, dry_run=False):
    """
    Log one line of counters per machine (so it reaches both the console and the log file):
      readings, evaluations   accepted readings, and how often the detectors ran
      alerts                  live alerts sent; in a dry run, alerts that WOULD have been sent
      history_alerts          history alerts sent (at most 1 per run); in a dry run, would-be ones
      malformed, out_of_order rejected stream lines and readings (warnings in the log say why)
      gaps                    silences longer than MAX_GAP_S between two readings
      reconnects              times the stream was opened again (or tried) after it ended
      stream_errors           connection problems: refused, timed out, broken, bad HTTP status
      unexpected_errors       exceptions caught by the last-resort handler (tracebacks in the log)
    """
    keys = ("readings", "evaluations", "alerts", "history_alerts", "malformed", "out_of_order", "gaps",
            "reconnects", "stream_errors", "unexpected_errors")
    log.info("Summary%s:", " (DRY RUN: 'alerts' are alerts that would have been sent)" if dry_run else "")
    for machine_id, monitor in monitors.items():
        log.info("  %-12s %s", machine_id, " ".join(f"{key}={monitor.stats[key]}" for key in keys))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def setup_logging(log_file):
    """
    Console: INFO and above (one line per evaluation, alerts, connection problems).
    Log file: DEBUG and above, i.e. also every active evidence item as JSON. The file is
    appended to, so earlier runs are kept. Raises OSError if the file cannot be opened.
    """
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")  # may raise OSError
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handle_result() checks this before writing the JSON evidence lines
    root.addHandler(file_handler)
    root.addHandler(console)
    # requests uses urllib3, which logs every HTTP connection at DEBUG; keep only its warnings.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def prepare_baseline(base_url, saved_path, save_path):
    """
    Return the baseline the agent will use.
      saved_path given : load that file (written earlier by baseline.py or by this agent).
      otherwise        : rebuild it from the simulator we are about to monitor (its history is
                         regenerated at every simulator restart) and keep a copy in save_path.
    Raises SimulatorError, OSError, KeyError, TypeError or ValueError with a readable message.
    """
    if saved_path:
        baseline = load_baseline(saved_path)
        machines = baseline.get("machines") if isinstance(baseline, dict) else None
        if not isinstance(machines, dict) or not machines:
            raise ValueError(f"{saved_path} is not a baseline file (no 'machines' section)")
        meta = baseline.get("meta") if isinstance(baseline.get("meta"), dict) else {}
        log.info("using saved baseline %s (created %s from %s)", saved_path,
                 meta.get("created_at", "?"), meta.get("source", "?"))
        return baseline

    log.info("building the baseline from %s (7 days of history per machine)", base_url)
    baseline = build_from_server(base_url, progress=log.info)
    try:
        save_baseline(baseline, save_path)
        log.info("baseline saved to %s", save_path)
    except OSError as exc:
        # The copy is only for inspection and reuse; the agent can run without it.
        log.warning("could not save the baseline to %s (%s); continuing", save_path, exc)
    return baseline


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Phase 4: monitor all machines and raise alerts.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="simulator address")
    parser.add_argument("--log-file", default="agent.log",
                        help="full log including the DEBUG evidence, appended (default: agent.log)")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and log, but never POST alerts")
    parser.add_argument("--duration", type=float, metavar="SECONDS",
                        help="stop by itself after this many seconds (default: run until Ctrl+C)")
    parser.add_argument("--use-saved-baseline", metavar="FILE",
                        help="load this baseline file instead of rebuilding it from the simulator")
    parser.add_argument("--save-baseline", metavar="FILE", default="agent_baseline.json",
                        help="where the rebuilt baseline is written (default: agent_baseline.json); "
                             "not used with --use-saved-baseline")
    args = parser.parse_args(argv)
    if args.duration is not None and not args.duration > 0:  # "not > 0" also rejects nan
        parser.error("--duration must be more than 0 seconds")
    return args


def main(argv=None):
    args = parse_args(argv)
    base_url = args.base_url.rstrip("/")
    try:
        setup_logging(args.log_file)
    except OSError as exc:
        print(f"ERROR: cannot open the log file {args.log_file}: {exc}")
        return 1

    log.info("=" * 70)
    log.info("agent starting: %s; simulator %s; log file %s",
             "DRY RUN (alerts are only logged)" if args.dry_run else "LIVE (alerts are POSTed)",
             base_url, args.log_file)
    log.info("settings: evaluate every %s s, persistence %s s, cooldown %s s, duration %s",
             EVAL_INTERVAL_S, PERSISTENCE_S, COOLDOWN_S,
             f"{args.duration:g} s" if args.duration else "until Ctrl+C")
    try:
        baseline = prepare_baseline(base_url, args.use_saved_baseline, args.save_baseline)
    except (SimulatorError, OSError, KeyError, TypeError, ValueError) as exc:
        log.error("cannot start: %s: %s", type(exc).__name__, exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted before monitoring started")
        return 130

    log.warning("REMINDER: close the simulator's browser dashboard. Each extra stream connection "
                "makes that machine run faster and splits its readings between the clients.")
    try:
        monitors = run_agent(base_url, baseline, dry_run=args.dry_run, duration=args.duration)
    except KeyboardInterrupt:
        log.warning("interrupted again while stopping; exiting without waiting for the threads")
        return 130
    print_summary(monitors, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
