"""Tests without hardware:  python3 -m unittest discover tests"""

import concurrent.futures
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwalk import fitness as f  # noqa: E402
from deskwalk import protocol as p  # noqa: E402
from deskwalk.links import SimBelt, SimLink  # noqa: E402
from deskwalk.logs import MemoryLog  # noqa: E402
from deskwalk.sessions import SessionTracker, streaks, thin_samples  # noqa: E402
from deskwalk.settings import settings_changes  # noqa: E402
from deskwalk.store import Store  # noqa: E402
from deskwalk.web import App, make_server, static_path  # noqa: E402
from deskwalk.worker import Worker, counter_deltas, merge_speed_commands  # noqa: E402


class Frames(unittest.TestCase):
    """Command frames must match PROTOCOL.md byte for byte."""

    def test_ask_status(self):
        self.assertEqual(p.cmd_ask_status().hex(" "), "f7 a2 00 00 a2 fd")

    def test_set_speed(self):
        self.assertEqual(p.cmd_set_speed(3.0).hex(" "), "f7 a2 01 1e c1 fd")
        self.assertEqual(p.cmd_set_speed(0).hex(" "), "f7 a2 01 00 a3 fd")

    def test_set_mode(self):
        self.assertEqual(p.cmd_set_mode(p.MODE_MANUAL).hex(" "), "f7 a2 02 01 a5 fd")
        self.assertEqual(p.cmd_set_mode(p.MODE_STANDBY).hex(" "), "f7 a2 02 02 a6 fd")

    def test_start(self):
        self.assertEqual(p.cmd_start().hex(" "), "f7 a2 04 01 a7 fd")

    def test_prefs(self):
        self.assertEqual(p.cmd_set_pref(p.PREF_START_SPEED, 30).hex(" "), "f7 a6 04 00 00 00 1e c8 fd")
        self.assertEqual(p.cmd_set_pref(p.PREF_START_INTEL, 0).hex(" "), "f7 a6 05 00 00 00 00 ab fd")
        self.assertEqual(p.cmd_set_pref(p.PREF_SENSITIVITY, 1).hex(" "), "f7 a6 06 00 00 00 01 ad fd")

    def test_pref_validation(self):
        self.assertEqual(p.pref_frame("start_speed", 9)[0], 6.0)
        for name, value in (("sensitivity", 4), ("sensitivity", True), ("inteli", "yes"), ("nope", 1)):
            with self.assertRaises(p.CommandError):
                p.pref_frame(name, value)

    def test_clamp(self):
        self.assertEqual(p.clamp_speed(0.1), 0.5)
        self.assertEqual(p.clamp_speed("7"), 6.0)
        self.assertEqual(p.clamp_speed(3.04), 3.0)
        for bad in (None, "x", True, float("nan")):
            with self.assertRaises(p.CommandError):
                p.clamp_speed(bad)


class Status(unittest.TestCase):
    def test_parse(self):
        data = bytes.fromhex("f8 a2 01 1e 01 000708 00000f 0003e8 00 00 00 00 b7 fd")
        st = p.parse_status(data)
        self.assertEqual((st["state"], st["speed"], st["mode"]), (1, 3.0, p.MODE_MANUAL))
        self.assertEqual((st["time"], st["dist"], st["steps"]), (1800, 15, 1000))

    def test_ignore_other_frames(self):
        self.assertIsNone(p.parse_status(bytes.fromhex("f8 a7 00 00")))
        self.assertIsNone(p.parse_status(b""))

    def test_sim_belt_roundtrip(self):
        belt = SimBelt()
        belt.handle(p.cmd_set_mode(p.MODE_MANUAL))
        belt.handle(p.cmd_start())
        belt.step(0, belt.start_at + 0.1)          # countdown over
        belt.handle(p.cmd_set_speed(4.0))
        for _ in range(50):
            belt.step(0.2, time.monotonic())
        st = p.parse_status(belt.handle(p.cmd_ask_status()))
        self.assertEqual((st["speed"], st["mode"]), (4.0, p.MODE_MANUAL))
        self.assertGreater(st["time"], 0)
        bad = bytearray(p.cmd_set_speed(1.0))
        bad[-2] ^= 0xFF                            # a wrong checksum is ignored
        belt.handle(bytes(bad))
        self.assertEqual(belt.target, 4.0)

    def test_counter_deltas(self):
        a = {"time": 100, "dist": 10, "steps": 150}
        self.assertEqual(counter_deltas(a, None), {"time": 0, "dist": 0, "steps": 0})  # unknown baseline
        self.assertEqual(counter_deltas({"time": 101, "dist": 10, "steps": 152}, a), {"time": 1, "dist": 0, "steps": 2})
        # the belt started again: counters went down, the new values count in full
        self.assertEqual(counter_deltas({"time": 3, "dist": 0, "steps": 4}, a), {"time": 3, "dist": 0, "steps": 4})


