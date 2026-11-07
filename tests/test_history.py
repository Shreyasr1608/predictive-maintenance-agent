"""
The history check: baseline.py's daily medians and detector.detect_history_shift, first on
hand-made daily statistics with known answers, then on 7-day histories made the way the
simulator makes them (tests/sim_history.py) for all four machines.
"""
import unittest
from datetime import timedelta

from tests import helpers, sim_history
from tests.helpers import TEST_MACHINE as M, make_history_baseline
from tests.test_baseline import NOMINAL, build, make_history
import baseline
from common import SENSOR_FIELDS
from detector import HISTORY_SHIFT_SPREADS as LIMIT, LIVE_NOISE_SD, detect_history_shift

DAY = timedelta(days=1)
SEEDS = (1, 2, 3)


def by_sensor(signals):
    return {s.sensor: s for s in signals}


def triggered(signals):
    return sorted(s.sensor for s in signals if s.triggered)


class DailyMediansTest(unittest.TestCase):
    """What baseline.py stores for the history check."""

    def test_every_full_day_gets_a_number_and_a_median(self):
        sensors = build(make_history())
        for sensor in SENSOR_FIELDS:
            with self.subTest(sensor):
                info = sensors[sensor]
                self.assertEqual(info["daily_days"], [1, 2, 3, 4, 5, 6, 7])   # the lone last reading is dropped
                self.assertEqual(len(info["daily_medians"]), 7)
                for median in info["daily_medians"]:
                    self.assertAlmostEqual(median, NOMINAL[sensor], delta=0.01 * NOMINAL[sensor])

    def test_a_daily_median_ignores_bursts_that_move_the_daily_mean(self):
        # +4 mm/s in one minute of every twelve (8%), like PUMP_03's cavitation bursts
        bursts = make_history(change=lambda day, s: 4.0 if s == "vibration_mm_s" and round(day * 1440) % 12 == 0 else 0.0)
        vib = build(bursts)["vibration_mm_s"]
        for mean, median in zip(vib["daily_means"], vib["daily_medians"]):
            self.assertGreater(mean - median, 0.25)                        # the mean is pulled up ...
            self.assertAlmostEqual(median, NOMINAL["vibration_mm_s"], delta=0.02)   # ... the median is not


