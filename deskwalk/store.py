"""Settings and sessions in one JSON file (data.json)."""

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("deskwalk")

DEFAULT_PROGRAMS = [
    {"id": "warmup", "name": "Easy warm-up",
     "segments": [{"speed": 2.0, "seconds": 180}, {"speed": 3.0, "seconds": 300}, {"speed": 3.5, "seconds": 600},
                  {"speed": 2.5, "seconds": 120}]},
    {"id": "intervals", "name": "Intervals 4 x 3 min",
     "segments": [{"speed": 3.0, "seconds": 180}, {"speed": 5.0, "seconds": 180}, {"speed": 3.0, "seconds": 120},
                  {"speed": 5.0, "seconds": 180}, {"speed": 3.0, "seconds": 120}, {"speed": 5.0, "seconds": 180},
                  {"speed": 3.0, "seconds": 120}, {"speed": 5.0, "seconds": 180}, {"speed": 2.5, "seconds": 180}]},
    {"id": "desk", "name": "Desk hour",
     "segments": [{"speed": 2.5, "seconds": 1500}, {"speed": 4.0, "seconds": 300}, {"speed": 2.5, "seconds": 1500},
                  {"speed": 4.0, "seconds": 300}]},
]

DEFAULT_SETTINGS = {
    "address": None,             # last Bluetooth address found (stays local in data.json, never sent to the UI)
    "auto_reconnect": True,
    "apply_on_connect": False,   # send the stored belt preferences after every connect
    "restore_speed": True,       # after start, return to the last speed
    "keep_running": False,       # restart the belt if it stops by itself in manual mode
    "hold_speed": False,         # experimental: send the speed again as soon as the belt slows down by itself
    "prefs": {"inteli": None, "sensitivity": None, "child_lock": None, "start_speed": None, "max_speed": None,
              "units_miles": None, "display": None, "target": None},
    "weight_kg": 75.0,
    "goal": {"type": "steps", "value": 8000},
    "calibration": [],
    "meeting_speed": 1.5,
    "reminder": {"enabled": True, "minutes": 60},
    "belt_km": 0.0,
    "oil_interval_km": 200.0,
    "oil_last_km": 0.0,
    "oil_interval_days": 90,     # the manual says: oil every 3 months
    "oil_last_ts": None,         # set on first start, so the 90 days count from then
    "imported_records": [],      # signatures of stored walks already imported as sessions
    "programs": DEFAULT_PROGRAMS,
}
CONFIG_KEYS = ("auto_reconnect", "apply_on_connect", "restore_speed", "keep_running", "hold_speed")

SESSION_KEYS = ("id", "day", "start_ts", "end_ts", "last_ts", "last_running_ts", "active_s", "distance_km",
                "belt_steps", "own_steps", "calories", "max_speed", "dev_time", "dev_dist", "dev_steps", "samples")


def merge(defaults: dict, stored: dict) -> dict:
    """Stored values over defaults. Unknown keys are dropped, wrong types fall back to the default."""
    out = copy.deepcopy(defaults)
    for key, value in stored.items():
        if key not in out:
            continue
        default = out[key]
        if isinstance(default, dict) and isinstance(value, dict):
            out[key] = merge(default, value)
        elif default is None or value is None or isinstance(value, type(default)) or (
                isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool)):
            out[key] = value
    return out


def valid_session(s) -> bool:
    return isinstance(s, dict) and all(k in s for k in SESSION_KEYS) and isinstance(s["samples"], list)


class Store:
    """Settings and sessions in one JSON file. Writes go to a temp file that is then renamed."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.sessions: list = []
        if self.path.exists():
            self._load()
        self.next_id = max((s["id"] for s in self.sessions), default=0) + 1

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except (OSError, ValueError) as exc:
            # keep the broken file for a manual look instead of overwriting it
            backup = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
            os.replace(self.path, backup)
            log.error("data file unreadable (%s), moved to %s, starting empty", exc, backup.name)
            return
        settings = data.get("settings")
        self.settings = merge(DEFAULT_SETTINGS, settings if isinstance(settings, dict) else {})
        sessions = data.get("sessions")
        sessions = sessions if isinstance(sessions, list) else []
        self.sessions = [s for s in sessions if valid_session(s)]
        if len(self.sessions) != len(sessions):
            log.warning("%d invalid sessions in the data file ignored", len(sessions) - len(self.sessions))

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
