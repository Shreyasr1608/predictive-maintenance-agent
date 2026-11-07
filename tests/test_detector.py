"""detector.py: the spike, drift and pattern detectors on hand-made data with known answers."""
import unittest
from datetime import timedelta

from tests import helpers
from tests.helpers import START, TEST_MACHINE as M, make_baseline, reading, window
import detector
from detector import LIVE_NOISE_SD as NOISE, detect_drift, detect_patterns, detect_spikes

BASE = make_baseline()   # tiny history spreads, so one "spread" = the live noise of the sensor


def by_sensor(signals):
    return {s.sensor: s for s in signals}


def by_pattern(signals):
    return {s.evidence["pattern"]: s for s in signals}


class SpikeTest(unittest.TestCase):
    def spikes(self, baseline=BASE, moment=START, **values):
        return by_sensor(detect_spikes(M, reading(moment, **values), baseline))

    def test_normal_reading_triggers_nothing(self):
        signals = self.spikes()
        self.assertEqual(sorted(signals), sorted(helpers.SENSOR_FIELDS))
        self.assertFalse(any(s.triggered for s in signals.values()))
        self.assertTrue(all(s.score == 0.0 for s in signals.values()))

    def test_high_temperature_triggers_with_a_score_relative_to_the_threshold(self):
        s = self.spikes(temperature_C=70.0 + 5 * NOISE["temperature_C"])["temperature_C"]
        self.assertTrue(s.triggered)
        self.assertAlmostEqual(s.evidence["z"], 5.0)
        self.assertAlmostEqual(s.score, 5.0 / detector.SPIKE_THRESHOLD_Z["temperature_C"])
        self.assertEqual(s.evidence["spread_source"], "live noise")

    def test_just_below_and_just_above_the_threshold(self):
        limit = detector.SPIKE_THRESHOLD_Z["current_A"]
        below = self.spikes(current_A=12.0 + (limit - 0.1) * NOISE["current_A"])["current_A"]
        above = self.spikes(current_A=12.0 + (limit + 0.1) * NOISE["current_A"])["current_A"]
        self.assertFalse(below.triggered)
        self.assertTrue(above.triggered)

    def test_only_the_suspicious_direction_counts(self):
        cold = self.spikes(temperature_C=70.0 - 10 * NOISE["temperature_C"])["temperature_C"]
        self.assertFalse(cold.triggered, "a cold machine is not a fault sign")
        slow = self.spikes(rpm=1500.0 - 10 * NOISE["rpm"])["rpm"]
        fast = self.spikes(rpm=1500.0 + 10 * NOISE["rpm"])["rpm"]
        self.assertTrue(slow.triggered and fast.triggered, "rpm is suspicious in both directions")

    def test_larger_history_spread_wins_over_live_noise(self):
        wide = make_baseline(spreads={"temperature_C": 3.0})
        s = self.spikes(baseline=wide, temperature_C=70.0 + 5 * NOISE["temperature_C"])["temperature_C"]
        self.assertEqual((s.evidence["spread"], s.evidence["spread_source"]), (3.0, "history"))
        self.assertFalse(s.triggered)   # +6 C is only 2 spreads of 3 C

    def test_drifting_sensor_is_compared_with_its_nominal_value(self):
        # like PUMP_03: rpm was already sliding in the history, so its median (2800) is not normal
        drifting = make_baseline(centres={"rpm": 2950.0}, medians={"rpm": 2800.0}, drifting=("rpm",))
        self.assertFalse(self.spikes(baseline=drifting, rpm=2950.0)["rpm"].triggered)
        self.assertTrue(self.spikes(baseline=drifting, rpm=2800.0)["rpm"].triggered)

    def test_hour_of_day_median_with_fallback_to_all_hours(self):
        hourly = make_baseline(hourly={"temperature_C": {"5": {"median": 80.0}}})
        at_5 = self.spikes(baseline=hourly, moment=START, temperature_C=80.0)["temperature_C"]
        at_6 = self.spikes(baseline=hourly, moment=START + timedelta(hours=1), temperature_C=80.0)["temperature_C"]
        self.assertFalse(at_5.triggered)   # 05:00 UTC: compared with the hour-5 median of 80
        self.assertTrue(at_6.triggered)    # 06:00 UTC: no hour-6 statistics, all-hours median 70

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            detect_spikes(M, dict(reading(START), rpm="fast"), BASE)
        with self.assertRaises(KeyError):
            detect_spikes("NOPE_99", reading(START, machine="NOPE_99"), BASE)