class Queue(unittest.TestCase):
    def test_only_last_speed_counts(self):
        futs = [concurrent.futures.Future() for _ in range(4)]
        items = [("speed", {"speed": 2.0}, futs[0]), ("mode", {"mode": "manual"}, futs[1]),
                 ("speed", {"speed": 2.5}, futs[2]), ("speed", {"speed": 3.0}, futs[3])]
        kept = merge_speed_commands(items)
        self.assertEqual([(n, a) for n, a, _ in kept], [("mode", {"mode": "manual"}), ("speed", {"speed": 3.0})])
        self.assertTrue(futs[0].done() and futs[2].done())
        self.assertFalse(futs[3].done())


class Fitness(unittest.TestCase):
    def test_stride_interpolation(self):
        cal = [{"speed": 3.0, "stride_m": 0.5}, {"speed": 5.0, "stride_m": 0.64}]
        self.assertAlmostEqual(f.stride_m(4.0, cal), 0.57)
        self.assertAlmostEqual(f.stride_m(6.0, cal), 0.71)     # extended with the edge slope
        self.assertAlmostEqual(f.stride_m(4.0, []), 0.61)      # default without calibration
        self.assertAlmostEqual(f.stride_m(4.0, [{"speed": 3.0, "stride_m": 0.5}]), 0.59)

    def test_calibration_limit(self):
        points = []
        for i, (speed, steps) in enumerate([(2, 80), (3, 95), (4, 105), (5, 120)]):
            points = f.add_calibration(points, f.calibration_point(speed, steps, 60, i))
        self.assertEqual([pt["speed"] for pt in points], [3, 4, 5])
        self.assertAlmostEqual(f.calibration_point(3.0, 100, 60, 0)["stride_m"], 0.5)

    def test_calories(self):
        self.assertAlmostEqual(f.kcal(4.0, 80, 3600), 240.0)   # MET 3.0 x 80 kg x 1 h
        self.assertEqual(f.met(0), 0)
        self.assertAlmostEqual(f.cadence(3.0, 0.5), 100.0)
        self.assertEqual(f.cadence(3.0, 0), 0.0)


class Sessions(unittest.TestCase):
    def test_streaks(self):
        goal = {"type": "km", "value": 2}
        days = {"2026-01-01": {"km": 3}, "2026-01-02": {"km": 2}, "2026-01-03": {"km": 1},
                "2026-01-04": {"km": 5}, "2026-01-05": {"km": 4}}
        result = streaks(days, goal, date(2026, 1, 5))
        self.assertEqual((result["current"], result["best"], result["today_reached"]), (2, 2, True))
        self.assertEqual(streaks(days, goal, date(2026, 1, 6))["current"], 2)  # today not reached yet

    def test_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.json")
            tracker = SessionTracker(store, end_after=10)
            now = 1_000_000.0
            dev = {"time": 0, "dist": 0, "steps": 0}
            for i in range(60):                      # one minute at 3 km/h
                tracker.update(3.0, 1, 0.05 / 60, 1, now + i, dev)
            tracker.update(0.0, 0, 0, 0, now + 61, dev)
            self.assertIsNotNone(tracker.current)
            tracker.tick(now + 75, connected=True)   # 10 s standstill: session ends
            self.assertIsNone(tracker.current)
            saved = json.loads((Path(tmp) / "data.json").read_text())
            session = saved["sessions"][0]
            self.assertEqual(session["active_s"], 60)
            self.assertAlmostEqual(session["distance_km"], 0.05)
            self.assertGreater(len(session["samples"]), 5)
            self.assertFalse((Path(tmp) / "data.json.tmp").exists())

    def test_thin_samples(self):
        samples = [[i, 30, 100] for i in range(5000)]
        thin = thin_samples(samples, 720)
        self.assertEqual(len(thin), 720)
        self.assertEqual((thin[0][0], thin[-1][0]), (0, 4999))
        self.assertEqual(thin_samples(samples[:10], 720), samples[:10])

    def test_only_newest_open_session_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            now = time.time()

            def session(sid, start):
                return {"id": sid, "day": "2026-01-01", "start_ts": start, "end_ts": None, "last_ts": start + 60,
                        "last_running_ts": start + 60, "active_s": 60, "distance_km": 0.05, "belt_steps": 0,
                        "own_steps": 90, "calories": 3, "max_speed": 3, "dev_time": 60, "dev_dist": 5,
                        "dev_steps": 90, "samples": []}
            path.write_text(json.dumps({"settings": {}, "sessions": [session(1, now - 600), session(2, now - 120)]}))
            tracker = SessionTracker(Store(path))
            self.assertEqual(tracker.current["id"], 2)
            self.assertIsNotNone(tracker.store.sessions[0]["end_ts"])

    def test_broken_data_file_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            path.write_text("{not json")
            store = Store(path)
            self.assertEqual(store.sessions, [])
            self.assertTrue(any(x.name.startswith("data.json.broken-") for x in Path(tmp).iterdir()))

    def test_settings_validation(self):
        self.assertEqual(settings_changes({"weight_kg": "80"}), {"weight_kg": 80.0})
        for bad in ({"weight_kg": 5}, {"weight_kg": True}, {"weight_kg": "nan"}, {"goal": {"type": "miles", "value": 3}},
                    {"goal": "x"}, {"keep_running": "yes"}, {"programs": [{"name": "A", "segments": [{"speed": 9}]}]},
                    {"programs": ["x"]}):
            with self.assertRaises(p.CommandError, msg=bad):
                settings_changes(bad)
        progs = settings_changes({"programs": [{"name": "A", "segments": [{"speed": 3.04, "seconds": 60}]}]})
        self.assertEqual(progs["programs"][0]["segments"][0]["speed"], 3.0)
        self.assertEqual(settings_changes({"goal": {"type": "steps", "value": 8000.4}})["goal"]["value"], 8000)


