"""common.py: timestamp parsing, reading validation and the HTTP helper."""
import unittest
from datetime import timedelta
from unittest import mock

from tests import helpers
import common
from common import SimulatorError, check_reading, clean_readings, parse_timestamp


class ParseTimestampTest(unittest.TestCase):
    def test_simulator_format_becomes_utc(self):
        moment = parse_timestamp("2026-11-05T05:22:37.583Z")
        self.assertEqual(moment.utcoffset(), timedelta(0))
        self.assertEqual((moment.hour, moment.minute, moment.second, moment.microsecond), (5, 22, 37, 583000))

    def test_other_offsets_are_converted_to_utc(self):
        # regression: the hour-of-day baselines are in UTC, so 10:52 at +05:30 must be 05:22
        moment = parse_timestamp("2026-11-05T10:52:37+05:30")
        self.assertEqual((moment.hour, moment.minute), (5, 22))
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_timestamp_without_time_zone_is_rejected(self):
        # regression: such a time used to pass, then crash the agent when compared
        with self.assertRaises(ValueError):
            parse_timestamp("2026-11-05T05:22:37.583")

    def test_garbage_and_non_text_are_rejected(self):
        for bad in ("yesterday", "", 1730784157, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_timestamp(bad)


class CheckReadingTest(unittest.TestCase):
    def setUp(self):
        self.good = helpers.reading(helpers.START)

    def test_valid_reading_has_no_issues(self):
        self.assertEqual(check_reading(self.good), [])

    def test_every_kind_of_problem_is_reported(self):
        cases = {
            "not an object": [1, 2, 3],
            "missing field": {k: v for k, v in self.good.items() if k != "rpm"},
            "null field": dict(self.good, status=None),
            "text instead of a number": dict(self.good, temperature_C="hot"),
            "bool instead of a number": dict(self.good, current_A=True),
            "NaN": dict(self.good, rpm=float("nan")),
            "infinity": dict(self.good, vibration_mm_s=float("inf")),
            "unparseable timestamp": dict(self.good, timestamp="not a time"),
            "timestamp without time zone": dict(self.good, timestamp="2026-11-05T05:00:00"),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                self.assertTrue(check_reading(bad), f"{name} was not reported")

    def test_clean_readings_sorts_by_time_and_counts_bad_ones(self):
        later = helpers.reading(helpers.at(5))
        clean, n_bad = clean_readings([later, {"oops": 1}, self.good])
        self.assertEqual(n_bad, 1)
        self.assertEqual([r for _, r in clean], [self.good, later])


class GetJsonTest(unittest.TestCase):
    """The HTTP helper, with requests replaced by fakes."""

    def test_connection_error_says_the_simulator_may_not_be_running(self):
        refused = common.requests.exceptions.ConnectionError("refused")
        with mock.patch.object(common.requests, "get", side_effect=refused):
            with self.assertRaisesRegex(SimulatorError, "is the simulator running"):
                common.get_json("http://localhost:3000", "/machines")

    def test_error_status_is_an_error(self):
        answer = mock.Mock(status_code=404, text='{"error": "Unknown machine: NOPE"}')
        with mock.patch.object(common.requests, "get", return_value=answer):
            with self.assertRaisesRegex(SimulatorError, "HTTP 404"):
                common.get_json("http://sim.test", "/history/NOPE")

    def test_body_that_is_not_json_is_an_error(self):
        answer = mock.Mock(status_code=200, text="<html>")
        answer.json.side_effect = ValueError("not JSON")
        with mock.patch.object(common.requests, "get", return_value=answer):
            with self.assertRaisesRegex(SimulatorError, "not valid JSON"):
                common.get_json("http://sim.test", "/machines")

    def test_history_without_a_readings_list_is_an_error(self):
        with mock.patch.object(common, "get_json", return_value={"machine_id": "CNC_01"}):
            with self.assertRaisesRegex(SimulatorError, "no 'readings' list"):
                common.load_history("http://sim.test", "CNC_01")


if __name__ == "__main__":
    unittest.main()