class DriftTest(unittest.TestCase):
    def drift(self, values=None, seconds=300, pairs=None):
        return by_sensor(detect_drift(M, pairs if pairs is not None else window(seconds, values), BASE))

    def test_not_enough_data_is_not_evaluated(self):
        self.assertEqual(detect_drift(M, [], BASE), [])
        self.assertEqual(detect_drift(M, window(20), BASE), [])              # too few readings
        self.assertEqual(detect_drift(M, window(100, step=0.5), BASE), [])   # 201 readings, only 100 s

    def test_gradual_temperature_rise_is_drift(self):
        rise = 10 * NOISE["temperature_C"]   # 10 spreads in 5 minutes
        signals = self.drift(lambda t: {"temperature_C": 70.0 + rise * t / 300})
        temp = signals["temperature_C"]
        self.assertTrue(temp.triggered)
        self.assertTrue(temp.evidence["gradual"])
        self.assertAlmostEqual(temp.evidence["change_spreads"], 10.0, places=6)
        self.assertAlmostEqual(temp.evidence["slope_per_min"], rise / 5, places=6)
        self.assertFalse(any(s.triggered for name, s in signals.items() if name != "temperature_C"))

    def test_change_in_the_harmless_direction_is_ignored(self):
        temp = self.drift(lambda t: {"temperature_C": 70.0 - 12.0 * t / 300})["temperature_C"]
        self.assertFalse(temp.triggered)
        self.assertEqual(temp.score, 0.0)

    def test_falling_rpm_is_drift(self):
        drop = 10 * NOISE["rpm"]
        self.assertTrue(self.drift(lambda t: {"rpm": 1500.0 - drop * t / 300})["rpm"].triggered)

    def test_small_rise_scores_below_one(self):
        vib = self.drift(lambda t: {"vibration_mm_s": 2.0 + 3 * NOISE["vibration_mm_s"] * t / 300})["vibration_mm_s"]
        self.assertFalse(vib.triggered)
        self.assertAlmostEqual(vib.score, 3 / detector.DRIFT_MIN_CHANGE_SPREADS, places=6)

    def test_sudden_step_is_not_drift(self):
        # a 10-spread jump at t = 200 s is big but not gradual: that is the spike detector's job
        step = 10 * NOISE["temperature_C"]
        temp = self.drift(lambda t: {"temperature_C": 70.0 + (step if t >= 200 else 0.0)})["temperature_C"]
        self.assertFalse(temp.evidence["gradual"])
        self.assertFalse(temp.triggered)
        self.assertEqual(temp.score, 0.0)

    def test_only_the_newest_window_counts(self):
        # a ramp that ended five minutes ago: the newest 300 s are flat
        temp = self.drift(lambda t: {"temperature_C": 70.0 + 12.0 * min(t, 300) / 300}, seconds=600)["temperature_C"]
        self.assertFalse(temp.triggered)

    def test_slope_uses_real_seconds_even_with_an_outage(self):
        pairs = [p for p in window(300, lambda t: {"current_A": 12.0 + 0.01 * t})
                 if not 100 < (p[0] - START).total_seconds() < 160]   # 59 s without data
        current = self.drift(pairs=pairs)["current_A"]
        self.assertAlmostEqual(current.evidence["slope_per_min"], 0.6, places=6)

    def test_window_out_of_time_order_is_an_error(self):
        pairs = window(300)
        pairs[10], pairs[11] = pairs[11], pairs[10]
        with self.assertRaises(ValueError):
            detect_drift(M, pairs, BASE)


class PatternTest(unittest.TestCase):
    def patterns(self, values=None, seconds=120):
        return by_pattern(detect_patterns(M, window(seconds, values), BASE))

    def test_not_enough_data_is_not_evaluated(self):
        self.assertEqual(detect_patterns(M, window(20), BASE), [])
        self.assertEqual(detect_patterns(M, window(60), BASE), [])   # 61 readings, but only 60 s of 120

    def test_healthy_window_triggers_nothing(self):
        signals = self.patterns(lambda t: {"vibration_mm_s": 2.0 + (0.05 if int(t) % 2 else -0.05)})
        self.assertEqual(sorted(signals), ["co_elevation", "rpm_decline_with_current", "vibration_variability"])
        self.assertFalse(any(s.triggered for s in signals.values()))

    def test_jumpy_vibration_is_variability_but_not_co_elevation(self):
        # alternating 1 and 3 mm/s: std about 1.0, i.e. 6.7 live-noise spreads (threshold 5).
        # Vibration alone is ONE sensor, so it is not co-elevation: the healthy conveyor case.
        signals = self.patterns(lambda t: {"vibration_mm_s": 3.0 if int(t) % 2 else 1.0})
        self.assertTrue(signals["vibration_variability"].triggered)
        self.assertFalse(signals["co_elevation"].triggered)

    def test_temperature_and_current_high_together_is_co_elevation(self):
        hot = {"temperature_C": 70.0 + 5 * NOISE["temperature_C"], "current_A": 12.0 + 3 * NOISE["current_A"]}
        co = self.patterns(lambda t: hot if int(t) % 2 else {})["co_elevation"]   # every other reading
        self.assertTrue(co.triggered)
        self.assertEqual(co.evidence["dominant_pair"], "temperature_C+current_A")
        self.assertAlmostEqual(co.evidence["share"], 60 / 121)

    def test_one_sensor_high_alone_is_not_co_elevation(self):
        co = self.patterns(lambda t: {"vibration_mm_s": 2.0 + 10 * NOISE["vibration_mm_s"]})["co_elevation"]
        self.assertFalse(co.triggered)
        self.assertIsNone(co.evidence["dominant_pair"])

    def test_rpm_decline_with_a_current_change(self):
        slow_and_light = {"rpm": 1500.0 - 4 * NOISE["rpm"], "current_A": 12.0 - 3 * NOISE["current_A"]}
        p = self.patterns(lambda t: slow_and_light)["rpm_decline_with_current"]
        self.assertTrue(p.triggered)
        self.assertAlmostEqual(p.score, min(4 / 3, 3 / 2))   # both parts must be strong

    def test_rpm_decline_without_a_current_change_is_not_this_pattern(self):
        p = self.patterns(lambda t: {"rpm": 1500.0 - 4 * NOISE["rpm"]})["rpm_decline_with_current"]
        self.assertFalse(p.triggered)


if __name__ == "__main__":
    unittest.main()
