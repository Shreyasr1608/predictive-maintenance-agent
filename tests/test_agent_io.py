"""agent.py's I/O layer with `requests` replaced by fakes: posting alerts, dry run, reading the
stream, reconnecting with back-off, and the startup helpers. Nothing here opens a connection."""
import contextlib
import io
import json
import logging
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.helpers import START, TEST_MACHINE as M, make_baseline, reading
import agent
from agent import Assessment, Decision, Evidence, MachineMonitor

URL = "http://sim.test"


def http_answer(status, body):
    """A stand-in for requests.Response."""
    answer = mock.Mock(status_code=status, text=body)
    answer.json.side_effect = lambda: json.loads(body)
    return answer


class PostAlertTest(unittest.TestCase):
    def test_success_returns_the_stored_alert(self):
        stored = {"id": "ALERT-1", "machine_id": M}
        answer = http_answer(201, json.dumps({"success": True, "alert": stored}))
        with mock.patch.object(agent.requests, "post", return_value=answer) as fake_post:
            self.assertEqual(agent.post_alert(URL, M, "reason text", {"rpm": 1}), stored)
        fake_post.assert_called_once_with(URL + "/alert", json={"machine_id": M, "reason": "reason text",
                                                                 "reading": {"rpm": 1}}, timeout=agent.POST_TIMEOUT_S)

    def test_failures_return_none_so_the_agent_retries(self):
        failures = {
            "HTTP 500": {"return_value": http_answer(500, '{"error": "boom"}')},
            "HTTP 400": {"return_value": http_answer(400, '{"error": "Body must include machine_id and reason."}')},
            "connection refused": {"side_effect": agent.requests.exceptions.ConnectionError("refused")},
            "timeout": {"side_effect": agent.requests.exceptions.Timeout("too slow")},
        }
        for name, behaviour in failures.items():
            with self.subTest(name), mock.patch.object(agent.requests, "post", **behaviour):
                with self.assertLogs("agent", level="ERROR"):
                    self.assertIsNone(agent.post_alert(URL, M, "r", None))

    def test_201_with_an_unexpected_body_counts_as_sent(self):
        # regression: a JSON list raised AttributeError, and a non-JSON body made the agent send
        # the alert again, although 201 means the server stored it (a duplicate)
        for body in ("[]", "<html>created</html>", '{"success": true}'):
            with self.subTest(body=body), mock.patch.object(agent.requests, "post", return_value=http_answer(201, body)):
                with self.assertLogs("agent", level="WARNING"):
                    self.assertEqual(agent.post_alert(URL, M, "r", None), {})


def alert_result(machine, action="alert"):
    """An (Assessment, Decision) pair as MachineMonitor.evaluate() returns it."""
    detail = {"share": 0.9, "lookback_s": 60, "value": 80.0, "baseline": 70.0}
    items = (Evidence("spike", "temperature_C", ("temperature_C",), 1.8, detail),)
    return Assessment(machine, START, "high", 1.8, items, reading(START, machine)), Decision(action, "test")


class HandleResultTest(unittest.TestCase):
    def setUp(self):
        self.monitor = MachineMonitor(M, make_baseline())

    def handle(self, result, dry_run, post_answer):
        with mock.patch.object(agent, "post_alert", return_value=post_answer) as fake_post, \
                mock.patch.object(agent, "post_maintenance", return_value=("booked", {})), \
                self.assertLogs("agent", level="INFO") as logs:
            agent.handle_result(self.monitor, result, URL, dry_run, agent.simple_reason)
        return fake_post, "\n".join(logs.output)

    def test_dry_run_never_posts_but_starts_the_cooldown(self):
        fake_post, text = self.handle(alert_result(M), dry_run=True, post_answer={"id": "X"})
        fake_post.assert_not_called()
        self.assertIn("DRY RUN, would alert", text)
        self.assertEqual(self.monitor.last_alert_time, START)

    def test_successful_post_sends_the_explanation_and_starts_the_cooldown(self):
        fake_post, text = self.handle(alert_result(M), dry_run=False, post_answer={"id": "ALERT-7"})
        base_url, machine, reason, latest = fake_post.call_args.args
        self.assertEqual((base_url, machine, latest["machine_id"]), (URL, M, M))
        self.assertIn("temperature above its normal range in 90% of the last 60 s (latest 80.0 C, normal 70.0)", reason)
        self.assertIn("Likely cause: thermal stress or a cooling failure.", reason)   # temperature alone
        self.assertIn("ALERT SENT id=ALERT-7", text)
        self.assertEqual((self.monitor.stats["alerts"], self.monitor.last_alert_time), (1, START))

    def test_failed_post_starts_no_cooldown(self):
        _, text = self.handle(alert_result(M), dry_run=False, post_answer=None)
        self.assertIn("alert NOT sent; will retry", text)
        self.assertEqual((self.monitor.stats["alerts"], self.monitor.last_alert_time), (0, None))

    def test_other_decisions_never_post(self):
        for action in ("none", "waiting", "cooldown"):
            with self.subTest(action):
                fake_post, _ = self.handle(alert_result(M, action), dry_run=False, post_answer={"id": "X"})
                fake_post.assert_not_called()


