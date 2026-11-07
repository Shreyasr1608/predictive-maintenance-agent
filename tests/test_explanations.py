"""
agent.py's alert texts: which likely cause is named for which evidence (live and history), and
what the history alert says for the simulated CNC_01 and PUMP_03. The live texts of the recorded
CNC_02 and PUMP_03 alerts are checked in test_agent_decisions.py.
"""
import unittest

from tests import sim_history
import agent
from agent import Evidence, LIKELY_CAUSES, MachineMonitor, history_causes, history_reason, live_causes


def live(detector, name, sensors, pair=None):
    return Evidence(detector, name, tuple(sensors), 1.5, {"dominant_pair": pair} if pair else {})


def shifted(*sensors):
    return [Evidence("history", s, (s,), 2.0, {}) for s in sensors]


class LiveCausesTest(unittest.TestCase):
    CASES = {   # name: (evidence, expected cause keys)
        "CNC_02-like: temperature with current": (
            [live("spike", "temperature_C", ["temperature_C"]),
             live("pattern", "co_elevation", ["temperature_C", "current_A"], "temperature_C+current_A")],
            ["thermal_runaway"]),
        "PUMP_03-like: bursty vibration with current": (
            [live("spike", "vibration_mm_s", ["vibration_mm_s"]),
             live("pattern", "vibration_variability", ["vibration_mm_s"]),
             live("pattern", "co_elevation", ["vibration_mm_s", "current_A"], "vibration_mm_s+current_A")],
            ["cavitation"]),
        "vibration and temperature together": (
            [live("spike", "temperature_C", ["temperature_C"]),
             live("pattern", "co_elevation", ["temperature_C", "vibration_mm_s"], "temperature_C+vibration_mm_s")],
            ["friction"]),
        "rpm low with a current change": (
            [live("spike", "rpm", ["rpm"]), live("pattern", "rpm_decline_with_current", ["rpm", "current_A"])],
            ["clog"]),
        "vibration rising plus a current spike": (
            [live("drift", "vibration_mm_s", ["vibration_mm_s"]), live("spike", "current_A", ["current_A"])],
            ["wear_or_imbalance", "overload"]),
        "temperature rising plus a vibration spike": (
            [live("drift", "temperature_C", ["temperature_C"]), live("spike", "vibration_mm_s", ["vibration_mm_s"])],
            ["cooling", "shocks"]),
        "rpm spike plus a vibration spike": (
            [live("spike", "rpm", ["rpm"]), live("spike", "vibration_mm_s", ["vibration_mm_s"])],
            ["speed", "shocks"]),
        "nothing recognisable": ([], ["unknown"]),
    }

    def test_the_cause_follows_the_evidence(self):
        for name, (evidence, expected) in self.CASES.items():
            with self.subTest(name):
                self.assertEqual(live_causes(evidence), expected)

    def test_at_most_two_causes(self):
        everything = [item for evidence, _ in self.CASES.values() for item in evidence]
        self.assertEqual(len(live_causes(everything)), agent.MAX_CAUSES)


class HistoryCausesTest(unittest.TestCase):
    def test_the_cause_follows_the_shifted_sensors(self):
        cases = {
            ("vibration_mm_s", "temperature_C", "current_A"): ["bearing_wear"],   # CNC_01
            ("vibration_mm_s", "current_A"): ["bearing_wear"],
            ("vibration_mm_s",): ["wear_or_imbalance"],
            ("rpm",): ["clog"],                                                  # PUMP_03
            ("rpm", "current_A"): ["clog"],
            ("vibration_mm_s", "rpm"): ["wear_or_imbalance", "clog"],
            ("temperature_C", "current_A"): ["thermal_runaway"],
            ("temperature_C",): ["cooling"],
            ("current_A",): ["overload"],
            (): ["unknown"],
        }
        for sensors, expected in cases.items():
            with self.subTest(sensors=sensors):
                self.assertEqual(history_causes(shifted(*sensors)), expected)


class WordingTest(unittest.TestCase):
    def test_no_text_can_break_the_dashboard(self):
        # The simulator's dashboard inserts each reason into its page as HTML.
        for key, (cause, check) in LIKELY_CAUSES.items():
            with self.subTest(key):
                self.assertNotIn("<", cause + check)


class HistoryReasonTest(unittest.TestCase):
    """The history alert texts for the simulated machines (seed 1)."""

    @classmethod
    def setUpClass(cls):
        built = sim_history.simulated_baseline(1)
        cls.texts = {}
        for machine in sim_history.BASELINES:
            monitor = MachineMonitor(machine, built)
            if monitor.history_evidence:
                cls.texts[machine] = history_reason(machine, monitor.history_evidence)

    def test_only_the_degrading_machines_get_a_text(self):
        self.assertEqual(sorted(self.texts), ["CNC_01", "PUMP_03"])

    def test_cnc01_names_bearing_wear_and_shows_the_daily_rise(self):
        text = self.texts["CNC_01"]
        self.assertTrue(text.startswith("CNC_01: warning from the 7-day history. Likely cause: bearing wear. "))
        self.assertIn("Evidence (daily medians): vibration 5.30 mm/s vs normal 1.80 (+194%), "
                      "day 4 to 7: 1.80 -> 2.97 -> 4.13 -> 5.30 (about +1.17 mm/s per day), above normal since day 5; ", text)
        self.assertIn("motor current 15.0 A vs normal 12.5 (+20%); ", text)
        self.assertIn("temperature 84.0 C vs normal 72.0 (+17%). ", text)
        self.assertTrue(text.endswith("Suggested check: bearing condition and lubrication."))

    def test_pump03_names_a_developing_clog_and_the_rate_of_decline(self):
        text = self.texts["PUMP_03"]
        self.assertIn("Likely cause: a growing restriction or load slowing the machine, such as a developing clog.", text)
        self.assertIn("speed 2771 rpm vs nominal 2950 (-6%), day 1 to 7: 2924 -> ", text)
        self.assertIn("(about -26 rpm per day), below normal since day 4.", text)
        self.assertNotIn("vibration", text)   # its cavitation bursts are the live alert's business

    def test_texts_are_plain(self):
        for machine, text in self.texts.items():
            with self.subTest(machine):
                self.assertNotIn("<", text)
                self.assertNotIn("_", text.split(": ", 1)[1])   # sensor names, not field names
                self.assertLess(len(text), 500)


if __name__ == "__main__":
    unittest.main()
