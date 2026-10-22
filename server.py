#!/usr/bin/env python3
"""deskwalk: minimal web controller for the Kingsmith WalkingPad A1 Pro.

    python3 server.py          talk to the belt via Bluetooth (needs: pip install bleak)
    python3 server.py --sim    simulated belt, no hardware needed

Then open http://127.0.0.1:5000. The wire protocol is documented in PROTOCOL.md.

Layout of this file:
    protocol    byte frames to and from the belt
    fitness     stride length, cadence, calories
    store       data.json (settings and sessions), written atomically
    sessions    session tracking, daily goal, streaks, records
    links       Bluetooth link (bleak) and simulated link
    worker      Bluetooth thread with its own asyncio loop and command queue
    http        ThreadingHTTPServer with the JSON API and Server-Sent Events
"""

import argparse
import asyncio
import concurrent.futures
import copy
import inspect
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time
from collections import deque
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
log = logging.getLogger("deskwalk")


# ── Protocol (see PROTOCOL.md) ──────────────────────────────────────────
FE01 = "0000fe01-0000-1000-8000-00805f9b34fb"  # notify: status frames from the belt
FE02 = "0000fe02-0000-1000-8000-00805f9b34fb"  # write: command frames to the belt

MODE_AUTO, MODE_MANUAL, MODE_STANDBY = 0, 1, 2
MODE_NAMES = {MODE_AUTO: "auto", MODE_MANUAL: "manual", MODE_STANDBY: "standby"}
MODE_BY_NAME = {v: k for k, v in MODE_NAMES.items()}

PREF_MAX_SPEED = 3
PREF_START_SPEED = 4
PREF_START_INTEL = 5
PREF_SENSITIVITY = 6     # 1 = high, 2 = medium, 3 = low
PREF_CHILD_LOCK = 9

MIN_SPEED, MAX_SPEED = 0.5, 6.0  # km/h


def frame(*payload: int) -> bytes:
    """Command frame: F7, payload, checksum (sum of payload mod 256), FD."""
    return bytes([0xF7, *payload, sum(payload) % 256, 0xFD])


def cmd_ask_status() -> bytes:
    return frame(0xA2, 0x00, 0x00)


def cmd_set_speed(kmh: float) -> bytes:
    return frame(0xA2, 0x01, int(round(kmh * 10)))


def cmd_set_mode(mode: int) -> bytes:
    return frame(0xA2, 0x02, mode)


def cmd_start() -> bytes:
    return frame(0xA2, 0x04, 0x01)


