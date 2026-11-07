"""baseline.py on synthetic 7-day histories shaped like the simulator's (one reading per minute)."""
import random
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests import helpers
import baseline
from common import SENSOR_FIELDS

NOMINAL = {"temperature_C": 72.0, "vibration_mm_s": 1.8, "rpm": 1480.0, "current_A": 12.5}
# Uniform noise of +-3% / 4% / 1.5% / 3% of the nominal value, as in generate-history.js.
NOISE = {"temperature_C": 0.03, "vibration_mm_s": 0.04, "rpm": 0.015, "current_A": 0.03}


def make_history(change=None, noise=NOISE, nominal=NOMINAL, seed=1):
    """
    7 days + 1 reading at one per minute. change(day, sensor) returns a value to add, where
    `day` counts days from the first reading (0.0 to 7.0), to put faults or drift in.
    Returns (datetime, reading) pairs, oldest first: the format baseline.py takes.
    """
    rng = random.Random(seed)
    start = helpers.START - timedelta(days=7)
    history = []
    for k in range(7 * 1440 + 1):
        moment = start + timedelta(minutes=k)
        reading = {"machine_id": "TEST_01", "timestamp": helpers.iso(moment), "status": "running"}
        for sensor in SENSOR_FIELDS:
            value = nominal[sensor] * (1 + rng.uniform(-noise[sensor], noise[sensor]))
            reading[sensor] = value + (change(k / 1440, sensor) if change else 0.0)
        history.append((moment, reading))
    return history


def build(history, nominal=NOMINAL):
    """Build the baseline of one machine and return its per-sensor part."""
    built = baseline.build_baseline({"TEST_01": history}, {"TEST_01": nominal})
    return built["machines"]["TEST_01"]["sensors"]


class BuildBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.healthy = build(make_history())

    def test_healthy_history(self):
        for sensor in SENSOR_FIELDS:
            info = self.healthy[sensor]
            with self.subTest(sensor):
                self.assertFalse(info["drifting_in_window"])
                self.assertAlmostEqual(info["reference_center"], NOMINAL[sensor], delta=0.01 * NOMINAL[sensor])
                self.assertGreater(info["reference_spread"], 0)
                self.assertEqual(len(info["hourly"]), 24)
                self.assertIsNone(info["first_departure_day"])
                self.assertEqual(len(info["daily_means"]), 7)

    def test_fault_after_the_reference_window_does_not_change_normal(self):
        # +20 C during the last two days, like CNC_02's late heat episodes
        sensors = build(make_history(change=lambda day, s: 20.0 if s == "temperature_C" and day >= 5 else 0.0))
        temp = sensors["temperature_C"]
        self.assertAlmostEqual(temp["reference_center"], NOMINAL["temperature_C"], delta=0.5)
        self.assertEqual(temp["first_departure_day"], 6)

    def test_sensor_already_drifting_in_the_window_uses_the_nominal_value(self):
        # rpm sliding 30 per day from the start, like PUMP_03: the history has no healthy level
        sensors = build(make_history(change=lambda day, s: -30.0 * day if s == "rpm" else 0.0))
        rpm = sensors["rpm"]
        self.assertTrue(rpm["drifting_in_window"])
        self.assertGreater(rpm["drift_ratio"], baseline.DRIFT_RATIO_LIMIT)
        self.assertEqual(rpm["reference_center"], NOMINAL["rpm"])

    def test_flat_sensor_gets_a_positive_floor_spread(self):
        sensors = build(make_history(noise=dict(NOISE, current_A=0.0)))
        expected = baseline.sig(baseline.MIN_STD_FRACTION * NOMINAL["current_A"])
        self.assertEqual(sensors["current_A"]["reference_spread"], expected)

    def test_nominal_zero_keeps_the_tiny_absolute_floor(self):
        # regression: rounding to 4 decimals once turned the 1e-6 floor into 0.0
        nominal = dict(NOMINAL, current_A=0.0)
        sensors = build(make_history(noise=dict(NOISE, current_A=0.0), nominal=nominal), nominal=nominal)
        self.assertEqual(sensors["current_A"]["reference_spread"], baseline.ABSOLUTE_MIN_STD)

    def test_too_little_history_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            build(make_history()[:20])


class BaselineHelpersTest(unittest.TestCase):
    def test_sig_keeps_significant_figures_not_decimal_places(self):
        self.assertEqual(baseline.sig(0.000001234567), 1.23457e-06)
        self.assertEqual(baseline.sig(1480.123456), 1480.12)

    def test_robust_std_ignores_a_few_outliers(self):
        values = [10.0 + 0.1 * (i % 11 - 5) for i in range(1000)]
        clean = baseline.robust_std(values)
        dirty = baseline.robust_std(values[:-10] + [1000.0] * 10)
        self.assertAlmostEqual(clean, dirty, delta=0.05 * clean)

    def test_get_reference_falls_back_to_all_hours(self):
        data = {"machines": {"M": {"sensors": {"rpm": {"hourly": {"5": {"median": 1}}, "overall": {"median": 2}}}}}}
        self.assertEqual(baseline.get_reference(data, "M", "rpm", 5)["median"], 1)
        self.assertEqual(baseline.get_reference(data, "M", "rpm", 6)["median"], 2)

    def test_save_and_load_round_trip(self):
        data = {"meta": {"reference_days": 4}, "machines": {"M": {"sensors": {}}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "baseline.json"
            baseline.save_baseline(data, path)
            self.assertEqual(baseline.load_baseline(path), data)


if __name__ == "__main__":
    unittest.main()
