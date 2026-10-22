#!/usr/bin/env python3
"""Tests for deskwalk without hardware:  python3 test_protocol.py"""

import asyncio
import concurrent.futures
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

import server as s


class Frames(unittest.TestCase):
    """Command frames must match PROTOCOL.md byte for byte."""

    def test_ask_status(self):
        self.assertEqual(s.cmd_ask_status().hex(" "), "f7 a2 00 00 a2 fd")

    def test_set_speed(self):
        self.assertEqual(s.cmd_set_speed(3.0).hex(" "), "f7 a2 01 1e c1 fd")
        self.assertEqual(s.cmd_set_speed(0).hex(" "), "f7 a2 01 00 a3 fd")

    def test_set_mode(self):
        self.assertEqual(s.cmd_set_mode(s.MODE_MANUAL).hex(" "), "f7 a2 02 01 a5 fd")
        self.assertEqual(s.cmd_set_mode(s.MODE_STANDBY).hex(" "), "f7 a2 02 02 a6 fd")

    def test_start(self):
        self.assertEqual(s.cmd_start().hex(" "), "f7 a2 04 01 a7 fd")

    def test_prefs(self):
        self.assertEqual(s.cmd_set_pref(s.PREF_START_SPEED, 30).hex(" "), "f7 a6 04 00 00 00 1e c8 fd")
        self.assertEqual(s.cmd_set_pref(s.PREF_START_INTEL, 0).hex(" "), "f7 a6 05 00 00 00 00 ab fd")
        self.assertEqual(s.cmd_set_pref(s.PREF_SENSITIVITY, 1).hex(" "), "f7 a6 06 00 00 00 01 ad fd")

    def test_pref_validation(self):
        self.assertEqual(s.prefs_frames("start_speed", 9)[0], 6.0)
        with self.assertRaises(s.CommandError):
            s.prefs_frames("sensitivity", 4)
        with self.assertRaises(s.CommandError):
            s.prefs_frames("inteli", "yes")


class Status(unittest.TestCase):
    def test_parse(self):
        data = bytes.fromhex("f8 a2 01 1e 01 000708 00000f 0003e8 00 00 00 00 b7 fd")
        st = s.parse_status(data)
        self.assertEqual(st["state"], 1)
        self.assertEqual(st["speed"], 3.0)
        self.assertEqual(st["mode"], s.MODE_MANUAL)
        self.assertEqual(st["time"], 1800)
        self.assertEqual(st["dist"], 15)      # 150 m
        self.assertEqual(st["steps"], 1000)

    def test_ignore_other_frames(self):
        self.assertIsNone(s.parse_status(bytes.fromhex("f8 a7 00 00")))
        self.assertIsNone(s.parse_status(b""))

    def test_sim_belt_roundtrip(self):
        belt = s.SimBelt()
        belt.handle(s.cmd_set_mode(s.MODE_MANUAL))
        belt.handle(s.cmd_start())
        belt.step(0, belt.start_at + 0.1)          # countdown over
        belt.handle(s.cmd_set_speed(4.0))
        for _ in range(50):
            belt.step(0.2, time.monotonic())
        st = s.parse_status(belt.handle(s.cmd_ask_status()))
        self.assertEqual(st["speed"], 4.0)
        self.assertEqual(st["mode"], s.MODE_MANUAL)
        self.assertGreater(st["time"], 0)
        bad = bytearray(s.cmd_set_speed(1.0))
        bad[-2] ^= 0xFF                            # wrong checksum is ignored
        belt.handle(bytes(bad))
        self.assertEqual(belt.target, 4.0)


class Queue(unittest.TestCase):
    def test_only_last_speed_counts(self):
        futs = [concurrent.futures.Future() for _ in range(4)]
        items = [("speed", {"speed": 2.0}, futs[0]), ("mode", {"mode": "manual"}, futs[1]),
                 ("speed", {"speed": 2.5}, futs[2]), ("speed", {"speed": 3.0}, futs[3])]
        kept = s.merge_speed_commands(items)
        self.assertEqual([(n, a) for n, a, _ in kept], [("mode", {"mode": "manual"}), ("speed", {"speed": 3.0})])
        self.assertTrue(futs[0].done() and futs[2].done())
        self.assertFalse(futs[3].done())

    def test_clamp(self):
        self.assertEqual(s.clamp_speed(0.1), 0.5)
        self.assertEqual(s.clamp_speed("7"), 6.0)
        self.assertEqual(s.clamp_speed(3.04), 3.0)