def cmd_set_pref(key: int, value: int) -> bytes:
    value = int(value)
    return frame(0xA6, key, 0x00, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def parse_status(data: bytes) -> dict | None:
    """Status frame F8 A2 ... from fe01, or None for anything else."""
    if len(data) < 17 or data[0] != 0xF8 or data[1] != 0xA2:
        return None
    return {
        "state": data[2],                              # 0 stopped, 1 running, 5 standby, 9 starting
        "speed": data[3] / 10,                         # km/h
        "mode": data[4],                               # 0 auto, 1 manual, 2 standby
        "time": int.from_bytes(data[5:8], "big"),      # s since start
        "dist": int.from_bytes(data[8:11], "big"),     # units of 10 m
        "steps": int.from_bytes(data[11:14], "big"),   # belt step counter
        "button": data[16],
    }


def clamp_speed(value) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise CommandError("err_invalid_speed")
    return round(min(MAX_SPEED, max(MIN_SPEED, speed)), 1)


def is_walkingpad_name(name: str) -> bool:
    name = name.lower()
    return "walkingpad" in name or name.startswith("ks-")


# ── Fitness ─────────────────────────────────────────────────────────────
# Stride length without calibration: typical walking values (3 km/h ≈ 0.52 m, 5 km/h ≈ 0.70 m)
STRIDE_BASE, STRIDE_SLOPE = 0.25, 0.09
STRIDE_MIN, STRIDE_MAX = 0.20, 1.20
MAX_CALIBRATION_POINTS = 3

# MET values for level walking (Compendium of Physical Activities), speed in km/h
MET_TABLE = [(1.0, 2.0), (3.2, 2.8), (4.0, 3.0), (4.8, 3.5), (5.6, 4.3), (6.4, 5.0)]


def interpolate(points, x, extrapolate=False):
    """Piecewise linear over sorted (x, y) points; outside: hold the edge value or extend its slope."""
    if x <= points[0][0] or x >= points[-1][0]:
        edge, (x0, y0), (x1, y1) = ((points[0], points[0], points[1]) if x <= points[0][0]
                                    else (points[-1], points[-2], points[-1]))
        return edge[1] + (x - edge[0]) * (y1 - y0) / (x1 - x0) if extrapolate else edge[1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return points[-1][1]


def stride_m(speed: float, calibration: list) -> float:
    """Stride length in metres for a speed, from up to 3 calibration points."""
    points = sorted({round(p["speed"], 1): p["stride_m"] for p in calibration}.items())
    if not points:
        stride = STRIDE_BASE + STRIDE_SLOPE * speed
    elif len(points) == 1:
        stride = points[0][1] + STRIDE_SLOPE * (speed - points[0][0])  # typical slope through the one point
    else:
        stride = interpolate(points, speed, extrapolate=True)
    return min(STRIDE_MAX, max(STRIDE_MIN, stride))


def cadence(speed: float, stride: float) -> float:
    """Steps per minute."""
    return speed * 1000 / 60 / stride if speed > 0 else 0.0


def met(speed: float) -> float:
    return interpolate(MET_TABLE, speed) if speed > 0 else 0.0


def kcal(speed: float, weight_kg: float, seconds: float) -> float:
    """Calories = MET × body weight in kg × hours."""
    return met(speed) * weight_kg * seconds / 3600


def calibration_point(speed: float, steps: int, seconds: float, ts: float) -> dict:
    meters = speed / 3.6 * seconds
    return {"speed": round(speed, 1), "steps": int(steps), "seconds": seconds,
            "stride_m": round(meters / steps, 4), "ts": ts}


def add_calibration(points: list, new: dict) -> list:
    """Same speed replaces the old point; at most 3 points, the oldest one is dropped."""
    points = [p for p in points if round(p["speed"], 1) != new["speed"]] + [new]
    points.sort(key=lambda p: p["ts"])
    return sorted(points[-MAX_CALIBRATION_POINTS:], key=lambda p: p["speed"])


# ── Store: data.json ────────────────────────────────────────────────────
DEFAULT_PROGRAMS = [
    {"id": "warmup", "name": "Easy warm-up",
     "segments": [{"speed": 2.0, "seconds": 180}, {"speed": 3.0, "seconds": 300}, {"speed": 3.5, "seconds": 600},
                  {"speed": 2.5, "seconds": 120}]},
    {"id": "intervals", "name": "Intervals 4 × 3 min",
     "segments": [{"speed": 3.0, "seconds": 180}, {"speed": 5.0, "seconds": 180}, {"speed": 3.0, "seconds": 120},
                  {"speed": 5.0, "seconds": 180}, {"speed": 3.0, "seconds": 120}, {"speed": 5.0, "seconds": 180},
                  {"speed": 3.0, "seconds": 120}, {"speed": 5.0, "seconds": 180}, {"speed": 2.5, "seconds": 180}]},
    {"id": "desk", "name": "Desk hour",
     "segments": [{"speed": 2.5, "seconds": 1500}, {"speed": 4.0, "seconds": 300}, {"speed": 2.5, "seconds": 1500},
                  {"speed": 4.0, "seconds": 300}]},
]

DEFAULT_SETTINGS = {
    "address": None,             # last Bluetooth address found (stays local in data.json)
    "auto_reconnect": True,
    "apply_on_connect": False,   # send the stored belt preferences after every connect
    "restore_speed": True,       # after start, return to the last speed
    "keep_running": False,       # restart the belt if it stops by itself in manual mode
    "prefs": {"inteli": None, "sensitivity": None, "child_lock": None, "start_speed": None, "max_speed": None},
    "weight_kg": 75.0,
    "goal": {"type": "steps", "value": 8000},
    "calibration": [],
    "meeting_speed": 1.5,
    "reminder": {"enabled": True, "minutes": 60},
    "belt_km": 0.0,
    "oil_interval_km": 200.0,
    "oil_last_km": 0.0,
    "programs": DEFAULT_PROGRAMS,
}
CONFIG_KEYS = ("auto_reconnect", "apply_on_connect", "restore_speed", "keep_running")


def merge(defaults: dict, stored: dict) -> dict:
    """Stored values over defaults; unknown keys are dropped."""
    out = copy.deepcopy(defaults)
    for key, value in stored.items():
        if key in out:
            out[key] = merge(out[key], value) if isinstance(out[key], dict) and isinstance(value, dict) else value
    return out


class Store:
    """Settings and sessions in one JSON file. Writes go to a temp file that is then renamed."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.sessions: list = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.settings = merge(DEFAULT_SETTINGS, data.get("settings", {}))
            self.sessions = data.get("sessions", [])
        self.next_id = max((s["id"] for s in self.sessions), default=0) + 1

    def save(self):
        with self.lock:
            text = json.dumps({"version": 1, "settings": self.settings, "sessions": self.sessions},
                              ensure_ascii=False, separators=(",", ":"))
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)

    def update_settings(self, **values):
        with self.lock:
            self.settings.update(values)
            self.save()

    def setting(self, key):
        with self.lock:
            return copy.deepcopy(self.settings[key])


# ── Sessions ────────────────────────────────────────────────────────────
SESSION_END_AFTER = 120  # s standstill until a session ends
SAMPLE_EVERY = 5         # s between two history points
MAX_GAP = 1800           # s without connection after which an open session is closed
MIN_ACTIVE = 20          # s; shorter sessions without distance are dropped
SAVE_EVERY = 30          # s between writes of data.json while walking

SUMMARY_FIELDS = ("id", "day", "start_ts", "end_ts", "active_s", "distance_km", "belt_steps", "own_steps",
                  "calories", "max_speed")


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def goal_metric(totals: dict, goal: dict) -> float:
    return totals.get("km", 0) if goal["type"] == "km" else totals.get("steps", 0)


def streaks(days: dict, goal: dict, today: date) -> dict:
    """Days in a row with the goal reached: current (up to today or yesterday) and best."""
    reached = {d for d, t in days.items() if goal["value"] > 0 and goal_metric(t, goal) >= goal["value"]}
    best = run = 0
    prev = None
    for d in sorted(reached):
        cur = date.fromisoformat(d)
        run = run + 1 if prev and cur - prev == timedelta(days=1) else 1
        best = max(best, run)
        prev = cur
    current = 0
    day = today if today.isoformat() in reached else today - timedelta(days=1)
    while day.isoformat() in reached:
        current += 1
        day -= timedelta(days=1)
    return {"current": current, "best": best, "today_reached": today.isoformat() in reached}


class SessionTracker:
    """A session starts when the belt starts moving and ends after 2 minutes of standstill.

    If the Bluetooth link drops, the session stays open and continues after reconnecting
    (the belt keeps counting). It is closed after 30 minutes without a connection.
    """

    def __init__(self, store: Store, end_after=SESSION_END_AFTER):
        self.store = store
        self.end_after = end_after
        self.current = None
        self.stopped_since = None
        self.speed = 0.0
        self.cadence = 0.0
        self.stride = stride_m(3.0, store.settings["calibration"])
        self._last_running_speed = 0.0
        self._last_sample = 0.0
        self._last_save = time.time()
        self._dirty = False
        self._days_cache = None
        closed = [s for s in store.sessions if s.get("end_ts")]
        self.last_walk_ts = max((s["end_ts"] for s in closed), default=None)

        for s in store.sessions:
            if s.get("end_ts") is None:
                if time.time() - s["last_ts"] > MAX_GAP:
                    self._close(s)
                else:
                    self.current = s
                    self.stopped_since = s["last_ts"] if s["last_running_ts"] < s["last_ts"] else None
                    log.info("Resuming open session %s", s["id"])
        store.save()

    def device_baseline(self) -> dict:
        """Last belt counters, so nothing is counted twice after an app restart."""
        c = self.current
        return {"time": c["dev_time"], "dist": c["dev_dist"], "steps": c["dev_steps"]} if c else {}

    # data from the belt (BLE thread, under the worker lock)
    def update(self, speed, d_time, d_dist_km, d_steps, now, dev):
        settings = self.store.settings
        running = speed > 0
        self.speed = speed
        if running:
            self._last_running_speed = speed
        settings["belt_km"] = round(settings["belt_km"] + d_dist_km, 3)
        if d_dist_km:
            self._dirty = True

        if running and not self.current:
            self._start(now)
        cur = self.current
        if not cur:
            self.cadence = 0.0
            return

        # while slowing down the belt still reports distance at speed 0: use the last running speed
        calc_speed = speed or self._last_running_speed or 3.0
        self.stride = stride_m(calc_speed, settings["calibration"])
        self.cadence = cadence(speed, self.stride)

        cur["distance_km"] += d_dist_km
        cur["belt_steps"] += d_steps
        # own steps = distance / stride length; the belt reports distance in 10 m steps,
        # so the distance is taken from speed × running time (which is what the belt measures, too)
        cur["own_steps"] += calc_speed / 3.6 * d_time / self.stride
        cur["active_s"] += d_time
        cur["calories"] += kcal(calc_speed, float(settings["weight_kg"]), d_time)
        cur["max_speed"] = max(cur["max_speed"], speed)
        cur["last_ts"] = now
        cur["dev_time"], cur["dev_dist"], cur["dev_steps"] = dev["time"], dev["dist"], dev["steps"]
        if running:
            cur["last_running_ts"] = now
            self.stopped_since = None
            self.last_walk_ts = now
        elif self.stopped_since is None:
            self.stopped_since = now

        if now - self._last_sample >= SAMPLE_EVERY:
            self._last_sample = now
            cur["samples"].append([round(now - cur["start_ts"]), int(round(speed * 10)), round(self.cadence)])
            self._dirty = True

    def tick(self, now, connected: bool):
        cur = self.current
        if cur:
            if connected and self.stopped_since and now - self.stopped_since >= self.end_after:
                self.finish()
            elif not connected and now - cur["last_ts"] >= MAX_GAP:
                log.info("Session %s: no connection for %d min, closing", cur["id"], MAX_GAP // 60)
                self.finish()
        if self._dirty and now - self._last_save >= SAVE_EVERY:
            self.flush()

    def flush(self):
        self._last_save = time.time()
        self._dirty = False
        self.store.save()

    def _start(self, now):
        store = self.store
        session = {"id": store.next_id, "day": day_of(now), "start_ts": now, "end_ts": None, "last_ts": now,
                   "last_running_ts": now, "active_s": 0, "distance_km": 0.0, "belt_steps": 0, "own_steps": 0.0,
                   "calories": 0.0, "max_speed": 0.0, "dev_time": 0, "dev_dist": 0, "dev_steps": 0, "samples": []}
        store.next_id += 1
        store.sessions.append(session)
        self.current = session
        self.stopped_since = None
        self._last_sample = 0.0
        log.info("Session %s started", session["id"])

    def finish(self):
        cur, self.current = self.current, None
        self.stopped_since = None
        self.cadence = 0.0
        if cur:
            self._close(cur)
        self._days_cache = None
        self.flush()

    def _close(self, s):
        if s["active_s"] < MIN_ACTIVE and s["distance_km"] < 0.01:
            self.store.sessions.remove(s)
            log.info("Session %s dropped (too short)", s["id"])
            return
        s["end_ts"] = s["last_running_ts"]
        offset = s["end_ts"] - s["start_ts"] + SAMPLE_EVERY
        s["samples"] = [p for p in s["samples"] if p[0] <= offset]
        log.info("Session %s ended: %.2f km, %d s", s["id"], s["distance_km"], s["active_s"])

    def delete(self, session_id: int) -> bool:
        if self.current and self.current["id"] == session_id:
            raise CommandError("err_session_running")
        before = len(self.store.sessions)
        self.store.sessions[:] = [s for s in self.store.sessions if s["id"] != session_id]
        self._days_cache = None
        self.store.save()
        return len(self.store.sessions) < before

    def invalidate(self):
        self._days_cache = None

    # evaluation
    def _closed_days(self) -> dict:
        now = time.time()
        if not self._days_cache or now - self._days_cache[0] > 300:
            days = {}
            for s in self.store.sessions:
                if s is not self.current:
                    self._add_day(days, s)
            self._days_cache = (now, days)
        return self._days_cache[1]

    @staticmethod
    def _add_day(days, s):
        t = days.setdefault(s["day"], {"day": s["day"], "km": 0.0, "steps": 0.0, "kcal": 0.0, "seconds": 0.0,
                                       "sessions": 0})
        t["km"] += s["distance_km"]
        t["steps"] += s["own_steps"]
        t["kcal"] += s["calories"]
        t["seconds"] += s["active_s"]
        t["sessions"] += 1

    def days(self) -> dict:
        days = {d: dict(v) for d, v in self._closed_days().items()}
        if self.current:
            self._add_day(days, self.current)
        return days

    def snapshot(self) -> dict:
        settings = self.store.settings
        now = time.time()
        goal = settings["goal"]
        days = self.days()
        today = date.today()
        t = days.get(today.isoformat(), {})
        value = goal_metric(t, goal)
        session = None
        if self.current:
            c = self.current
            stopped_for = now - self.stopped_since if self.stopped_since else 0
            session = {
                "id": c["id"], "start_ts": c["start_ts"], "active_s": round(c["active_s"]),
                "distance_km": round(c["distance_km"], 3), "belt_steps": c["belt_steps"],
                "own_steps": round(c["own_steps"]), "calories": round(c["calories"], 1),
                "max_speed": c["max_speed"],
                "ends_in": max(0, round(self.end_after - stopped_for)) if self.stopped_since else None,
            }
        belt_km = settings["belt_km"]
        oil_interval = float(settings["oil_interval_km"])
        since_oil = belt_km - float(settings["oil_last_km"])
        return {
            "session": session,
            "cadence": round(self.cadence),
            "stride_m": round(self.stride, 3),
            "today": {"km": round(t.get("km", 0), 3), "steps": round(t.get("steps", 0)),
                      "kcal": round(t.get("kcal", 0)), "seconds": round(t.get("seconds", 0)),
                      "sessions": t.get("sessions", 0)},
            "goal": {**goal, "current": round(value, 2),
                     "progress": round(min(1.0, value / goal["value"]), 4) if goal["value"] else 0},
            "streak": streaks(days, goal, today),
            "last_walk_ts": self.last_walk_ts,
            "maintenance": {"belt_km": round(belt_km, 2), "oil_interval_km": oil_interval,
                            "since_oil_km": round(since_oil, 2), "oil_due": oil_interval > 0 and since_oil >= oil_interval},
        }

    def history(self) -> dict:
        days = self.days()
        since = (date.today() - timedelta(days=371)).isoformat()
        sessions = sorted(self.store.sessions, key=lambda s: s["start_ts"], reverse=True)[:200]
        closed = [s for s in self.store.sessions if s.get("end_ts")]
        longest = max(closed, key=lambda s: s["active_s"], default=None)
        best_day = max(days.values(), key=lambda d: d["km"], default=None)
        all_s = self.store.sessions
        return {
            "days": [{"day": d, "km": round(v["km"], 3), "steps": round(v["steps"]), "kcal": round(v["kcal"]),
                      "seconds": round(v["seconds"]), "sessions": v["sessions"]}
                     for d, v in sorted(days.items()) if d >= since],
            "sessions": [{k: s[k] for k in SUMMARY_FIELDS} for s in sessions],
            "records": {
                "longest_session": longest and {k: longest[k] for k in ("start_ts", "active_s", "distance_km")},
                "best_day": best_day and {"day": best_day["day"], "km": round(best_day["km"], 2),
                                          "steps": round(best_day["steps"])},
                "streak": streaks(days, self.store.settings["goal"], date.today()),
                "totals": {"sessions": len(all_s), "days": len(days),
                           "km": round(sum(s["distance_km"] for s in all_s), 2),
                           "steps": round(sum(s["own_steps"] for s in all_s)),
                           "seconds": round(sum(s["active_s"] for s in all_s)),
                           "kcal": round(sum(s["calories"] for s in all_s)),
                           "max_speed": max((s["max_speed"] for s in all_s), default=0)},
            },
        }

    def session_detail(self, session_id) -> dict | None:
        if session_id == "current":
            s = self.current
        else:
            s = next((x for x in self.store.sessions if str(x["id"]) == str(session_id)), None)
        if not s:
            return None
        return {**{k: s[k] for k in SUMMARY_FIELDS},
                "samples": [{"ts": s["start_ts"] + p[0], "speed": p[1] / 10, "cadence": p[2]} for p in s["samples"]]}


# ── Links: real Bluetooth and simulation ────────────────────────────────
class BleLink:
    """Bluetooth link via bleak. A connection counts only when fe01 and fe02 exist
    and notifications on fe01 are running (checked by the worker with a first status reply)."""

    def __init__(self, on_data, on_disconnect):
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.client = None
        self.name = None
        self.address = None

    async def find(self, address=None):
        from bleak import BleakScanner  # imported here so tests and --sim run without bleak
        if address:
            device = await BleakScanner.find_device_by_address(address, timeout=8)
            if device:
                return device
        found = await BleakScanner.discover(timeout=10, return_adv=True)
        for device, adv in found.values():
            name = device.name or adv.local_name
            if not name:
                continue  # skip devices without a name
            if is_walkingpad_name(name):
                return device
        return None

    async def connect(self, device):
        from bleak import BleakClient
        self.name, self.address = device.name, device.address
        self.client = BleakClient(device, disconnected_callback=lambda _c: self.on_disconnect(), timeout=20.0)
        await self.client.connect()
        uuids = {c.uuid.lower() for service in self.client.services for c in service.characteristics}
        if FE01 not in uuids or FE02 not in uuids:
            raise ConnectionError("characteristics fe01/fe02 not found")
        await self.client.start_notify(FE01, lambda _sender, data: self.on_data(bytes(data)))

    async def write(self, data: bytes):
        await self.client.write_gatt_char(FE02, data)

    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()


class SimBelt:
    """The simulated belt itself. It keeps running when the link drops, like the real one."""

    COUNTDOWN = 3.0

    def __init__(self):
        self.mode = MODE_STANDBY
        self.speed = self.target = 0.0
        self.start_at = None
        self.time = self.dist_m = self.steps = 0.0
        self.prefs = {}
        self._task = None

    def ensure_running(self):
        if not self._task:
            self._task = asyncio.ensure_future(self._physics())

    async def _physics(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            dt, last = now - last, now
            self.step(dt, now)

    def step(self, dt, now):
        if self.start_at and now >= self.start_at:
            self.start_at = None
            self.target = self.prefs.get(PREF_START_SPEED, 20) / 10
        if self.speed < self.target:
            self.speed = min(self.target, self.speed + 1.0 * dt)
        elif self.speed > self.target:
            self.speed = max(self.target, self.speed - 2.0 * dt)
        if self.speed > 0:
            self.time += dt
            self.dist_m += self.speed / 3.6 * dt
            self.steps += self.speed / 3.6 / 0.6 * dt

    def handle(self, data: bytes) -> bytes | None:
        """Apply a command frame; returns a status frame for status requests."""
        if len(data) < 4 or data[0] != 0xF7 or data[-1] != 0xFD or sum(data[1:-2]) % 256 != data[-2]:
            return None
        kind, sub = data[1], data[2]
        if kind == 0xA2 and sub == 0x00:
            return self.status_frame()
        if kind == 0xA2 and sub == 0x01:
            self.target = data[3] / 10
            if data[3] == 0:
                self.start_at = None
        elif kind == 0xA2 and sub == 0x02:
            self.mode = data[3]
            if self.mode == MODE_STANDBY:
                self.target = 0
        elif kind == 0xA2 and sub == 0x04:
            if self.speed == 0 and not self.start_at:
                self.time = self.dist_m = self.steps = 0  # the belt resets its counters on start
                self.start_at = time.monotonic() + self.COUNTDOWN
                self.target = 0
        elif kind == 0xA6:
            self.prefs[sub] = int.from_bytes(data[4:7], "big")
        return None

    def status_frame(self) -> bytes:
        state = 9 if self.start_at else (1 if self.speed > 0 else 0)
        body = bytes([0xF8, 0xA2, state, int(round(self.speed * 10)), self.mode])
        body += int(self.time).to_bytes(3, "big") + int(self.dist_m / 10).to_bytes(3, "big")
        body += int(self.steps).to_bytes(3, "big") + bytes([0, 0, 0, 0])
        return body + bytes([sum(body[1:]) % 256, 0xFD])


class SimLink:
    belt = SimBelt()

    def __init__(self, on_data, on_disconnect):
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = False
        self.name, self.address = "WalkingPad (simulated)", "SIM"

    async def find(self, address=None):
        await asyncio.sleep(0.3)
        return self

    async def connect(self, device):
        await asyncio.sleep(0.5)
        self.connected = True
        self.belt.ensure_running()

    async def write(self, data: bytes):
        if not self.connected:
            raise ConnectionError("simulated link closed")
        await asyncio.sleep(0.05)
        reply = self.belt.handle(data)
        if reply:
            asyncio.get_running_loop().call_later(0.08, lambda: self.connected and self.on_data(reply))

    async def disconnect(self):
        if self.connected:
            self.connected = False
            self.on_disconnect()


# ── Worker: Bluetooth thread ────────────────────────────────────────────
CMD_SPACING = 0.7        # s between two commands
POLL_INTERVAL = 1.0      # s between status requests
DEAD_AFTER = 5.0         # s without a status reply = connection lost
FIRST_REPLY_TIMEOUT = 5.0
RECONNECT_DELAYS = [2, 5, 10, 30]
MAX_AUTO_RESTARTS = 3    # "keep running": at most this many restarts ...
AUTO_RESTART_WINDOW = 60  # ... within this many seconds
BELT_COMMANDS = {"start", "stop", "speed", "mode", "pref", "apply_prefs", "meeting", "pause", "program_start"}


class CommandError(Exception):
    """Error with a message key that the UI translates."""

    def __init__(self, key, **args):
        super().__init__(key)
        self.key, self.params = key, args


def prefs_frames(name, value):
    """Validate a belt preference and build its command frame."""
    if name in ("inteli", "child_lock"):
        if not isinstance(value, bool):
            raise CommandError("err_invalid_value")
        return value, cmd_set_pref(PREF_START_INTEL if name == "inteli" else PREF_CHILD_LOCK, int(value))
    if name == "sensitivity":
        if value not in (1, 2, 3):
            raise CommandError("err_invalid_value")
        return value, cmd_set_pref(PREF_SENSITIVITY, value)
    if name in ("start_speed", "max_speed"):
        value = clamp_speed(value)
        return value, cmd_set_pref(PREF_START_SPEED if name == "start_speed" else PREF_MAX_SPEED, round(value * 10))
    raise CommandError("err_unknown_setting")


def merge_speed_commands(items: list) -> list:
    """Queued speed commands: only the last one counts. Returns the items to run."""
    last = max((i for i, it in enumerate(items) if it[0] == "speed"), default=None)
    keep = []
    for i, it in enumerate(items):
        if it[0] == "speed" and i != last:
            if not it[2].done():
                it[2].set_result({"key": "msg_speed_replaced"})
            continue
        keep.append(it)
    return keep


class Worker:
    """Owns the Bluetooth link in its own thread with its own asyncio loop.

    HTTP threads hand commands over with submit() (thread-safe via call_soon_threadsafe).
    A dispatcher runs app commands at once and queues belt commands, which the link loop
    sends one by one with at least CMD_SPACING in between.
    """

    def __init__(self, store: Store, tracker: SessionTracker, sim=False, address=None):
        self.store = store
        self.tracker = tracker
        self.sim = sim
        self.address_override = address
        self.lock = store.lock  # one lock for state, sessions and data.json
        self.loop = None
        self.queue = None
        self.belt_queue = None
        self.ready = threading.Event()
        self.link = None
        self.lost = None
        self.want_connected = True
        self.attempt = 0
        self.retry_at = None
        self._wake = None
        self._conn_task = None
        self._last_cmd = 0.0
        self._last_poll = 0.0
        self._poll_sent = None
        self._expect_stop_until = 0.0
        self._prev_speed = 0.0
        self._stable_speed = None
        self._commanded = (None, 0.0)
        self._restarts = deque()
        self._last_dev = {"time": 0, "dist": 0, "steps": 0, **tracker.device_baseline()}
        self._last_tick = time.monotonic()
        self.meeting_restore = None
        self.paused_speed = None
        self.program = None
        self.events = deque(maxlen=30)
        self.stats = {"connects": 0, "drops": 0, "connected_since": None, "latency_ms": None, "packets": 0}
        self.state = {"conn": "disconnected", "message": {"key": ""}, "device": None, "belt": None,
                      "last_status": None, "last_speed": 2.0}

    # thread and queues
    def start(self):
        threading.Thread(target=self._thread_main, name="bluetooth", daemon=True).start()
        self.ready.wait(5)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.belt_queue = asyncio.Queue()
        self._wake = asyncio.Event()
        self.ready.set()
        self.loop.run_until_complete(self._main())

    def submit(self, name, args) -> concurrent.futures.Future:
        fut = concurrent.futures.Future()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (name, args, fut))
        return fut

    def call(self, name, args, timeout=20) -> dict:
        try:
            return self.submit(name, args).result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise CommandError("err_timeout")

    async def _dispatcher(self):
        while True:
            name, args, fut = await self.queue.get()
            if name in BELT_COMMANDS and not (name == "speed" and not self.link):
                if not self._connected():
                    fut.set_exception(CommandError("err_not_connected"))
                else:
                    self.belt_queue.put_nowait((name, args, fut))
                continue
            await self._run(name, args, fut)

    async def _run(self, name, args, fut):
        handler = getattr(self, f"_cmd_{name}", None)
        try:
            if handler is None:
                raise CommandError("err_unknown_command", name=name)
            try:
                inspect.signature(handler).bind(**args)
            except TypeError:
                raise CommandError("err_bad_arguments", name=name)
            result = await handler(**args)
            if not fut.done():
                fut.set_result(result or {"key": "msg_ok"})
        except CommandError as exc:
            if not fut.done():
                fut.set_exception(exc)
        except Exception as exc:
            log.exception("Command %s failed", name)
            if not fut.done():
                fut.set_exception(CommandError("err_bluetooth", detail=str(exc)))

    # state
    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _message(self, key, **args):
        self._set(message={"key": key, "args": args})

    def event(self, key, level=logging.INFO, **args):
        log.log(level, "%s%s", key, "".join(f" {k}={v}" for k, v in args.items()))
        with self.lock:
            self.events.appendleft({"t": time.time(), "key": key, "args": args,
                                    "level": logging.getLevelName(level).lower()})

    def _connected(self):
        return self.link is not None and self.state["conn"] == "connected"

    def _belt(self) -> dict:
        return self.state.get("belt") or {}

    def running(self) -> bool:
        return bool(self._belt().get("running"))

    def snapshot(self) -> dict:
        with self.lock:
            s = copy.deepcopy(self.state)
            events = list(self.events)
            stats = dict(self.stats)
            program = self._program_state()
            settings = copy.deepcopy(self.store.settings)
        now = time.time()
        age = now - s["last_status"] if s["last_status"] else None
        settings.pop("address", None)
        return {
            **s,
            "simulation": self.sim,
            "connected": s["conn"] == "connected",
            "want_connected": self.want_connected,
            "attempt": self.attempt,
            "retry_in": max(0, round(self.retry_at - now)) if self.retry_at else None,
            "status_age": round(age, 1) if age is not None else None,
            "stale": age is None or age > 3,
            "link": stats,
            "settings": settings,
            "limits": {"min": MIN_SPEED, "max": MAX_SPEED},
            "meeting": {"active": self.meeting_restore is not None, "restore_speed": self.meeting_restore},
            "paused": {"active": self.paused_speed is not None, "speed": self.paused_speed},
            "program": program,
            "events": events,
        }

    # main loop
    async def _main(self):
        asyncio.ensure_future(self._dispatcher())
        asyncio.ensure_future(self._ticker())
        while True:
            try:
                if not self.want_connected:
                    self._set(conn="disconnected")
                    self._message("msg_disconnected")
                    await self._idle(None)
                    continue
                self._conn_task = asyncio.ensure_future(self._connect())
                try:
                    ok = await self._conn_task
                except asyncio.CancelledError:
                    ok = False
                finally:
                    self._conn_task = None
                if ok:
                    self.attempt = 0
                    await self._link_loop()
                    await self._cleanup()
                    if self.want_connected and not self.store.settings["auto_reconnect"]:
                        self.want_connected = False
                        self.event("ev_lost_no_reconnect", logging.WARNING)
                    continue
                if self.want_connected:
                    delay = RECONNECT_DELAYS[min(self.attempt, len(RECONNECT_DELAYS) - 1)]
                    self.attempt += 1
                    self.retry_at = time.time() + delay
                    self._set(conn="waiting")
                    await self._idle(delay)
                    self.retry_at = None
            except Exception as exc:  # this loop must never die
                log.exception("Error in the Bluetooth loop")
                self.event("ev_internal_error", logging.ERROR, detail=str(exc))
                await self._cleanup()
                await asyncio.sleep(2)

    async def _idle(self, seconds):
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _ticker(self):
        while True:
            await asyncio.sleep(1)
            try:
                now = time.monotonic()
                dt, self._last_tick = now - self._last_tick, now
                with self.lock:
                    self._program_tick(dt)
                    self.tracker.tick(time.time(), connected=self._connected())
            except Exception:
                log.exception("Error in the ticker")

    async def _link_loop(self):
        while self.want_connected and not self.lost.is_set():
            timeout = max(0.05, self._last_poll + POLL_INTERVAL - time.monotonic())
            try:
                item = await asyncio.wait_for(self.belt_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                item = None
            if item:
                items = [item]
                while not self.belt_queue.empty():
                    items.append(self.belt_queue.get_nowait())
                for cmd in merge_speed_commands(items):
                    await self._run(*cmd)
                    if self.lost.is_set() or not self.want_connected:
                        break
            if self.lost.is_set() or not self.want_connected:
                break
            if time.monotonic() - self._last_poll >= POLL_INTERVAL:
                await self._poll()
            last = self.state["last_status"]
            if last and time.time() - last > DEAD_AFTER:
                self.event("ev_no_reply", logging.WARNING)
                break

    async def _poll(self):
        self._last_poll = time.monotonic()
        try:
            self._poll_sent = time.monotonic()
            await self._send(cmd_ask_status())
        except Exception as exc:
            log.warning("Status request failed: %s", exc)

    async def _send(self, data: bytes):
        """Send one frame, at least CMD_SPACING after the previous one (the belt drops faster commands)."""
        if not self.link:
            raise CommandError("err_not_connected")
        wait = self._last_cmd + CMD_SPACING - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await asyncio.wait_for(self.link.write(data), timeout=5)
        finally:
            self._last_cmd = time.monotonic()

    def _new_link(self):
        cls = SimLink if self.sim else BleLink
        return cls(self._on_data, lambda: self.loop.call_soon_threadsafe(self.lost.set))

    async def _connect(self) -> bool:
        self.lost = asyncio.Event()
        link = self._new_link()
        address = self.address_override or self.store.settings["address"]
        self._set(conn="searching")
        self._message("msg_searching")
        try:
            device = await link.find(address)
        except Exception as exc:
            self._set(conn="error")
            self._message("err_scan_failed", detail=str(exc))
            self.event("err_scan_failed", logging.ERROR, detail=str(exc))
            return False
        if not device:
            self._set(conn="error")
            self._message("err_not_found")
            self.event("err_not_found", logging.WARNING)
            return False

        self._set(conn="connecting")
        self._message("msg_connecting", name=device.name or "WalkingPad")
        self.link = link
        try:
            await asyncio.wait_for(link.connect(device), timeout=45)
            # notifications on fe01 must run: wait for the first status reply. The belt ignores
            # the first request right after subscribing, so ask once per second (see PROTOCOL.md).
            sent = time.time()
            deadline = time.monotonic() + FIRST_REPLY_TIMEOUT
            while not (self.state["last_status"] or 0) > sent and time.monotonic() < deadline:
                await self._send(cmd_ask_status())
                await asyncio.sleep(0.3)
            if not (self.state["last_status"] or 0) > sent:
                raise ConnectionError("no status reply on fe01")
        except asyncio.CancelledError:
            self.link = None
            try:
                await asyncio.wait_for(asyncio.shield(link.disconnect()), timeout=5)
            except BaseException:
                pass
            raise
        except Exception as exc:
            self.link = None
            self._set(conn="error")
            self._message("err_connect_failed", detail=str(exc) or type(exc).__name__)
            self.event("err_connect_failed", logging.ERROR, detail=str(exc) or type(exc).__name__)
            try:
                await asyncio.wait_for(link.disconnect(), timeout=5)
            except Exception:
                pass
            return False

        if not self.sim and not self.address_override and self.store.settings["address"] != link.address:
            self.store.update_settings(address=link.address)
        with self.lock:
            self.stats["connects"] += 1
            self.stats["connected_since"] = time.time()
        self._set(conn="connected", device={"name": link.name})
        self._message("msg_connected")
        self.event("ev_connected", name=link.name or "WalkingPad")
        if self.tracker.current:
            self.event("ev_session_resumed")
        if self.store.settings["apply_on_connect"]:
            await self._apply_prefs()
        self._last_poll = 0.0
        return True

    async def _cleanup(self):
        link, self.link = self.link, None
        while not self.belt_queue.empty():
            _name, _args, fut = self.belt_queue.get_nowait()
            if not fut.done():
                fut.set_exception(CommandError("err_connection_lost"))
        if link:
            try:
                await asyncio.wait_for(link.disconnect(), timeout=5)
            except Exception as exc:
                log.debug("Disconnect: %s", exc)
        with self.lock:
            was_connected = self.state["conn"] == "connected"
            if was_connected:
                self.state["conn"] = "disconnected"
                self.stats["connected_since"] = None
                if self.want_connected:
                    self.stats["drops"] += 1
        self._prev_speed = 0.0
        if was_connected and self.want_connected:
            self.event("ev_connection_lost", logging.WARNING)

    # status from the belt (runs in the Bluetooth thread)
    def _on_data(self, data: bytes):
        st = parse_status(data)
        if st:
            self._on_status(st)

    def _on_status(self, st: dict):
        now = time.time()
        speed = st["speed"]
        mode = MODE_NAMES.get(st["mode"], "unknown")
        with self.lock:
            deltas = {}
            for key in ("time", "dist", "steps"):
                new, last = st[key], self._last_dev[key]
                deltas[key] = new if new < last else new - last  # counter on the belt was reset
                self._last_dev[key] = new
            self.stats["packets"] += 1
            if self._poll_sent is not None:
                self.stats["latency_ms"] = round((time.monotonic() - self._poll_sent) * 1000)
                self._poll_sent = None
            self.state["belt"] = {"state": st["state"], "running": speed > 0, "speed": speed, "mode": mode,
                                  "dev_time": st["time"], "dev_dist": st["dist"] / 100, "dev_steps": st["steps"]}
            self.state["last_status"] = now
            if speed > 0 and speed == self._prev_speed:  # two equal readings: not slowing down
                self._stable_speed = speed
                self.state["last_speed"] = speed
            self.tracker.update(speed, deltas["time"], deltas["dist"] / 100, deltas["steps"], now, dict(self._last_dev))
            if self._prev_speed == 0 and speed > 0 and self.program:
                self.program["need_speed"] = True  # back to the segment speed after a stop
        stopped_by_itself = (self._prev_speed > 0 and speed == 0 and mode != "auto"
                             and now > self._expect_stop_until)
        self._prev_speed = speed
        if stopped_by_itself:
            self.event("ev_stopped_by_itself", logging.WARNING)
            if self.store.settings["keep_running"]:
                self._schedule_restart()

    def _schedule_restart(self):
        now = time.time()
        while self._restarts and now - self._restarts[0] > AUTO_RESTART_WINDOW:
            self._restarts.popleft()
        if len(self._restarts) >= MAX_AUTO_RESTARTS:
            self.event("ev_restart_limit", logging.WARNING, count=MAX_AUTO_RESTARTS)
            return
        self._restarts.append(now)
        self.event("ev_restarting")
        self.belt_queue.put_nowait(("start", {}, concurrent.futures.Future()))

    # belt helpers
    def _require_connected(self):
        if not self._connected():
            raise CommandError("err_not_connected")

    def _current_speed(self) -> float:
        """Intended walking speed: right after a command the sent one (the belt is still ramping)."""
        speed, at = self._commanded
        if speed and time.monotonic() - at < 15:
            return speed
        return self._stable_speed or self._belt().get("speed") or self.state["last_speed"]

    async def _set_speed(self, speed):
        await self._send(cmd_set_speed(speed))
        self._commanded = (speed, time.monotonic())

    async def _poll_until(self, done, seconds) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if done():
                return True
            await asyncio.sleep(0.5)
            await self._send(cmd_ask_status())
        return done()

    async def _start_belt(self, speed=None) -> dict:
        self._require_connected()
        belt = self._belt
        if belt().get("running") and time.time() < self._expect_stop_until:
            # still running out after a stop: wait for standstill, or the start is ignored
            await self._poll_until(lambda: not belt().get("running"), 6)
        if belt().get("running"):
            if speed:
                await self._set_speed(speed)
            return {"key": "msg_already_running"}
        if belt().get("mode") == "standby":
            await self._send(cmd_set_mode(MODE_MANUAL))
        sent = time.time()
        await self._send(cmd_start())
        self._expect_stop_until = 0
        self.event("ev_start")
        if not speed:
            return {"key": "msg_starting"}
        # wait for the belt's countdown (about 3 s): only a fresh status after the start counts
        started = await self._poll_until(
            lambda: self.state["last_status"] > sent + 0.3 and belt().get("running") and belt().get("state") != 9, 10)
        if not started:
            return {"key": "msg_starting"}
        await self._set_speed(speed)
        return {"key": "msg_running_at", "args": {"speed": speed}}

    async def _stop_belt(self):
        self._require_connected()
        self._expect_stop_until = time.time() + 15
        if self.running():
            self._set(last_speed=self._current_speed())
        await self._send(cmd_set_speed(0))

    async def _apply_prefs(self):
        sent = 0
        for name, value in self.store.settings["prefs"].items():
            if value is None:
                continue
            try:
                await self._send(prefs_frames(name, value)[1])
                sent += 1
            except Exception as exc:
                log.warning("Preference %s not sent: %s", name, exc)
        if sent:
            self.event("ev_prefs_sent", count=sent)

    # commands
    async def _cmd_connect(self):
        self.want_connected = True
        self.attempt = 0
        self._wake.set()
        return {"key": "msg_connecting", "args": {"name": "WalkingPad"}}

    async def _cmd_disconnect(self):
        self.want_connected = False
        self._wake.set()
        if self._conn_task:
            self._conn_task.cancel()
        self.event("ev_disconnected_by_user")
        return {"key": "msg_disconnected"}

    async def _cmd_forget_device(self):
        self.store.update_settings(address=None)
        return {"key": "msg_device_forgotten"}

    async def _cmd_start(self):
        speed = self.state["last_speed"] if self.store.settings["restore_speed"] else None
        return await self._start_belt(speed)

    async def _cmd_stop(self):
        self.meeting_restore = self.paused_speed = None
        await self._stop_belt()
        self.event("ev_stop")
        return {"key": "msg_stopping"}

    async def _cmd_speed(self, speed):
        speed = clamp_speed(speed)
        self._set(last_speed=speed)
        if not self.link or not self.running():
            return {"key": "msg_speed_on_start", "args": {"speed": speed}}
        await self._set_speed(speed)
        return {"key": "msg_speed", "args": {"speed": speed}}

    async def _cmd_mode(self, mode):
        self._require_connected()
        if mode not in MODE_BY_NAME:
            raise CommandError("err_invalid_value")
        if mode == "standby":
            self._expect_stop_until = time.time() + 15
        await self._send(cmd_set_mode(MODE_BY_NAME[mode]))
        self.event("ev_mode", mode=mode)
        return {"key": "msg_mode_changed"}

    async def _cmd_pref(self, name, value):
        value, data = prefs_frames(name, value)
        self._require_connected()
        await self._send(data)
        with self.lock:
            self.store.settings["prefs"][name] = value
            self.store.save()
        self.event("ev_pref", name=name, value=value)
        return {"key": "msg_pref_sent"}

    async def _cmd_apply_prefs(self):
        self._require_connected()
        await self._apply_prefs()
        return {"key": "msg_prefs_sent"}

    async def _cmd_meeting(self):
        """First press: meeting speed. Second press: back to the previous speed."""
        if self.meeting_restore is not None:
            speed, self.meeting_restore = self.meeting_restore, None
            if self.running():
                await self._set_speed(speed)
            self._set(last_speed=speed)
            self.event("ev_meeting_off", speed=speed)
            return {"key": "msg_back_to", "args": {"speed": speed}}
        if not self.running():
            raise CommandError("err_not_running")
        meeting = clamp_speed(self.store.settings["meeting_speed"])
        self.meeting_restore = self._current_speed()
        await self._set_speed(meeting)
        self.event("ev_meeting_on", speed=meeting)
        return {"key": "msg_meeting", "args": {"speed": meeting}}

    async def _cmd_pause(self):
        """First press: stop and remember the speed. Second press: continue at that speed."""
        if self.paused_speed is not None:
            speed, self.paused_speed = self.paused_speed, None
            self._set(last_speed=speed)
            return await self._start_belt(speed)
        if not self.running():
            raise CommandError("err_not_running")
        self.paused_speed = self.meeting_restore or self._current_speed()
        self.meeting_restore = None
        await self._stop_belt()
        self.event("ev_pause", speed=self.paused_speed)
        return {"key": "msg_paused"}

    async def _cmd_calibrate(self, speed, steps, seconds=60):
        try:
            speed, steps, seconds = float(speed), int(steps), float(seconds)
        except (TypeError, ValueError):
            raise CommandError("err_invalid_value")
        if not (MIN_SPEED <= speed <= MAX_SPEED and 10 <= steps <= 1000 and 20 <= seconds <= 300):
            raise CommandError("err_invalid_value")
        point = calibration_point(speed, steps, seconds, time.time())
        with self.lock:
            self.store.update_settings(calibration=add_calibration(self.store.settings["calibration"], point))
        log.info("Calibration: %.1f km/h -> %.1f cm stride", point["speed"], point["stride_m"] * 100)
        return {"key": "msg_calibrated", "args": {"stride_m": point["stride_m"]}}

    async def _cmd_delete_calibration(self, index):
        with self.lock:
            points = self.store.settings["calibration"]
            if not isinstance(index, int) or not 0 <= index < len(points):
                raise CommandError("err_invalid_value")
            points.pop(index)
            self.store.save()
        return {"key": "msg_saved"}

    async def _cmd_delete_session(self, id):
        with self.lock:
            ok = self.tracker.delete(id)
        if not ok:
            raise CommandError("err_not_found_session")
        return {"key": "msg_session_deleted"}

    async def _cmd_oiled(self):
        with self.lock:
            self.store.update_settings(oil_last_km=self.store.settings["belt_km"])
        return {"key": "msg_oiled"}

    # interval programs
    def _program_state(self):
        p = self.program
        if not p:
            return None
        segs = p["segments"]
        seg = segs[p["index"]]
        seg_remaining = max(0, seg["seconds"] - p["seg_elapsed"])
        return {"id": p["id"], "name": p["name"], "index": p["index"], "count": len(segs),
                "seg_speed": seg["speed"], "seg_seconds": seg["seconds"], "seg_remaining": round(seg_remaining),
                "total_remaining": round(seg_remaining + sum(s["seconds"] for s in segs[p["index"] + 1:])),
                "segments": segs}

    def _queue_speed(self, speed):
        if self._connected():
            self.belt_queue.put_nowait(("speed", {"speed": speed}, concurrent.futures.Future()))

    def _program_tick(self, dt):
        """Program time runs only while the belt runs and no meeting is active."""
        p = self.program
        if not p or not self.running() or self.meeting_restore is not None or not self._connected():
            return
        segs = p["segments"]
        if p["need_speed"]:
            p["need_speed"] = False
            self._queue_speed(segs[p["index"]]["speed"])
        p["seg_elapsed"] += dt
        if p["seg_elapsed"] < segs[p["index"]]["seconds"]:
            return
        p["index"] += 1
        p["seg_elapsed"] = 0.0
        if p["index"] >= len(segs):
            self.program = None
            self.event("ev_program_done", name=p["name"])
            self.belt_queue.put_nowait(("stop", {}, concurrent.futures.Future()))
            return
        self.event("ev_segment", index=p["index"] + 1, count=len(segs), speed=segs[p["index"]]["speed"])
        self._queue_speed(segs[p["index"]]["speed"])

    async def _cmd_program_start(self, id):
        prog = next((p for p in self.store.settings["programs"] if p["id"] == id), None)
        if not prog:
            raise CommandError("err_program_not_found")
        self.meeting_restore = self.paused_speed = None
        with self.lock:
            self.program = {"id": prog["id"], "name": prog["name"], "segments": prog["segments"],
                            "index": 0, "seg_elapsed": 0.0, "need_speed": False}
        first = prog["segments"][0]["speed"]
        self.event("ev_program_started", name=prog["name"])
        if self.running():
            await self._set_speed(first)
        else:
            await self._start_belt(first)
        return {"key": "msg_program_running", "args": {"name": prog["name"]}}

    async def _cmd_program_stop(self):
        if not self.program:
            raise CommandError("err_no_program")
        with self.lock:
            name, self.program = self.program["name"], None
        self.event("ev_program_cancelled", name=name)
        return {"key": "msg_program_stopped"}


# ── Settings from the UI ────────────────────────────────────────────────
def number(data, key, lo, hi):
    try:
        value = float(data[key])
    except (KeyError, TypeError, ValueError):
        raise CommandError("err_invalid_value", field=key)
    if not lo <= value <= hi:
        raise CommandError("err_out_of_range", field=key, min=lo, max=hi)
    return value


def clean_programs(raw) -> list:
    if not isinstance(raw, list) or len(raw) > 30:
        raise CommandError("err_invalid_value", field="programs")
    programs, ids = [], set()
    for i, prog in enumerate(raw):
        name = str(prog.get("name") or "").strip()[:40]
        if not name:
            raise CommandError("err_program_name")
        pid = str(prog.get("id") or "")
        if not pid.replace("-", "").replace("_", "").isalnum() or len(pid) > 40 or pid in ids:
            pid = f"p{int(time.time() * 1000):x}{i}"
        ids.add(pid)
        segs = prog.get("segments") or []
        if not 1 <= len(segs) <= 50:
            raise CommandError("err_invalid_value", field="segments")
        programs.append({"id": pid, "name": name, "segments": [
            {"speed": clamp_speed(number(s, "speed", MIN_SPEED, MAX_SPEED)),
             "seconds": int(round(number(s, "seconds", 15, 10800)))} for s in segs]})
    return programs


def settings_changes(data: dict) -> dict:
    """Validate a partial settings update (all values metric)."""
    changes = {}
    if "weight_kg" in data:
        changes["weight_kg"] = round(number(data, "weight_kg", 20, 300), 1)
    if "meeting_speed" in data:
        changes["meeting_speed"] = clamp_speed(number(data, "meeting_speed", MIN_SPEED, MAX_SPEED))
    if "goal" in data:
        goal = data["goal"] or {}
        if goal.get("type") not in ("steps", "km"):
            raise CommandError("err_invalid_value", field="goal")
        changes["goal"] = {"type": goal["type"],
                           "value": number(goal, "value", 0.1, 100 if goal["type"] == "km" else 100000)}
    if "reminder" in data:
        rem = data["reminder"] or {}
        changes["reminder"] = {"enabled": bool(rem.get("enabled")), "minutes": int(number(rem, "minutes", 5, 600))}
    for key in ("oil_interval_km", "belt_km"):
        if key in data:
            changes[key] = number(data, key, 0, 100000)
    for key in CONFIG_KEYS:
        if key in data:
            if not isinstance(data[key], bool):
                raise CommandError("err_invalid_value", field=key)
            changes[key] = data[key]
    if "programs" in data:
        changes["programs"] = clean_programs(data["programs"])
    return changes


# ── Logging: rotating file plus the last lines in memory ───────────────
class MemoryLog(logging.Handler):
    def __init__(self, size=50):
        super().__init__()
        self.lines = deque(maxlen=size)
        self.seq = 0

    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.lines.append(line)
        self.seq += 1

    def since(self, seq):
        """New lines since seq (all kept lines for a new client), and the current seq."""
        if seq >= self.seq:
            return [], self.seq
        return list(self.lines)[-(self.seq - seq):], self.seq


def setup_logging(log_file: Path) -> MemoryLog:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.namer = lambda name: name.replace(".log.", ".") + ".log"  # deskwalk.1.log stays *.log
    memory = MemoryLog()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (file_handler, memory, logging.StreamHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.getLogger("bleak").setLevel(logging.WARNING)
    return memory


# ── HTTP ────────────────────────────────────────────────────────────────
class App:
    def __init__(self, args):
        self.store = Store(Path(args.data))
        self.tracker = SessionTracker(self.store)
        self.worker = Worker(self.store, self.tracker, sim=args.sim, address=args.address)
        self.started = time.time()
        self.memory_log = None

    def state(self) -> dict:
        state = self.worker.snapshot()
        with self.worker.lock:  # the tracker is changed in the Bluetooth thread under this lock
            state.update(self.tracker.snapshot())
        state["app_started_ts"] = self.started
        return state


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "deskwalk"

    def log_message(self, fmt, *args):
        pass  # no access log

    def _send(self, status, body: bytes, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, exc: CommandError, status=409):
        self._json({"ok": False, "key": exc.key, "args": exc.params}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise CommandError("err_invalid_value")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise CommandError("err_invalid_value")
        if not isinstance(data, dict):
            raise CommandError("err_invalid_value")
        return data

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/events":
            self._events()
        elif url.path == "/history":
            query = parse_qs(url.query)
            with self.app.worker.lock:
                if "session" in query:
                    result = self.app.tracker.session_detail(query["session"][0])
                else:
                    result = self.app.tracker.history()
            if result is None:
                return self._json({"ok": False, "key": "err_not_found_session"}, 404)
            self._json(result)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
            if path == "/cmd":
                name = str(data.pop("cmd", ""))
                result = self.app.worker.call(name, data)
                self._json({"ok": True, **result, "state": self.app.state()})
            elif path == "/settings":
                changes = settings_changes(data)
                with self.app.worker.lock:
                    self.app.store.update_settings(**changes)
                    self.app.tracker.invalidate()
                self._json({"ok": True, "key": "msg_saved", "state": self.app.state()})
            else:
                self._send(404, b"not found", "text/plain")
        except CommandError as exc:
            self._error(exc, 400 if path == "/settings" else 409)

    def _events(self):
        """Server-Sent Events: the full state every second, new log lines when there are some."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = 0
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while True:
                msg = f"event: state\ndata: {json.dumps(self.app.state(), separators=(',', ':'))}\n\n"
                lines, seq = self.app.memory_log.since(seq)
                if lines:
                    msg += f"event: log\ndata: {json.dumps(lines)}\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # sends nothing, only picks the interface
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description="Web controller for the Kingsmith WalkingPad A1 Pro")
    parser.add_argument("--sim", action="store_true", help="simulated belt, no Bluetooth")
    parser.add_argument("--host", default="0.0.0.0", help="127.0.0.1 = this computer only (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--data", default=str(HERE / "data.json"), help="data file (default: data.json)")
    parser.add_argument("--address", help="Bluetooth address of the belt instead of scanning")
    args = parser.parse_args()

    memory_log = setup_logging(HERE / "deskwalk.log")
    app = App(args)
    app.memory_log = memory_log
    Handler.app = app
    app.worker.start()

    def shutdown(*_):
        with app.worker.lock:
            app.tracker.flush()
        log.info("Stopped, data saved")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"\n  deskwalk:        http://127.0.0.1:{args.port}", flush=True)
    if args.host == "0.0.0.0" and lan_ip():
        print(f"  Phone (Wi-Fi):   http://{lan_ip()}:{args.port}", flush=True)
    print("  Stop with Ctrl+C\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
