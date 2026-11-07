"""
agent.py's maintenance booking (the assignment's bonus): POST /schedule-maintenance after a
confirmed alert, at most once per machine per run, never in a dry run, and never able to disturb
the alerting or the monitoring. `requests` and post_alert are faked: nothing is sent anywhere.
"""
import json
import unittest
from datetime import timedelta
from unittest import mock

from tests.helpers import START, TEST_MACHINE as M, make_baseline, make_history_baseline, reading
from tests.test_agent_io import RecordingEvent, alert_result, http_answer, scripted_stream
import agent
from agent import MachineMonitor

URL = "http://sim.test"
BOOKING = {"id": "MAINT-1", "machine_id": M, "slot": "2026-11-09T00:30:00.000Z",
           "booked_at": "2026-11-07T10:00:00.000Z", "technician": "Auto-assigned"}
WEAR = {"vibration_mm_s": [2.0, 2.0, 2.0, 2.0, 4.0, 6.0, 8.0]}   # a history alert is due


def no_network():
    """Fail the test if anything tries a real POST."""
    return mock.patch.object(agent.requests, "post", side_effect=AssertionError("no real POST allowed"))


class PostMaintenanceTest(unittest.TestCase):
    """The request and the three outcomes, following server.js exactly."""

    def test_success_sends_only_the_machine_id_and_returns_the_booking(self):
        answer = http_answer(201, json.dumps({"success": True, "booking": BOOKING}))
        with mock.patch.object(agent.requests, "post", return_value=answer) as fake_post:
            self.assertEqual(agent.post_maintenance(URL, M), ("booked", BOOKING))
        fake_post.assert_called_once_with(URL + "/schedule-maintenance", json={"machine_id": M},
                                          timeout=agent.POST_TIMEOUT_S)

    def test_an_http_error_is_a_rejection(self):
        for status, body in ((400, '{"error": "Body must include machine_id."}'), (500, "boom")):
            with self.subTest(status), mock.patch.object(agent.requests, "post", return_value=http_answer(status, body)):
                outcome, info = agent.post_maintenance(URL, M)
            self.assertEqual(outcome, "rejected")
            self.assertTrue(info.startswith(f"HTTP {status}: "))

    def test_no_answer_is_an_unknown_outcome(self):
        for exc in (agent.requests.exceptions.ConnectionError("refused"),
                    agent.requests.exceptions.Timeout("too slow")):
            with self.subTest(type(exc).__name__), mock.patch.object(agent.requests, "post", side_effect=exc):
                outcome, info = agent.post_maintenance(URL, M)
            self.assertEqual(outcome, "unknown")
            self.assertIn(type(exc).__name__, info)

    def test_201_with_an_unexpected_body_still_counts_as_booked(self):
        # 201 means the server booked it; asking again would book a second slot
        for body in ("[]", "<html>created</html>", '{"success": true}'):
            with self.subTest(body=body), mock.patch.object(agent.requests, "post", return_value=http_answer(201, body)):
                self.assertEqual(agent.post_maintenance(URL, M), ("booked", {}))


