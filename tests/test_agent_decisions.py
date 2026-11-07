"""agent.py's decision core: combining evidence, persistence, cooldown and input checks,
plus the recorded captures and a long healthy stream replayed through MachineMonitor."""
import random
import unittest

from tests import helpers
from tests.helpers import TEST_MACHINE as M, at, make_baseline, reading
import agent
from agent import Evidence, MachineMonitor, combine


def evidence(detector, name, sensors, score=1.5):
    return Evidence(detector, name, tuple(sensors), score, {})


class CombineTest(unittest.TestCase):
    def test_no_evidence(self):
        self.assertEqual(combine([]), ("none", 0.0))

    def test_one_detector_on_one_sensor_is_only_watch(self):
        self.assertEqual(combine([evidence("spike", "vibration_mm_s", ["vibration_mm_s"])])[0], "watch")

    def test_two_detectors_on_the_same_single_sensor_is_still_watch(self):
        # what the healthy conveyor's vibration bursts produce
        level, _ = combine([evidence("spike", "vibration_mm_s", ["vibration_mm_s"]),
                            evidence("pattern", "vibration_variability", ["vibration_mm_s"])])
        self.assertEqual(level, "watch")

    def test_one_detector_on_two_sensors_is_still_watch(self):
        level, _ = combine([evidence("spike", "temperature_C", ["temperature_C"]),
                            evidence("spike", "current_A", ["current_A"])])
        self.assertEqual(level, "watch")

    def test_two_detectors_and_two_sensors_is_high(self):
        level, risk = combine([evidence("spike", "temperature_C", ["temperature_C"], 1.2),
                               evidence("spike", "current_A", ["current_A"], 2.0),
                               evidence("pattern", "co_elevation", ["temperature_C", "current_A"], 4.0)])
        self.assertEqual(level, "high")
        self.assertAlmostEqual(risk, 2.0 + 4.0)   # the strongest score of each detector kind


class PersistenceAndCooldownTest(unittest.TestCase):
    """_decide() fed one risk level per evaluation (every EVAL_INTERVAL_S of data time)."""

    def setUp(self):
        self.monitor = MachineMonitor(M, make_baseline())

    def run_levels(self, levels, start=0):
        """Returns {seconds: action} for one evaluation every 5 s starting at `start`."""
        step = agent.EVAL_INTERVAL_S
        return {start + i * step: self.monitor._decide(level, at(start + i * step)).action
                for i, level in enumerate(levels)}

    def test_high_must_hold_for_the_persistence_time(self):
        actions = self.run_levels(["high"] * 13)                      # t = 0, 5, ..., 60
        self.assertEqual([actions[t] for t in range(0, 60, 5)], ["waiting"] * 12)
        self.assertEqual(actions[agent.PERSISTENCE_S], "alert")

    def test_one_short_dip_does_not_restart_the_clock(self):
        actions = self.run_levels(["high"] * 7 + ["watch"] + ["high"] * 5)   # dip at 35 s
        self.assertEqual(actions[35], "none")
        self.assertEqual(actions[60], "alert")

    def test_two_dips_in_a_row_restart_the_clock(self):
        actions = self.run_levels(["high"] * 7 + ["watch", "none"] + ["high"] * 13)   # dips at 35, 40 s
        self.assertEqual(actions[60], "waiting")
        self.assertEqual(actions[100], "waiting")    # high again since 45 s: only 55 s so far
        self.assertEqual(actions[105], "alert")

    def test_cooldown_after_a_successful_alert(self):
        self.run_levels(["high"] * 13)                                 # alert at 60 s
        self.monitor.confirm_alert(at(60))                             # the POST worked
        actions = self.run_levels(["high"] * 120, start=65)            # 65 s ... 660 s, still high
        self.assertTrue(all(actions[t] == "cooldown" for t in range(65, 660, 5)))
        self.assertEqual(actions[60 + agent.COOLDOWN_S], "alert")      # cooldown over, still faulty
        self.assertEqual(self.monitor.stats["alerts"], 1)

    def test_failed_post_means_a_retry_at_the_next_evaluation(self):
        actions = self.run_levels(["high"] * 14)                       # alert at 60 s, never confirmed
        self.assertEqual((actions[60], actions[65]), ("alert", "alert"))

    def test_new_episode_after_the_cooldown_needs_full_persistence(self):
        self.run_levels(["high"] * 13)
        self.monitor.confirm_alert(at(60))
        self.run_levels(["none"] * 10, start=65)                       # the problem went away
        actions = self.run_levels(["high"] * 13, start=700)            # and came back at 700 s
        self.assertEqual((actions[755], actions[760]), ("waiting", "alert"))