class FakeStreamResponse:
    """What requests.get(..., stream=True) returns, reduced to what read_stream_lines uses."""

    def __init__(self, status, lines):
        self.status_code, self.lines = status, lines

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_lines(self):
        return iter(self.lines)


class ReadStreamLinesTest(unittest.TestCase):
    def test_yields_only_the_data_payloads(self):
        wire = [b": connected to TEST_01 stream", b"", b'data: {"a": 1}', b"", b"event: other", b'data:{"b": 2}', b""]
        with mock.patch.object(agent.requests, "get", return_value=FakeStreamResponse(200, wire)) as fake_get, \
                self.assertLogs("agent", level="INFO"):
            payloads = list(agent.read_stream_lines(URL, M, threading.Event()))
        self.assertEqual(payloads, ['{"a": 1}', '{"b": 2}'])
        self.assertEqual(fake_get.call_args.args[0], URL + "/stream/TEST_01")
        self.assertEqual(fake_get.call_args.kwargs["timeout"],
                         (agent.STREAM_CONNECT_TIMEOUT_S, agent.STREAM_READ_TIMEOUT_S))

    def test_stops_when_the_agent_is_stopping(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(agent.requests, "get", return_value=FakeStreamResponse(200, [b'data: {"a": 1}'])), \
                self.assertLogs("agent", level="INFO"):
            self.assertEqual(list(agent.read_stream_lines(URL, M, stop)), [])

    def test_error_status_raises_a_request_exception(self):
        with mock.patch.object(agent.requests, "get", return_value=FakeStreamResponse(404, [])):
            with self.assertRaises(agent.requests.exceptions.RequestException):
                list(agent.read_stream_lines(URL, "NOPE_99", threading.Event()))


class RecordingEvent(threading.Event):
    """A stop flag whose wait() returns at once and remembers how long it was asked to wait."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


def scripted_stream(connections):
    """A fake read_stream_lines(): each call plays the next connection (items to yield, or an
    exception to raise). When the script is used up, it sets the stop flag."""
    script = iter(connections)

    def fake(base_url, machine_id, stop_event):
        try:
            items = next(script)
        except StopIteration:
            stop_event.set()
            return
        for item in items:
            if isinstance(item, BaseException):
                raise item
            yield item
    return fake


class RunMachineTest(unittest.TestCase):
    def run_machine(self, connections, monitor=None):
        monitor = monitor or MachineMonitor(M, make_baseline())
        stop = RecordingEvent()
        with mock.patch.object(agent, "read_stream_lines", scripted_stream(connections)), \
                self.assertLogs("agent", level="INFO") as logs:
            agent.run_machine(URL, monitor, stop, dry_run=True, explain=agent.simple_reason)
        return monitor, stop.waits, "\n".join(logs.output)

    def test_reconnects_with_back_off_and_survives_unexpected_errors(self):
        refused = agent.requests.exceptions.ConnectionError("refused")
        good = [json.dumps(reading(START + timedelta(seconds=i))) for i in range(3)]
        monitor, waits, text = self.run_machine([
            [refused],                                # wait 1 s
            [refused],                                # wait 2 s
            good,                                     # data flows, then the server closes: wait 1 s again
            ["this is not json", RuntimeError("a bug")],   # no valid data in between: wait 2 s
        ])
        self.assertEqual(waits, [1, 2, 1, 2])
        counts = {k: monitor.stats[k] for k in ("readings", "malformed", "stream_errors", "unexpected_errors", "reconnects")}
        self.assertEqual(counts, {"readings": 3, "malformed": 1, "stream_errors": 2, "unexpected_errors": 1, "reconnects": 4})
        self.assertIn("stream closed by the server", text)
        self.assertIn("RuntimeError: a bug", text)   # the full traceback is in the log
        self.assertIn("monitor stopped", text)

    def test_wait_grows_to_30_seconds_and_stays_there(self):
        down = agent.requests.exceptions.ConnectionError("down")
        _, waits, _ = self.run_machine([[down] for _ in range(8)])
        self.assertEqual(waits, [1, 2, 4, 8, 15, 30, 30, 30])


class StartupTest(unittest.TestCase):
    def test_defaults_never_touch_baseline_json(self):
        args = agent.parse_args([])
        self.assertEqual(args.save_baseline, "agent_baseline.json")
        self.assertEqual((args.base_url, args.log_file, args.dry_run, args.duration, args.use_saved_baseline),
                         ("http://localhost:3000", "agent.log", False, None, None))

    def test_duration_must_be_positive(self):
        for bad in ("0", "-5", "nan"):
            with self.subTest(bad), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                agent.parse_args(["--duration", bad])

    def test_saved_baseline_is_loaded_and_nothing_is_written(self):
        with mock.patch.object(agent, "save_baseline") as fake_save, \
                mock.patch.object(agent, "build_from_server") as fake_build, \
                self.assertLogs("agent", level="INFO"):
            loaded = agent.prepare_baseline(URL, str(helpers.FIXTURES / "baseline.json"), "unused.json")
        self.assertEqual(sorted(loaded["machines"]), sorted(helpers.MACHINES))
        fake_save.assert_not_called()
        fake_build.assert_not_called()

    def test_saved_file_that_is_not_a_baseline_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "not_a_baseline.json"
            path.write_text('{"meta": {}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                agent.prepare_baseline(URL, str(path), "unused.json")

    def test_rebuilt_baseline_is_saved_where_asked(self):
        built = make_baseline()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "agent_baseline.json"
            with mock.patch.object(agent, "build_from_server", return_value=built) as fake_build, \
                    self.assertLogs("agent", level="INFO"):
                self.assertIs(agent.prepare_baseline(URL, None, str(target)), built)
            self.assertEqual(fake_build.call_args.args[0], URL)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), built)

    def test_failed_save_is_only_a_warning(self):
        built = make_baseline()
        with mock.patch.object(agent, "build_from_server", return_value=built), \
                mock.patch.object(agent, "save_baseline", side_effect=OSError("disk full")), \
                self.assertLogs("agent", level="WARNING") as logs:
            self.assertIs(agent.prepare_baseline(URL, None, "agent_baseline.json"), built)
        self.assertIn("could not save the baseline", "\n".join(logs.output))

    def test_main_exits_with_1_when_the_simulator_cannot_be_reached(self):
        root = logging.getLogger()
        handlers_before, level_before = list(root.handlers), root.level
        unreachable = agent.SimulatorError("cannot connect to http://sim.test/machines - is the simulator running?")
        with tempfile.TemporaryDirectory() as folder:
            try:
                with mock.patch.object(agent, "build_from_server", side_effect=unreachable), \
                        contextlib.redirect_stdout(io.StringIO()) as console:
                    code = agent.main(["--base-url", URL, "--log-file", str(Path(folder) / "test.log"),
                                       "--save-baseline", str(Path(folder) / "b.json")])
            finally:   # undo setup_logging(), so later tests are not printed to the console
                for handler in root.handlers[:]:
                    if handler not in handlers_before:
                        root.removeHandler(handler)
                        handler.close()
                root.setLevel(level_before)
        self.assertEqual(code, 1)
        self.assertIn("is the simulator running?", console.getvalue())


if __name__ == "__main__":
    unittest.main()