class ScheduleMaintenanceTest(unittest.TestCase):
    def schedule(self, monitor, outcome, dry_run=False, alert_id="ALERT-1"):
        with mock.patch.object(agent, "post_maintenance", return_value=outcome) as fake_post, \
                self.assertLogs("agent", level="INFO") as logs:
            agent.schedule_maintenance(monitor, URL, dry_run, "CNC: abnormal behaviour. Likely cause: X.", alert_id)
        return fake_post, "\n".join(logs.output)

    def test_success_is_logged_with_the_reason_and_never_repeated(self):
        monitor = MachineMonitor(M, make_baseline())
        fake_post, text = self.schedule(monitor, ("booked", BOOKING))
        fake_post.assert_called_once_with(URL, M)
        self.assertIn(f"machine={M} MAINTENANCE BOOKED id=MAINT-1 slot=2026-11-09T00:30:00.000Z "
                      "technician=Auto-assigned for alert ALERT-1 because: CNC: abnormal behaviour. Likely cause: X.", text)
        self.assertEqual((monitor.maintenance["state"], monitor.maintenance["alert_id"]), ("booked", "ALERT-1"))
        self.assertEqual(monitor.stats["maintenance"], 1)
        with mock.patch.object(agent, "post_maintenance") as again:
            agent.schedule_maintenance(monitor, URL, False, "a later alert", "ALERT-2")
        again.assert_not_called()   # one booking per machine per run

    def test_a_rejection_is_logged_and_may_be_tried_at_the_next_alert(self):
        monitor = MachineMonitor(M, make_baseline())
        _, text = self.schedule(monitor, ("rejected", "HTTP 500: boom"))
        self.assertIn("maintenance NOT booked (HTTP 500: boom); will try again only after the next confirmed alert", text)
        self.assertTrue(monitor.maintenance_due())
        self.assertEqual(monitor.stats["maintenance"], 0)
        fake_post, _ = self.schedule(monitor, ("booked", BOOKING), alert_id="ALERT-2")
        fake_post.assert_called_once()
        self.assertEqual((monitor.maintenance["alert_id"], monitor.stats["maintenance"]), ("ALERT-2", 1))

    def test_no_answer_is_never_repeated_because_it_may_have_been_booked(self):
        monitor = MachineMonitor(M, make_baseline())
        _, text = self.schedule(monitor, ("unknown", "Timeout: too slow"))
        self.assertIn("maintenance request got no answer (Timeout: too slow); it may or may not be booked", text)
        self.assertFalse(monitor.maintenance_due())
        with mock.patch.object(agent, "post_maintenance") as again:
            agent.schedule_maintenance(monitor, URL, False, "a later alert", "ALERT-2")
        again.assert_not_called()

    def test_a_bug_while_booking_never_raises(self):
        monitor = MachineMonitor(M, make_baseline())
        with mock.patch.object(agent, "post_maintenance", side_effect=RuntimeError("bug")), \
                self.assertLogs("agent", level="ERROR") as logs:
            agent.schedule_maintenance(monitor, URL, False, "reason", "ALERT-1")   # must not raise
        self.assertIn("unexpected error while booking maintenance", "\n".join(logs.output))
        self.assertEqual(monitor.maintenance["state"], "unknown")

    def test_dry_run_only_logs(self):
        monitor = MachineMonitor(M, make_baseline())
        with no_network(), self.assertLogs("agent", level="INFO") as logs:
            agent.schedule_maintenance(monitor, URL, True, "the alert text", None)
            agent.schedule_maintenance(monitor, URL, True, "a later alert", None)
        text = "\n".join(logs.output)
        self.assertEqual(text.count("DRY RUN, would schedule maintenance (POST /schedule-maintenance) "
                                    "because: the alert text"), 1)
        self.assertEqual((monitor.maintenance["state"], monitor.stats["maintenance"]), ("dry_run", 1))


class AfterLiveAlertTest(unittest.TestCase):
    """handle_result books maintenance only after a live alert was confirmed."""

    def handle(self, monitor, result, post_answer, maintenance=("booked", BOOKING), dry_run=False):
        with mock.patch.object(agent, "post_alert", return_value=post_answer), \
                mock.patch.object(agent, "post_maintenance", return_value=maintenance) as fake_maint, \
                self.assertLogs("agent", level="INFO") as logs:
            agent.handle_result(monitor, result, URL, dry_run, agent.simple_reason)
        return fake_maint, "\n".join(logs.output)

    def test_booked_after_a_successful_alert_with_its_id_and_reason(self):
        monitor = MachineMonitor(M, make_baseline())
        fake_maint, text = self.handle(monitor, alert_result(M), {"id": "ALERT-7"})
        fake_maint.assert_called_once_with(URL, M)
        self.assertLess(text.index("ALERT SENT id=ALERT-7"), text.index("MAINTENANCE BOOKED"))
        self.assertEqual(monitor.maintenance["alert_id"], "ALERT-7")
        self.assertIn("Likely cause: thermal stress or a cooling failure.", monitor.maintenance["reason"])

    def test_not_booked_when_the_alert_was_not_sent(self):
        monitor = MachineMonitor(M, make_baseline())
        fake_maint, _ = self.handle(monitor, alert_result(M), None)
        fake_maint.assert_not_called()
        self.assertIsNone(monitor.maintenance)

    def test_not_booked_without_an_alert_decision(self):
        for action in ("none", "waiting", "cooldown"):
            with self.subTest(action):
                monitor = MachineMonitor(M, make_baseline())
                fake_maint, _ = self.handle(monitor, alert_result(M, action), {"id": "X"})
                fake_maint.assert_not_called()

    def test_dry_run_sends_neither_the_alert_nor_the_booking(self):
        monitor = MachineMonitor(M, make_baseline())
        with no_network(), self.assertLogs("agent", level="INFO") as logs:
            agent.handle_result(monitor, alert_result(M), URL, True, agent.simple_reason)
        text = "\n".join(logs.output)
        self.assertIn("DRY RUN, would alert", text)
        self.assertIn("DRY RUN, would schedule maintenance", text)

    def test_a_failed_booking_leaves_the_alert_and_its_cooldown_as_they_were(self):
        for maintenance in (("rejected", "HTTP 500: boom"), ("unknown", "Timeout: too slow")):
            with self.subTest(maintenance[0]):
                monitor = MachineMonitor(M, make_baseline())
                _, text = self.handle(monitor, alert_result(M), {"id": "ALERT-7"}, maintenance=maintenance)
                self.assertIn("ALERT SENT id=ALERT-7", text)
                self.assertEqual((monitor.stats["alerts"], monitor.last_alert_time), (1, START))
                actions = [monitor._decide("high", START + timedelta(seconds=t)).action for t in range(0, 70, 5)]
                self.assertEqual(actions[-1], "cooldown")   # held 65 s: the cooldown from the alert applies


