"""
A small stand-in for the Node.js simulator, used only by the end-to-end tests. It listens on
127.0.0.1 and follows the contract of the simulator's server.js:
  GET  /stream/{id}   Server-Sent Events over chunked transfer: a ": connected" comment, then
                      one "data: {json}" event per reading
  POST /alert         201 {"success": true, "alert": {...}}; 400 if machine_id or reason is missing
  POST /schedule-maintenance
                      201 {"success": true, "booking": {...}}; 400 if machine_id is missing or
                      unknown. With maintenance_status=N it answers HTTP N without booking.
  GET  /alerts        {"count", "alerts"}
The streams replay the recorded captures in tests/fixtures (with their own timestamps) much
faster than real time, looping with timestamps that carry on after the last reading.
With drop_after=N, every machine's FIRST stream is cut off abruptly after N readings.
"""
import csv
import json
import threading
import time
from collections import Counter
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import FIXTURES, MACHINES, iso
from common import SENSOR_FIELDS, parse_timestamp


class FakeSimulator:
    def __init__(self, seconds_per_reading=0.025, drop_after=None, maintenance_status=201):
        self.seconds_per_reading = seconds_per_reading   # 0.025 s = 40x faster than the real 1 s
        self.drop_after = drop_after
        self.maintenance_status = maintenance_status
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.rows = {m: self._load(m) for m in MACHINES}
        self.sent = Counter()            # readings sent per machine, across connections
        self.connections = Counter()
        self.open_streams = Counter()
        self.max_open = Counter()        # most streams open at the same time, per machine
        self.first_timestamp = {}
        self.posts = []                  # every POST body received on any path except /schedule-maintenance
        self.alerts = []                 # alerts stored (answered with 201)
        self.maintenance_posts = []      # every POST /schedule-maintenance body received
        self.bookings = []               # bookings stored (answered with 201)
        self.events = []                 # ("alert" or "maintenance", machine_id) in arrival order
        self.server = None

    @staticmethod
    def _load(machine):
        with open(FIXTURES / f"live_{machine}.csv", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            for sensor in SENSOR_FIELDS:
                row[sensor] = float(row[sensor])
        return rows

    def next_reading(self, machine):
        """The next reading of a machine (call with the lock held)."""
        rows = self.rows[machine]
        loop, k = divmod(self.sent[machine], len(rows))
        reading = dict(rows[k])
        if loop:
            span = (parse_timestamp(rows[-1]["timestamp"]) - parse_timestamp(rows[0]["timestamp"])).total_seconds()
            shifted = parse_timestamp(reading["timestamp"]) + timedelta(seconds=loop * (span + 1.0))
            reading["timestamp"] = iso(shifted)
        self.first_timestamp.setdefault(machine, reading["timestamp"])
        self.sent[machine] += 1
        return reading

    def stored_per_machine(self):
        counts = Counter(alert["machine_id"] for alert in self.alerts)
        return {m: counts.get(m, 0) for m in MACHINES}

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)   # port 0: any free port
        self.server.daemon_threads = True
        self.server.sim = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc_info):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        return False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # needed for chunked transfer, as Node/Express sends it

    def log_message(self, *args):   # keep the test output quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _chunk(self, text):
        data = text.encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")

    def do_GET(self):
        sim = self.server.sim
        parts = self.path.strip("/").split("/")
        if parts == ["alerts"]:
            with sim.lock:
                alerts = list(sim.alerts)
            return self._json(200, {"count": len(alerts), "alerts": alerts})
        if len(parts) == 2 and parts[0] == "stream" and parts[1] in MACHINES:
            return self._stream(parts[1])
        return self._json(404, {"error": "not found"})

    def _stream(self, machine):
        sim = self.server.sim
        with sim.lock:
            sim.connections[machine] += 1
            first_connection = sim.connections[machine] == 1
            sim.open_streams[machine] += 1
            sim.max_open[machine] = max(sim.max_open[machine], sim.open_streams[machine])
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(f": connected to {machine} stream\n\n")
            sent_here = 0
            while not sim.stopping.is_set():
                time.sleep(sim.seconds_per_reading)
                if first_connection and sim.drop_after is not None and sent_here >= sim.drop_after:
                    return   # cut the connection without ending the chunked response
                with sim.lock:
                    reading = sim.next_reading(machine)
                self._chunk(f"data: {json.dumps(reading)}\n\n")
                sent_here += 1
        except OSError:
            return   # the agent closed the connection
        finally:
            with sim.lock:
                sim.open_streams[machine] -= 1

    def do_POST(self):
        sim = self.server.sim
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        if self.path == "/schedule-maintenance":
            return self._maintenance(body)
        with sim.lock:
            sim.posts.append(body)
            sim.events.append(("alert", body.get("machine_id")))
        if self.path != "/alert" or not body.get("machine_id") or not body.get("reason"):
            return self._json(400, {"error": "Body must include machine_id and reason."})
        with sim.lock:
            alert = {"id": f"ALERT-TEST-{len(sim.alerts) + 1}", "machine_id": body["machine_id"],
                     "reason": body["reason"], "reading": body.get("reading")}
            sim.alerts.append(alert)
        return self._json(201, {"success": True, "alert": alert})

    def _maintenance(self, body):
        sim = self.server.sim
        with sim.lock:
            sim.maintenance_posts.append(body)
            sim.events.append(("maintenance", body.get("machine_id")))
        if sim.maintenance_status != 201:
            return self._json(sim.maintenance_status, {"error": "test failure"})
        if body.get("machine_id") not in MACHINES:
            return self._json(400, {"error": "Body must include machine_id."})
        with sim.lock:
            booking = {"id": f"MAINT-TEST-{len(sim.bookings) + 1}", "machine_id": body["machine_id"],
                       "slot": body.get("proposed_slot") or "2026-11-09T00:30:00.000Z",
                       "booked_at": "2026-11-07T10:00:00.000Z", "technician": "Auto-assigned"}
            sim.bookings.append(booking)
        return self._json(201, {"success": True, "booking": booking})