class Fitness(unittest.TestCase):
    def test_stride_interpolation(self):
        cal = [{"speed": 3.0, "stride_m": 0.5}, {"speed": 5.0, "stride_m": 0.64}]
        self.assertAlmostEqual(s.stride_m(4.0, cal), 0.57)
        self.assertAlmostEqual(s.stride_m(6.0, cal), 0.71)     # extended with the edge slope
        self.assertAlmostEqual(s.stride_m(4.0, []), 0.61)      # default without calibration

    def test_calibration_limit(self):
        points = []
        for i, (speed, steps) in enumerate([(2, 80), (3, 95), (4, 105), (5, 120)]):
            points = s.add_calibration(points, s.calibration_point(speed, steps, 60, i))
        self.assertEqual([p["speed"] for p in points], [3, 4, 5])
        self.assertAlmostEqual(s.calibration_point(3.0, 100, 60, 0)["stride_m"], 0.5)

    def test_calories(self):
        self.assertAlmostEqual(s.kcal(4.0, 80, 3600), 240.0)   # MET 3.0 × 80 kg × 1 h
        self.assertEqual(s.met(0), 0)
        self.assertAlmostEqual(s.cadence(3.0, 0.5), 100.0)


class Sessions(unittest.TestCase):
    def test_streaks(self):
        goal = {"type": "km", "value": 2}
        days = {"2026-01-01": {"km": 3}, "2026-01-02": {"km": 2}, "2026-01-03": {"km": 1},
                "2026-01-04": {"km": 5}, "2026-01-05": {"km": 4}}
        result = s.streaks(days, goal, date(2026, 1, 5))
        self.assertEqual((result["current"], result["best"], result["today_reached"]), (2, 2, True))
        self.assertEqual(s.streaks(days, goal, date(2026, 1, 6))["current"], 2)  # today not reached yet

    def test_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = s.Store(Path(tmp) / "data.json")
            tracker = s.SessionTracker(store, end_after=10)
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

    def test_settings_validation(self):
        self.assertEqual(s.settings_changes({"weight_kg": "80"}), {"weight_kg": 80.0})
        with self.assertRaises(s.CommandError):
            s.settings_changes({"weight_kg": 5})
        with self.assertRaises(s.CommandError):
            s.settings_changes({"goal": {"type": "miles", "value": 3}})
        progs = s.settings_changes({"programs": [{"name": "A", "segments": [{"speed": 3.04, "seconds": 60}]}]})
        self.assertEqual(progs["programs"][0]["segments"][0]["speed"], 3.0)
        with self.assertRaises(s.CommandError):
            s.settings_changes({"programs": [{"name": "A", "segments": [{"speed": 9, "seconds": 60}]}]})


class Worker(unittest.TestCase):
    """End to end against the simulated link: connect, start, set speed."""

    def test_sim_start_and_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = s.Store(Path(tmp) / "data.json")
            worker = s.Worker(store, s.SessionTracker(store), sim=True)
            s.SimLink.belt = s.SimBelt()
            worker.start()
            deadline = time.time() + 10
            while not worker.snapshot()["connected"] and time.time() < deadline:
                time.sleep(0.2)
            self.assertTrue(worker.snapshot()["connected"])
            worker.call("speed", {"speed": 3.5})
            self.assertEqual(worker.call("start", {})["key"], "msg_running_at")
            time.sleep(2.5)
            self.assertEqual(worker.snapshot()["belt"]["speed"], 3.5)
            with self.assertRaises(s.CommandError):
                worker.call("speed", {})
            worker.call("stop", {})
            worker.call("disconnect", {})


if __name__ == "__main__":
    unittest.main(verbosity=1)