class WorkerSim(unittest.TestCase):
    """End to end against the simulated link: connect, start, set speed."""

    def test_sim_start_and_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.json")
            worker = Worker(store, SessionTracker(store), sim=True)
            SimLink.belt = SimBelt()
            worker.start()
            deadline = time.time() + 10
            while not worker.snapshot()["connected"] and time.time() < deadline:
                time.sleep(0.2)
            self.assertTrue(worker.snapshot()["connected"])
            worker.call("speed", {"speed": 3.5})
            self.assertEqual(worker.call("start", {})["key"], "msg_running_at")
            time.sleep(2.5)
            self.assertEqual(worker.snapshot()["belt"]["speed"], 3.5)
            for name, args in (("speed", {}), ("../x", {}), ("delete_session", {"id": "abc"}),
                               ("delete_calibration", {"index": True})):
                with self.assertRaises(p.CommandError):
                    worker.call(name, args)
            worker.call("stop", {})
            worker.shutdown()
            self.assertFalse(worker.snapshot()["connected"])


class Web(unittest.TestCase):
    """HTTP security: only same-origin JSON may control the belt."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        SimLink.belt = SimBelt()
        cls.app = App(Path(cls.tmp.name) / "data.json", sim=True)
        cls.app.memory_log = MemoryLog()
        cls.app.worker.start()
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.app.worker.shutdown()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, r.headers, data

    def test_static_files(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body.lower())
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, _ = self.request("GET", "/", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        for bad in ("/../server.py", "/%2e%2e/server.py", "/.hidden", "/js/../../server.py", "/nope.js"):
            self.assertEqual(self.request("GET", bad)[0], 404, bad)

    def test_static_path(self):
        self.assertIsNone(static_path("/..%2fserver.py"))
        self.assertIsNotNone(static_path("/js/main.js"))

    def test_post_rules(self):
        cmd = json.dumps({"cmd": "forget_device"})
        json_type = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/cmd", cmd, json_type)[0], 200)
        # a form or no-cors fetch from another site sends text/plain
        self.assertEqual(self.request("POST", "/cmd", cmd, {"Content-Type": "text/plain"})[0], 409)
        self.assertEqual(self.request("POST", "/cmd", cmd, {**json_type, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/cmd", cmd, {**json_type, "Sec-Fetch-Site": "cross-site"})[0], 403)
        own = {**json_type, "Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.request("POST", "/cmd", cmd, own)[0], 200)
        self.assertEqual(self.request("POST", "/cmd", "[1]", json_type)[0], 409)

    def test_bad_length_and_host(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/cmd")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 409)
        conn.close()
        # DNS rebinding: a foreign host name pointing at this computer
        self.assertEqual(self.request("GET", "/", headers={"Host": f"evil.example:{self.port}"})[0], 421)
        self.assertEqual(self.request("GET", "/", headers={"Host": f"192.168.1.20:{self.port}"})[0], 200)
        self.assertEqual(self.request("GET", "/", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_address_not_exposed(self):
        self.app.store.update_settings(address="AA:BB:CC:DD:EE:FF")
        status, _, body = self.request("GET", "/history")
        self.assertEqual(status, 200)
        self.assertNotIn(b"AA:BB", body)
        self.assertNotIn("address", self.app.state()["settings"])
        self.app.store.update_settings(address=None)


if __name__ == "__main__":
    unittest.main(verbosity=1)
