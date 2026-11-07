"""
agent.py's history alert: computed once by MachineMonitor, sent at most once per run by
send_history_alert() / run_machine(), retried after a failed POST, logged only in a dry run, and
independent of the live cooldown. post_alert and the stream are faked: nothing is sent anywhere.
"""
import json
import unittest
from datetime import timedelta
from unittest import mock

from tests.helpers import START, TEST_MACHINE as M, at, make_baseline, make_history_baseline, reading
from tests.test_agent_io import RecordingEvent, scripted_stream
import agent
from agent import MachineMonitor

URL = "http://sim.test"
WEAR = {"vibration_mm_s": [2.0, 2.0, 2.0, 2.0, 4.0, 6.0, 8.0]}   # +6 spreads on day 7 (spread 1.0)


def worn_monitor():
    return MachineMonitor(M, make_history_baseline(WEAR))


class MonitorHistoryTest(unittest.TestCase):
    def test_a_shift_becomes_history_evidence(self):
        monitor = worn_monitor()
        self.assertEqual(monitor.history_note, "evaluated")
        self.assertEqual([(e.detector, e.name) for e in monitor.history_evidence], [("history", "vibration_mm_s")])
        self.assertAlmostEqual(monitor.history_evidence[0].detail["shift_spreads"], 6.0)
        self.assertTrue(monitor.history_alert_due())

    def test_a_quiet_history_means_no_history_alert(self):
        monitor = MachineMonitor(M, make_history_baseline())
        self.assertEqual((monitor.history_note, monitor.history_evidence), ("evaluated", ()))
        self.assertEqual(len(monitor.history_signals), 4)   # evaluated, all clear
        self.assertFalse(monitor.history_alert_due())

    def test_an_old_baseline_file_is_not_evaluated(self):
        monitor = MachineMonitor(M, make_baseline())
        self.assertTrue(monitor.history_note.startswith("not evaluated: the baseline has no daily medians"))
        self.assertFalse(monitor.history_alert_due())

    def test_damaged_daily_statistics_never_stop_the_live_monitoring(self):
        damaged = make_history_baseline(WEAR)
        damaged["machines"][M]["sensors"]["rpm"]["daily_medians"] = [1500.0, "fast"]
        monitor = MachineMonitor(M, damaged)
        self.assertIn("damaged daily statistics", monitor.history_note)
        self.assertFalse(monitor.history_alert_due())
        self.assertTrue(monitor.add_reading(reading(at(0))))   # live monitoring works as before

    def test_a_history_alert_leaves_the_live_cooldown_alone(self):
        monitor = worn_monitor()
        monitor.add_reading(reading(at(0)))   # as if it went out on a retry, while streaming
        monitor.confirm_history_alert()
        self.assertFalse(monitor.history_alert_due())
        self.assertIsNone(monitor.last_alert_time)
        self.assertEqual((monitor.stats["history_alerts"], monitor.stats["alerts"]), (1, 0))
        actions = [monitor._decide("high", at(t)).action for t in range(0, 65, 5)]
        self.assertEqual(actions[-1], "alert")   # a live alert is not held back by it


class SendHistoryAlertTest(unittest.TestCase):
    def send(self, monitor, post_answer, dry_run=False):
        with mock.patch.object(agent, "post_alert", return_value=post_answer) as fake_post, \
                self.assertLogs("agent", level="INFO") as logs:
            agent.send_history_alert(monitor, URL, dry_run, agent.history_reason)
        return fake_post, "\n".join(logs.output)

    def test_success_posts_once_with_the_explanation_and_no_reading(self):
        monitor = worn_monitor()
        fake_post, text = self.send(monitor, {"id": "ALERT-9"})
        base_url, machine, reason, attached = fake_post.call_args.args
        self.assertEqual((base_url, machine, attached), (URL, M, None))
        self.assertTrue(reason.startswith(f"{M}: warning from the 7-day history. Likely cause: bearing wear or imbalance."))
        self.assertIn("ALERT SENT (history) id=ALERT-9", text)
        self.assertFalse(monitor.history_alert_due())
        with mock.patch.object(agent, "post_alert") as again:
            agent.send_history_alert(monitor, URL, False, agent.history_reason)
        again.assert_not_called()   # once per run

    def test_a_failed_post_is_retried_at_the_next_call(self):
        monitor = worn_monitor()
        _, text = self.send(monitor, None)
        self.assertIn("history alert NOT sent; will retry", text)
        self.assertTrue(monitor.history_alert_due())
        self.assertEqual(monitor.stats["history_alerts"], 0)
        fake_post, _ = self.send(monitor, {"id": "ALERT-10"})
        fake_post.assert_called_once()
        self.assertEqual(monitor.stats["history_alerts"], 1)

    def test_dry_run_only_logs(self):
        monitor = worn_monitor()
        fake_post, text = self.send(monitor, {"id": "X"}, dry_run=True)
        fake_post.assert_not_called()
        self.assertIn(f"machine={M} DRY RUN, would alert (history): {M}: warning from the 7-day history", text)
        self.assertFalse(monitor.history_alert_due())

    def test_nothing_happens_when_no_history_alert_is_due(self):
        for monitor in (MachineMonitor(M, make_baseline()), MachineMonitor(M, make_history_baseline())):
            with self.subTest(note=monitor.history_note[:20]), \
                    mock.patch.object(agent, "post_alert") as fake_post, self.assertNoLogs("agent", level="INFO"):
                agent.send_history_alert(monitor, URL, False, agent.history_reason)
            fake_post.assert_not_called()


class RunMachineHistoryTest(unittest.TestCase):
    def test_sent_before_streaming_and_retried_after_a_failed_post(self):
        monitor = worn_monitor()
        readings_at_post = []

        def fake_post(base_url, machine_id, reason, attached):
            readings_at_post.append(monitor.stats["readings"])
            return None if len(readings_at_post) == 1 else {"id": "ALERT-11"}   # the first POST fails

        stream = [json.dumps(reading(START + timedelta(seconds=i))) for i in range(12)]
        with mock.patch.object(agent, "read_stream_lines", scripted_stream([stream])), \
                mock.patch.object(agent, "post_alert", side_effect=fake_post), \
                self.assertLogs("agent", level="INFO") as logs:
            agent.run_machine(URL, monitor, RecordingEvent(), dry_run=False, explain=agent.simple_reason)
        self.assertEqual(readings_at_post, [0, 1])   # before the first reading, then at the first evaluation
        self.assertTrue(monitor.history_alert_sent)
        self.assertEqual(monitor.stats["history_alerts"], 1)
        self.assertEqual("\n".join(logs.output).count("ALERT SENT (history)"), 1)


class StartupLogAndSummaryTest(unittest.TestCase):
    def test_one_history_line_per_machine(self):
        cases = {
            "SHIFT found: vibration_mm_s +6.0 spreads (day 7 median 8 vs normal 2)": worn_monitor(),
            "no sustained shift (largest: ": MachineMonitor(M, make_history_baseline()),
            "history check not evaluated: the baseline has no daily medians": MachineMonitor(M, make_baseline()),
        }
        for expected, monitor in cases.items():
            with self.subTest(expected), self.assertLogs("agent", level="INFO") as logs:
                agent.log_history_check(monitor)
            self.assertIn(expected, logs.output[0])

    def test_summary_counts_history_alerts(self):
        monitor = worn_monitor()
        monitor.confirm_history_alert()
        with self.assertLogs("agent", level="INFO") as logs:
            agent.print_summary({M: monitor})
        self.assertIn("alerts=0 history_alerts=1", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
