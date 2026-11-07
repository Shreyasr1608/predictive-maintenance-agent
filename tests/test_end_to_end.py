"""
End to end: the real run_agent() (four threads, SSE parsing, decisions, POST /alert) against
the fake simulator in tests/fake_simulator.py, which replays the recorded captures about 40x
faster than real time on 127.0.0.1. Each test takes about 10 seconds.
"""
import unittest

from tests import helpers
from tests.fake_simulator import FakeSimulator
import agent
from common import parse_timestamp

RUN_SECONDS = 10   # wall-clock seconds, enough for about 350 readings per machine


class EndToEndTest(unittest.TestCase):
    def run_agent(self, sim, dry_run):
        with self.assertLogs("agent", level="INFO") as logs:
            monitors = agent.run_agent(sim.base_url, helpers.fixture_baseline(), dry_run=dry_run, duration=RUN_SECONDS)
        return monitors, "\n".join(logs.output)

    def test_live_run_alerts_the_faulty_machines_once_despite_a_dropped_stream(self):
        with FakeSimulator(drop_after=100) as sim:   # every machine's first stream breaks after 100 readings
            monitors, text = self.run_agent(sim, dry_run=False)
        self.assertEqual(sim.stored_per_machine(), {"CNC_01": 0, "CNC_02": 1, "PUMP_03": 1, "CONVEYOR_04": 0})
        for alert in sim.alerts:
            machine = alert["machine_id"]
            self.assertTrue(alert["reason"].startswith(f"{machine}: sustained abnormal behaviour"))
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
        for machine in ("CNC_02", "PUMP_03"):
            self.assertEqual(text.count(f"machine={machine} DRY RUN, would alert"), 1)
        for machine in ("CNC_01", "CONVEYOR_04"):
            self.assertNotIn(f"machine={machine} DRY RUN", text)


if __name__ == "__main__":
    unittest.main()