class AfterHistoryAlertTest(unittest.TestCase):
    def test_booked_after_the_history_alert_and_not_again_after_a_live_one(self):
        # PUMP_03's case: a history alert at startup, a live alert later; one booking in total
        monitor = MachineMonitor(M, make_history_baseline(WEAR))
        with mock.patch.object(agent, "post_alert", side_effect=[{"id": "ALERT-H"}, {"id": "ALERT-L"}]), \
                mock.patch.object(agent, "post_maintenance", return_value=("booked", BOOKING)) as fake_maint, \
                self.assertLogs("agent", level="INFO"):
            agent.send_history_alert(monitor, URL, False, agent.history_reason)
            agent.handle_result(monitor, alert_result(M), URL, False, agent.simple_reason)
        fake_maint.assert_called_once_with(URL, M)
        self.assertEqual(monitor.maintenance["alert_id"], "ALERT-H")
        self.assertIn("warning from the 7-day history", monitor.maintenance["reason"])
        self.assertEqual((monitor.stats["history_alerts"], monitor.stats["alerts"]), (1, 1))

    def test_not_booked_when_the_history_alert_was_not_sent(self):
        monitor = MachineMonitor(M, make_history_baseline(WEAR))
        with mock.patch.object(agent, "post_alert", return_value=None), \
                mock.patch.object(agent, "post_maintenance") as fake_maint, self.assertLogs("agent", level="INFO"):
            agent.send_history_alert(monitor, URL, False, agent.history_reason)
        fake_maint.assert_not_called()


class HealthyMachineTest(unittest.TestCase):
    def test_a_healthy_machine_never_books_maintenance(self):
        monitor = MachineMonitor(M, make_history_baseline())   # quiet history, normal live readings
        stream = [json.dumps(reading(START + timedelta(seconds=i))) for i in range(200)]
        with mock.patch.object(agent, "read_stream_lines", scripted_stream([stream])), \
                mock.patch.object(agent, "post_alert") as fake_alert, \
                mock.patch.object(agent, "post_maintenance") as fake_maint, \
                self.assertLogs("agent", level="INFO"):
            agent.run_machine(URL, monitor, RecordingEvent(), dry_run=False, explain=agent.simple_reason)
        fake_alert.assert_not_called()
        fake_maint.assert_not_called()
        self.assertEqual((monitor.stats["readings"], monitor.stats["maintenance"]), (200, 0))
        self.assertIsNone(monitor.maintenance)


class SummaryTest(unittest.TestCase):
    def test_summary_counts_bookings(self):
        monitor = MachineMonitor(M, make_baseline())
        monitor.record_maintenance("booked", alert_id="ALERT-1")
        with self.assertLogs("agent", level="INFO") as logs:
            agent.print_summary({M: monitor})
        self.assertIn("history_alerts=0 maintenance=1 malformed=0", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