class InputHandlingTest(unittest.TestCase):
    def setUp(self):
        self.monitor = MachineMonitor(M, make_baseline())

    def test_bad_readings_are_counted_and_never_crash(self):
        good = reading(at(0))
        bad = [
            "not a dict",
            {k: v for k, v in good.items() if k != "rpm"},
            dict(good, temperature_C="hot"),
            dict(good, rpm=float("nan")),
            dict(good, timestamp="2026-11-05T05:00:00"),   # no time zone
            reading(at(0), machine="CNC_02"),             # another machine's reading
        ]
        with self.assertLogs("agent", level="WARNING"):
            results = [self.monitor.add_reading(r) for r in bad]
        self.assertEqual(results, [False] * len(bad))
        self.assertEqual((self.monitor.stats["malformed"], self.monitor.stats["readings"]), (len(bad), 0))

    def test_timestamp_without_time_zone_after_a_good_reading(self):
        # regression: this used to raise TypeError (naive minus aware datetime) in add_reading
        self.assertTrue(self.monitor.add_reading(reading(at(0))))
        with self.assertLogs("agent", level="WARNING"):
            self.assertFalse(self.monitor.add_reading(dict(reading(at(1)), timestamp="2026-11-05T05:00:01")))
        self.assertEqual(self.monitor.stats["malformed"], 1)

    def test_old_or_repeated_timestamps_are_rejected(self):
        self.assertTrue(self.monitor.add_reading(reading(at(10))))
        with self.assertLogs("agent", level="WARNING"):
            self.assertFalse(self.monitor.add_reading(reading(at(10))))
            self.assertFalse(self.monitor.add_reading(reading(at(5))))
        self.assertEqual(self.monitor.stats["out_of_order"], 2)

    def test_a_long_silence_restarts_persistence(self):
        self.monitor.add_reading(reading(at(0)))
        self.monitor.high_since = at(0)   # as if "high" had been building up
        with self.assertLogs("agent", level="WARNING") as logs:
            self.monitor.add_reading(reading(at(agent.MAX_GAP_S + 5)))
        self.assertIsNone(self.monitor.high_since)
        self.assertEqual(self.monitor.stats["gaps"], 1)
        self.assertIn("persistence restarted", logs.output[0])

    def test_memory_stays_bounded(self):
        for t in range(1000):
            self.monitor.add_reading(reading(at(t)))
        span = (self.monitor.window[-1][0] - self.monitor.window[0][0]).total_seconds()
        self.assertLessEqual(span, agent.WINDOW_KEEP_S)
        self.assertLessEqual(len(self.monitor.spike_log), agent.SPIKE_LOOKBACK_S + 1)

    def test_detectors_run_every_few_seconds_of_data_time(self):
        evaluations = 0
        for t in range(61):
            self.monitor.add_reading(reading(at(t)))
            evaluations += self.monitor.evaluate_if_due() is not None
        self.assertEqual(evaluations, 13)   # at 0, 5, 10, ..., 60 s