class DetectHistoryShiftTest(unittest.TestCase):
    """The rule on hand-made daily medians (history spread 1.0, so values are in spreads)."""

    def signals(self, medians, **options):
        return by_sensor(detect_history_shift(M, make_history_baseline(medians, **options)))

    def test_a_shift_on_the_newest_day_triggers(self):
        s = self.signals({"temperature_C": [70, 70, 70, 70, 71, 72, 73.5]})
        temp = s["temperature_C"]
        self.assertTrue(temp.triggered)
        self.assertEqual(temp.detector, "history")
        self.assertAlmostEqual(temp.score, 3.5 / LIMIT)
        d = temp.evidence
        self.assertEqual((d["day"], d["median"], d["normal"], d["normal_source"]), (7, 73.5, 70.0, "reference median"))
        self.assertAlmostEqual(d["shift_spreads"], 3.5)
        self.assertAlmostEqual(d["change_pct"], 5.0)
        self.assertEqual(d["since_day"], 7)                    # day 6 was only 2 spreads off
        self.assertEqual((d["series_days"], d["series"]), ([6, 7], [72, 73.5]))   # from the last normal day
        self.assertAlmostEqual(d["slope_per_day"], 1.5)
        self.assertEqual(triggered(s.values()), ["temperature_C"])

    def test_the_threshold_is_inclusive(self):
        below = self.signals({"current_A": [12] * 6 + [12 + LIMIT - 0.01]})["current_A"]
        at = self.signals({"current_A": [12] * 6 + [12 + LIMIT]})["current_A"]
        self.assertFalse(below.triggered)
        self.assertTrue(at.triggered)

    def test_only_the_harmful_direction_counts(self):
        cases = {   # sensor: (newest median, should trigger)
            "temperature_C": [(60.0, False), (80.0, True)],    # a cooler machine is not a fault sign
            "vibration_mm_s": [(-3.0, False), (7.0, True)],
            "current_A": [(5.0, False), (17.0, True)],
            "rpm": [(1510.0, False), (1490.0, True)],          # falling rpm only, as for drift
        }
        for sensor, checks in cases.items():
            for newest, expected in checks:
                with self.subTest(sensor=sensor, newest=newest):
                    centre = helpers.CENTRES[sensor]
                    signal = self.signals({sensor: [centre] * 6 + [newest]})[sensor]
                    self.assertEqual(signal.triggered, expected)
                    if not expected:
                        self.assertEqual(signal.score, 0.0)

    def test_a_shift_that_has_gone_away_does_not_trigger(self):
        temp = self.signals({"temperature_C": [70, 70, 70, 70, 80, 80, 70]})["temperature_C"]
        self.assertFalse(temp.triggered)
        self.assertIsNone(temp.evidence["since_day"])

    def test_a_sensor_drifting_in_the_reference_window_is_compared_with_its_nominal_value(self):
        # like PUMP_03: rpm falling about 26 per day from day 1; normal is the nominal 2950
        falling = [2925, 2899, 2873, 2847, 2821, 2795, 2770]
        rpm = self.signals({"rpm": falling}, centres={"rpm": 2950.0}, spreads={"rpm": 30.0}, drifting=("rpm",))["rpm"]
        d = rpm.evidence
        self.assertTrue(rpm.triggered)
        self.assertEqual((d["normal"], d["normal_source"]), (2950.0, "nominal"))
        self.assertAlmostEqual(d["shift_spreads"], 6.0)
        self.assertEqual(d["since_day"], 4)                    # 2847 is the first day 3+ spreads low
        self.assertEqual(d["series_days"], [1, 2, 3, 4, 5, 6, 7])   # the whole decline is quoted
        self.assertAlmostEqual(d["slope_per_day"], -725 / 28)   # least-squares slope of those 7 medians

    def test_history_is_measured_in_history_spreads_not_live_noise(self):
        # vibration +0.2 mm/s with a history spread of 0.05: 4 history spreads, but only about
        # 1.3 live-noise spreads. History medians are compared with history noise.
        vib = self.signals({"vibration_mm_s": [2.0] * 6 + [2.2]}, spreads={"vibration_mm_s": 0.05})["vibration_mm_s"]
        self.assertLess(0.2 / LIVE_NOISE_SD["vibration_mm_s"], LIMIT)
        self.assertTrue(vib.triggered)
        self.assertAlmostEqual(vib.evidence["shift_spreads"], 4.0)

    def test_not_evaluated_without_daily_medians(self):
        # baseline files saved before this check existed (like tests/fixtures/baseline.json)
        self.assertEqual(detect_history_shift(M, helpers.make_baseline()), [])
        recorded = helpers.fixture_baseline()
        for machine in helpers.MACHINES:
            self.assertEqual(detect_history_shift(machine, recorded), [])

    def test_not_evaluated_while_every_day_is_in_the_reference_window(self):
        self.assertEqual(detect_history_shift(M, make_history_baseline({"rpm": [1500.0] * 4}, reference_days=4)), [])

    def test_damaged_daily_statistics_are_a_clear_error(self):
        damage = {
            "lengths differ": lambda info: info.update(daily_days=[1, 2, 3]),
            "not a number": lambda info: info["daily_medians"].__setitem__(2, "hot"),
            "days out of order": lambda info: info.update(daily_days=[1, 2, 3, 5, 4, 6, 7]),
            "fractional day": lambda info: info["daily_days"].__setitem__(6, 7.5),
        }
        for name, change in damage.items():
            with self.subTest(name):
                base = make_history_baseline()
                change(base["machines"][M]["sensors"]["temperature_C"])
                with self.assertRaises(ValueError):
                    detect_history_shift(M, base)

    def test_unknown_machine(self):
        with self.assertRaises(KeyError):
            detect_history_shift("NOPE_99", make_history_baseline())


