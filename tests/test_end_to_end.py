"""
End to end: the real run_agent() (four threads, SSE parsing, decisions, POST /alert) against
the fake simulator in tests/fake_simulator.py, which replays the recorded captures about 40x
faster than real time on 127.0.0.1. Each test takes about 10 seconds.
"""
import unittest

from tests import helpers, sim_history
from tests.fake_simulator import FakeSimulator
import agent
from common import parse_timestamp

RUN_SECONDS = 10   # wall-clock seconds, enough for about 350 readings per machine


class EndToEndTest(unittest.TestCase):
    def run_agent(self, sim, dry_run, baseline=None):
        with self.assertLogs("agent", level="INFO") as logs:
            monitors = agent.run_agent(sim.base_url, baseline or helpers.fixture_baseline(),
                                       dry_run=dry_run, duration=RUN_SECONDS)
        return monitors, "\n".join(logs.output)

    def test_live_run_alerts_the_faulty_machines_once_despite_a_dropped_stream(self):
        with FakeSimulator(drop_after=100) as sim:   # every machine's first stream breaks after 100 readings
            monitors, text = self.run_agent(sim, dry_run=False)
        self.assertEqual(sim.stored_per_machine(), {"CNC_01": 0, "CNC_02": 1, "PUMP_03": 1, "CONVEYOR_04": 0})
        for alert in sim.alerts:
            machine = alert["machine_id"]
            self.assertTrue(alert["reason"].startswith(f"{machine}: abnormal behaviour in the live stream. Likely cause: "))
            self.assertEqual(alert["reading"]["machine_id"], machine)
            # same moment as in the offline replay: the reconnect lost no data
            seconds = (parse_timestamp(alert["reading"]["timestamp"]) - parse_timestamp(sim.first_timestamp[machine])).total_seconds()
            self.assertEqual(round(seconds), {"CNC_02": 173, "PUMP_03": 172}[machine])
        for machine, monitor in monitors.items():
            with self.subTest(machine):
                self.assertEqual((monitor.stats["stream_errors"], monitor.stats["reconnects"]), (1, 1))
                self.assertEqual(monitor.stats["unexpected_errors"], 0)
                self.assertGreater(monitor.stats["readings"], 200)
        self.assertEqual(max(sim.max_open.values()), 1)   # never two connections to one machine
        self.assertEqual(text.count("ALERT SENT"), 2)

    def test_dry_run_sends_nothing(self):
        with FakeSimulator() as sim:
            _, text = self.run_agent(sim, dry_run=True)
        self.assertEqual(sim.posts, [])
        self.assertEqual(sim.maintenance_posts, [])
        for machine in ("CNC_02", "PUMP_03"):
            self.assertEqual(text.count(f"machine={machine} DRY RUN, would alert"), 1)
        for machine in ("CNC_01", "CONVEYOR_04"):
            self.assertNotIn(f"machine={machine} DRY RUN", text)

    def test_history_and_live_alerts_for_all_four_machines(self):
        # The recordings' baseline plus daily medians from a simulated 7-day history: CNC_01's
        # bearing wear and PUMP_03's falling rpm are in the history; the live part is unchanged.
        with FakeSimulator() as sim:
            monitors, text = self.run_agent(sim, dry_run=False, baseline=sim_history.fixture_baseline_with_history())
        history = [a for a in sim.alerts if " warning from the 7-day history. " in a["reason"]]
        live = [a for a in sim.alerts if " abnormal behaviour in the live stream. " in a["reason"]]
        self.assertEqual(len(history) + len(live), len(sim.alerts))
        self.assertEqual(sorted(a["machine_id"] for a in history), ["CNC_01", "PUMP_03"])
        self.assertEqual(sorted(a["machine_id"] for a in live), ["CNC_02", "PUMP_03"])
        self.assertEqual(sim.stored_per_machine(), {"CNC_01": 1, "CNC_02": 1, "PUMP_03": 2, "CONVEYOR_04": 0})

        reasons = {a["machine_id"]: a["reason"] for a in history}
        self.assertIn("Likely cause: bearing wear.", reasons["CNC_01"])
        self.assertIn("developing clog", reasons["PUMP_03"])
        self.assertTrue(all(a["reading"] is None for a in history))   # no live reading caused them
        pump = [a["reason"] for a in sim.alerts if a["machine_id"] == "PUMP_03"]
        self.assertIn("7-day history", pump[0])                       # sent before streaming started
        self.assertIn("Likely cause: cavitation", pump[1])            # the live alert, as before
        for alert in live:   # the live alerts come at the same moment as without the history check
            machine = alert["machine_id"]
            seconds = (parse_timestamp(alert["reading"]["timestamp"]) - parse_timestamp(sim.first_timestamp[machine])).total_seconds()
            self.assertEqual(round(seconds), {"CNC_02": 173, "PUMP_03": 172}[machine])
        self.assertEqual({m: monitor.stats["history_alerts"] for m, monitor in monitors.items()},
                         {"CNC_01": 1, "CNC_02": 0, "PUMP_03": 1, "CONVEYOR_04": 0})
        self.assertEqual(text.count("ALERT SENT (history)"), 2)
        self.assertIn("machine=CONVEYOR_04 history check: no sustained shift", text)

    def test_maintenance_is_booked_once_per_alerting_machine_after_its_first_alert(self):
        with FakeSimulator() as sim:
            monitors, text = self.run_agent(sim, dry_run=False, baseline=sim_history.fixture_baseline_with_history())
        self.assertEqual(sorted(b["machine_id"] for b in sim.bookings), ["CNC_01", "CNC_02", "PUMP_03"])
        self.assertEqual(sorted(sim.maintenance_posts, key=lambda body: body["machine_id"]),
                         [{"machine_id": m} for m in ("CNC_01", "CNC_02", "PUMP_03")])   # the contract, nothing more
        for machine in ("CNC_01", "CNC_02", "PUMP_03"):
            with self.subTest(machine):
                mine = [kind for kind, m in sim.events if m == machine]
                self.assertEqual(mine[0], "alert")              # never before the machine's first alert
                self.assertEqual(mine.count("maintenance"), 1)
                self.assertEqual(monitors[machine].maintenance["state"], "booked")
        self.assertIsNone(monitors["CONVEYOR_04"].maintenance)   # the healthy machine
        self.assertEqual(sim.stored_per_machine(), {"CNC_01": 1, "CNC_02": 1, "PUMP_03": 2, "CONVEYOR_04": 0})
        self.assertEqual(text.count("MAINTENANCE BOOKED"), 3)

    def test_failing_maintenance_bookings_change_nothing_else(self):
        with FakeSimulator(maintenance_status=500) as sim:
            monitors, text = self.run_agent(sim, dry_run=False)
        self.assertEqual(sim.stored_per_machine(), {"CNC_01": 0, "CNC_02": 1, "PUMP_03": 1, "CONVEYOR_04": 0})
        for alert in sim.alerts:   # the live alerts come at the same moment as without bookings
            machine = alert["machine_id"]
            seconds = (parse_timestamp(alert["reading"]["timestamp"]) - parse_timestamp(sim.first_timestamp[machine])).total_seconds()
            self.assertEqual(round(seconds), {"CNC_02": 173, "PUMP_03": 172}[machine])
        self.assertEqual(sorted(body["machine_id"] for body in sim.maintenance_posts), ["CNC_02", "PUMP_03"])
        self.assertEqual(sim.bookings, [])
        self.assertEqual(text.count("maintenance NOT booked (HTTP 500"), 2)
        for machine, monitor in monitors.items():
            with self.subTest(machine):
                self.assertEqual((monitor.stats["unexpected_errors"], monitor.stats["stream_errors"]), (0, 0))
                self.assertGreater(monitor.stats["readings"], 200)


if __name__ == "__main__":
    unittest.main()