def replay(monitor, readings):
    """Feed readings through a monitor like the agent does. Returns (alert seconds, level counts,
    alert texts); alerts are confirmed as if the POST had worked."""
    first, alerts, levels, reasons = None, [], {}, []
    for r in readings:
        monitor.add_reading(r)
        result = monitor.evaluate_if_due()
        if result is None:
            continue
        assessment, decision = result
        first = first or assessment.time
        levels[assessment.level] = levels.get(assessment.level, 0) + 1
        if decision.action == "alert":
            alerts.append(round((assessment.time - first).total_seconds()))
            reasons.append(agent.simple_reason(assessment))
            monitor.confirm_alert(assessment.time)
    return alerts, levels, reasons


class RecordedCaptureTest(unittest.TestCase):
    """The recorded 6-minute live captures (real simulator data) through the real decision core."""

    EXPECTED = {   # machine: (alert times in seconds after the first reading, level counts)
        "CNC_01": ([], {"none": 70, "watch": 3}),
        "CNC_02": ([173], {"none": 4, "watch": 22, "high": 46}),
        "PUMP_03": ([172], {"none": 13, "watch": 16, "high": 44}),
        "CONVEYOR_04": ([], {"none": 60, "watch": 12}),
    }

    def test_faulty_machines_alert_and_healthy_ones_stay_quiet(self):
        baseline = helpers.fixture_baseline()
        for machine, (expected_alerts, expected_levels) in self.EXPECTED.items():
            with self.subTest(machine):
                alerts, levels, reasons = replay(MachineMonitor(machine, baseline), helpers.fixture_capture(machine))
                self.assertEqual(alerts, expected_alerts)
                self.assertEqual(levels, expected_levels)
                for text in reasons:
                    self.assertTrue(text.startswith(f"{machine}: sustained abnormal behaviour"))
                    self.assertTrue(text.endswith("Inspection recommended."))

    def test_explanations_quote_the_measurements(self):
        baseline = helpers.fixture_baseline()
        _, _, (cnc02,) = replay(MachineMonitor("CNC_02", baseline), helpers.fixture_capture("CNC_02"))
        self.assertIn("temperature_C and current_A were abnormal at the same time", cnc02)
        self.assertIn("vs normal 68.0", cnc02)   # CNC_02's learned normal temperature
        _, _, (pump,) = replay(MachineMonitor("PUMP_03", baseline), helpers.fixture_capture("PUMP_03"))
        self.assertIn("vibration_mm_s and current_A were abnormal at the same time", pump)
        self.assertIn("more variable than normal", pump)


class HealthyStreamTest(unittest.TestCase):
    """A healthy machine simulated the way the simulator does it (server.js) must never alert."""

    @staticmethod
    def live_like_stream(seconds, seed, bursts):
        """Each second: 2% pull back towards normal plus uniform noise, as in server.js. With
        bursts=True, also the conveyor's random vibration bursts (0.5% chance per second)."""
        rng = random.Random(seed)
        noise = {"temperature_C": 0.4, "vibration_mm_s": 0.05, "rpm": 8.0, "current_A": 0.15}
        state = dict(helpers.CENTRES)
        for t in range(seconds):
            for sensor in state:
                state[sensor] += (helpers.CENTRES[sensor] - state[sensor]) * 0.02 + rng.uniform(-noise[sensor], noise[sensor])
            if bursts and rng.random() < 0.005:
                state["vibration_mm_s"] += rng.uniform(0.5, 1.5)
            yield reading(at(t), **{s: round(v, 0 if s == "rpm" else 2) for s, v in state.items()})

    def test_healthy_machine_never_alerts(self):
        for seed, bursts in ((7, False), (1, True), (2, True)):
            with self.subTest(seed=seed, bursts=bursts):
                alerts, levels, _ = replay(MachineMonitor(M, make_baseline()),
                                           self.live_like_stream(1200, seed, bursts))
                self.assertEqual(alerts, [])
                self.assertNotIn("high", levels)


if __name__ == "__main__":
    unittest.main()