class SimulatedMachinesTest(unittest.TestCase):
    """All four machines, histories made with the simulator's formulas (several seeds)."""

    @classmethod
    def setUpClass(cls):
        cls.results = {seed: {machine: by_sensor(detect_history_shift(machine, sim_history.simulated_baseline(seed)))
                              for machine in sim_history.BASELINES} for seed in SEEDS}

    def test_cnc01_bearing_wear_shows_on_vibration_temperature_and_current(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                signals = self.results[seed]["CNC_01"]
                self.assertEqual(triggered(signals.values()), ["current_A", "temperature_C", "vibration_mm_s"])
                vib = signals["vibration_mm_s"].evidence
                self.assertAlmostEqual(vib["median"], 5.30, delta=0.05)    # normal 1.80
                self.assertGreater(vib["shift_spreads"], 50)
                self.assertEqual(vib["since_day"], 5)                      # wear starts on day 5
                self.assertEqual(vib["series_days"], [4, 5, 6, 7])
                self.assertGreater(signals["temperature_C"].evidence["shift_spreads"], 2 * LIMIT)
                self.assertGreater(signals["current_A"].evidence["shift_spreads"], 2 * LIMIT)

    def test_pump03_clog_shows_on_rpm_only(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                signals = self.results[seed]["PUMP_03"]
                self.assertEqual(triggered(signals.values()), ["rpm"])     # cavitation bursts move no median
                rpm = signals["rpm"].evidence
                self.assertEqual((rpm["normal"], rpm["normal_source"]), (2950, "nominal"))
                self.assertAlmostEqual(rpm["median"], 2770, delta=10)
                self.assertGreater(rpm["shift_spreads"], 5.0)
                self.assertAlmostEqual(rpm["slope_per_day"], -180 / 7, delta=3)
                self.assertEqual(rpm["series_days"], [1, 2, 3, 4, 5, 6, 7])

    def test_cnc02_and_conveyor04_stay_quiet_with_a_wide_margin(self):
        # CNC_02's afternoon spikes cover a quarter of a day, so they do not move a daily median;
        # CNC_02 is caught by the live detectors instead (test_agent_decisions, test_end_to_end).
        for seed in SEEDS:
            for machine in ("CNC_02", "CONVEYOR_04"):
                with self.subTest(seed=seed, machine=machine):
                    signals = self.results[seed][machine]
                    self.assertEqual(triggered(signals.values()), [])
                    largest = max(abs(s.evidence["shift_spreads"]) for s in signals.values())
                    self.assertLess(largest, 1.0)   # threshold 3.0

    def test_the_simulators_status_labels_are_not_used(self):
        # Detection reads sensor values only. Relabelling every reading changes nothing.
        history = sim_history.histories(1, machines=("CONVEYOR_04",))
        for _, reading in history["CONVEYOR_04"]:
            reading["status"] = "fault"
        built = baseline.build_baseline(history, {"CONVEYOR_04": sim_history.nominals()["CONVEYOR_04"]})
        self.assertEqual(triggered(detect_history_shift("CONVEYOR_04", built)), [])


class EarlyWarningReplayTest(unittest.TestCase):
    """The same rule on a history cut off after day N: what would the agent have said back then?"""

    @staticmethod
    def history_until(machine, last_day, seed=1):
        history = sim_history.histories(seed, machines=(machine,))[machine]
        start = history[0][0]
        kept = [(t, r) for t, r in history if t < start + last_day * DAY]
        built = baseline.build_baseline({machine: kept}, {machine: sim_history.nominals()[machine]})
        return history, start, by_sensor(detect_history_shift(machine, built))

    def test_cnc01_is_flagged_after_day_5_while_the_simulator_still_says_running(self):
        history, start, signals = self.history_until("CNC_01", 5)
        self.assertIn("vibration_mm_s", triggered(signals.values()))
        self.assertEqual(signals["vibration_mm_s"].evidence["day"], 5)
        self.assertGreater(signals["vibration_mm_s"].evidence["shift_spreads"], 5 * LIMIT)

        def statuses(day):   # the simulator's own labels, used here only to show the timing
            return {r["status"] for t, r in history if start + (day - 1) * DAY <= t < start + day * DAY}
        self.assertEqual(statuses(5), {"running"})
        self.assertIn("warning", statuses(6))
        self.assertIn("fault", statuses(7))

    def test_nothing_can_be_judged_before_a_day_after_the_reference_window(self):
        _, _, signals = self.history_until("CNC_01", 4)
        self.assertEqual(signals, {})

    def test_pump03_rpm_decline_is_flagged_after_day_5(self):
        _, _, signals = self.history_until("PUMP_03", 5)
        self.assertEqual(triggered(signals.values()), ["rpm"])
        self.assertGreater(signals["rpm"].evidence["shift_spreads"], LIMIT)


class ConveyorStressTest(unittest.TestCase):
    """CONVEYOR_04 with an extra vibration excursion (+1.5 mm/s, like its day-3 event) on its
    newest day: only an excursion covering most of the day may count as a shift."""

    @staticmethod
    def vibration_signal(minutes):
        history = sim_history.histories(2, machines=("CONVEYOR_04",))["CONVEYOR_04"]
        newest_day = history[0][0] + 6 * DAY
        changed = []
        for t, r in history:
            if newest_day <= t < newest_day + timedelta(minutes=minutes):
                r = dict(r, vibration_mm_s=r["vibration_mm_s"] + 1.5)
            changed.append((t, r))
        built = baseline.build_baseline({"CONVEYOR_04": changed}, {"CONVEYOR_04": sim_history.nominals()["CONVEYOR_04"]})
        return by_sensor(detect_history_shift("CONVEYOR_04", built))["vibration_mm_s"]

    def test_excursions_shorter_than_half_a_day_stay_quiet(self):
        for minutes in (72, 10 * 60):
            with self.subTest(minutes=minutes):
                self.assertFalse(self.vibration_signal(minutes).triggered)

    def test_an_excursion_covering_most_of_the_newest_day_is_a_shift(self):
        self.assertTrue(self.vibration_signal(14 * 60).triggered)


if __name__ == "__main__":
    unittest.main()
