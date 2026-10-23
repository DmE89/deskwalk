"""Bluetooth protocol of the classic WalkingPad (service fe00), as used by the A1 Pro.

Every frame here is documented in PROTOCOL.md. Sources: ph4-walkingpad and a static analysis
of the official KS Fit app. Tested on a real belt: status, speed, start, stop.
"""

import time

FE00 = "0000fe00-0000-1000-8000-00805f9b34fb"  # classic WalkingPad service
FE01 = "0000fe01-0000-1000-8000-00805f9b34fb"  # notify: frames from the belt
FE02 = "0000fe02-0000-1000-8000-00805f9b34fb"  # write: frames to the belt
FTMS = "00001826-0000-1000-8000-00805f9b34fb"  # newer belts (for example KS-HD-A1P), not supported

MODE_AUTO, MODE_MANUAL, MODE_STANDBY, MODE_TUTORIAL = 0, 1, 2, 3
MODE_NAMES = {MODE_AUTO: "auto", MODE_MANUAL: "manual", MODE_STANDBY: "standby", MODE_TUTORIAL: "tutorial"}
MODE_BY_NAME = {"auto": MODE_AUTO, "manual": MODE_MANUAL, "standby": MODE_STANDBY}

# byte 2 of the status frame
STATE_NAMES = {0: "ready", 1: "running", 2: "paused", 3: "stopped", 4: "error", 5: "sleep",
               10: "other", 11: "lock_off", 12: "pre_start", 13: "pre_end", 0xFF: "lock_off"}
STATE_STARTING = 9  # kept for older callers, the countdown is 6 to 9


def state_name(state: int) -> str:
    if 6 <= state <= 9:
        return "countdown"
    return STATE_NAMES.get(state, "other")


# byte 16 of the status frame: last button on the remote control
BUTTONS = {1: "mode", 2: "up", 3: "stop", 4: "down", 0x81: "mode_long"}
BUTTON_STOP = 3

# A6 preference keys (protocol version below 20)
PREF_CALIBRATION = 2
PREF_MAX_SPEED = 3
PREF_START_SPEED = 4
PREF_START_INTEL = 5   # start mode: 0 = remote control, 1 = intelligent
PREF_SENSITIVITY = 6   # 1 = high, 2 = medium, 3 = low
PREF_DISPLAY = 7       # bit mask, see DISPLAY_FIELDS
PREF_UNITS = 8         # 0 = metric, 1 = imperial
PREF_CHILD_LOCK = 9

# B3 property ids and value lengths (protocol version 20 and newer)
PROPS = {
    0x01: ("mode", 1), 0x02: ("dist", 2), 0x03: ("panel", 1), 0x04: ("button", 2), 0x05: ("target", 4),
    0x06: ("tutorial", 1), 0x07: ("max_walk", 1), 0x08: ("initial", 1), 0x09: ("state", 1), 0x0A: ("step", 2),
    0x0B: ("calorie", 3), 0x0C: ("speed", 1), 0x0D: ("time", 2), 0x0E: ("spm", 1), 0x0F: ("heart_rate", 1),
    0x10: ("child_lock", 1), 0x11: ("start_speed", 1), 0x12: ("sensitivity", 1), 0x13: ("start_type", 1),
    0x14: ("error_code", 1), 0x15: ("handrail", 1), 0x16: ("mcu_version", 2), 0x17: ("unit", 1),
    0x18: ("max_run", 1), 0x19: ("record_id", 4), 0x1A: ("ascension", 1), 0x1B: ("voltage", 1),
    0x1C: ("current", 1), 0x1D: ("boot_logo", 1), 0x1E: ("noload_stop", 1),
}
PREF_PROPS = {"inteli": 0x13, "sensitivity": 0x12, "child_lock": 0x10, "start_speed": 0x11, "max_speed": 0x07,
              "units_miles": 0x17, "display": 0x03, "auto_stop": 0x1E}
NEW_PROTOCOL = 20  # from this protocol version on, settings go as B3 properties

DISPLAY_FIELDS = ("time", "speed", "distance", "calories", "steps")  # bit 0 to 4
DISPLAY_ALL = 0x1F
DISPLAY_MIN_FIELDS = 3

GOAL_TYPES = {0: "time", 1: "distance", 2: "calories", 3: "count"}  # as read from A6, cannot be set

MIN_SPEED, MAX_SPEED = 0.5, 6.0  # km/h, the belt may narrow this (A5 byte 8, A6 byte 7)
FIRMWARE_MIN = {"A1": 28, "P1": 18, "Pt": 11, "C1": 10}  # the official app refuses to control older ones


class CommandError(Exception):
    """Error with a message key that the UI translates."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key, self.params = key, params


def frame(*payload: int) -> bytes:
    """Command frame: F7, payload, checksum (sum of payload mod 256), FD."""
    return bytes([0xF7, *payload, sum(payload) % 256, 0xFD])


def cmd_ask_status() -> bytes:
    return frame(0xA2, 0x00, 0x00)


def cmd_set_speed(kmh: float) -> bytes:
    return frame(0xA2, 0x01, int(round(kmh * 10)))


def cmd_set_mode(mode: int) -> bytes:
    return frame(0xA2, 0x02, mode)


def cmd_tutorial(step: int) -> bytes:
    """Step of the novice guide: 0 begin ... 7 finish."""
    return frame(0xA2, 0x03, step)


def cmd_start() -> bytes:
    """Toggles start and pause on the belt."""
    return frame(0xA2, 0x04, 0x01)


def local_epoch(now: float | None = None) -> int:
    """Local time as seconds since 1970, the way the belt expects it."""
    now = time.time() if now is None else now
    return int(now + time.localtime(now).tm_gmtoff)


def cmd_hello(now: float | None = None) -> bytes:
    """A5: set the clock of the belt (it stamps walks with it). The belt answers with device info."""
    t = local_epoch(now) & 0xFFFFFFFF
    return frame(0xA5, *t.to_bytes(4, "big"), 0x00, 0x00)  # user id 0, as the app sends it without login


def cmd_ask_settings() -> bytes:
    return frame(0xA6, 0x00, 0x00, 0x00, 0x00, 0x00)


def cmd_set_pref(key: int, value: int) -> bytes:
    value = int(value)
    return frame(0xA6, key, 0x00, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def cmd_ask_records(cursor: int = 0xFF) -> bytes:
    """A7: stored walks. 0xFF starts from the newest, then pass the cursor of the last answer."""
    return frame(0xA7, 0xAA, cursor & 0xFF)


def cmd_set_prop(pid: int, value: int) -> bytes:
    length = PROPS[pid][1]
    return frame(0xB3, length + 1, pid, *int(value).to_bytes(length, "big"))


def cmd_ask_props(*pids: int) -> bytes:
    return frame(0xB1, len(pids), *pids)


# ── frames from the belt ───────────────────────────────────────────────
def frame_type(data: bytes) -> int | None:
    return data[1] if len(data) >= 3 and data[0] == 0xF8 else None


def parse_status(data: bytes) -> dict | None:
    """F8 A2: live status, or None for anything else."""
    if len(data) < 17 or frame_type(data) != 0xA2:
        return None
    return {
        "state": data[2],
        "speed": data[3] / 10,                         # km/h
        "mode": data[4],                               # see MODE_*
        "time": int.from_bytes(data[5:8], "big"),      # s since start
        "dist": int.from_bytes(data[8:11], "big"),     # units of 10 m
        "steps": int.from_bytes(data[11:14], "big"),   # belt step counter
        "error": data[15],
        "button": data[16],
    }


def parse_device_info(data: bytes) -> dict | None:
    """F8 A5: model, speed limits, firmware and protocol version."""
    if len(data) < 17 or frame_type(data) != 0xA5:
        return None
    model = bytes(data[3:5]).decode("ascii", "replace").strip("\x00 ")
    max_limit = data[7] / 10 or 12.0
    min_speed = max(data[8] / 10, MIN_SPEED) if data[8] else 1.0
    firmware = data[15]
    minimum = FIRMWARE_MIN.get(model)
    return {"model": model, "max_limit": max_limit, "min_speed": min_speed, "firmware": firmware,
            "protocol": data[16], "firmware_too_old": minimum is not None and firmware < minimum}


def parse_settings(data: bytes) -> dict | None:
    """F8 A6: settings stored in the belt."""
    if len(data) < 15 or frame_type(data) != 0xA6:
        return None
    return {
        "goal": {"type": GOAL_TYPES.get(data[2], "none"), "value": int.from_bytes(data[3:6], "big")},
        "max_speed": data[7] / 10 or None,
        "start_speed": data[8] / 10 or None,
        "inteli": bool(data[9]),
        "sensitivity": data[10] if data[10] in (1, 2, 3) else None,
        "display": data[11] & DISPLAY_ALL,
        "child_lock": bool(data[12]),
        "units_miles": bool(data[13]),
        "max_walk": data[14] / 10 or None,
    }


def parse_record(data: bytes, protocol: int = 0) -> dict | None:
    """F8 A7: one stored walk. Returns {"empty": True} for the all-zero end frame."""
    if len(data) < 18 or frame_type(data) != 0xA7:
        return None
    if not any(data[2:18]):
        return {"empty": True}
    cursor = data[17]
    if protocol >= NEW_PROTOCOL:
        return {"empty": False, "local_ts": int.from_bytes(data[2:6], "big"),
                "time": int.from_bytes(data[6:8], "big"), "dist": int.from_bytes(data[8:10], "big"),
                "steps": int.from_bytes(data[10:12], "big"), "kcal": int.from_bytes(data[12:16], "big") / 10,
                "cursor": cursor, "raw": data.hex(" ")}
    return {"empty": False, "a": int.from_bytes(data[2:5], "big"), "b": int.from_bytes(data[5:8], "big"),
            "time": int.from_bytes(data[8:11], "big"), "dist": int.from_bytes(data[11:14], "big"),
            "steps": int.from_bytes(data[14:17], "big"), "cursor": cursor, "raw": data.hex(" ")}


def record_start_ts(record: dict, now: float) -> float | None:
    """Start time of a stored walk. Plausible values only, otherwise None."""
    if "local_ts" in record:
        ts = record["local_ts"] - time.localtime(record["local_ts"]).tm_gmtoff
    else:
        ts = now - record["a"] + record["b"]  # the way the app computes it for old firmware
    if not now - 400 * 86400 < ts <= now:
        return None
    return ts


def parse_error(data: bytes) -> int | None:
    """F8 E0 <code>: error reported by the belt."""
    if len(data) < 3 or frame_type(data) != 0xE0:
        return None
    return data[2]


def parse_props(data: bytes) -> dict | None:
    """F8 B2 <n> [id value]...: properties (protocol version 20 and newer)."""
    if len(data) < 4 or frame_type(data) != 0xB2:
        return None
    out, pos = {}, 3
    for _ in range(data[2]):
        if pos >= len(data) - 2 or data[pos] not in PROPS:
            break
        name, length = PROPS[data[pos]]
        out[name] = int.from_bytes(data[pos + 1:pos + 1 + length], "big")
        pos += 1 + length
    return out


# ── validation ─────────────────────────────────────────────────────────
def clamp_speed(value, low: float = MIN_SPEED, high: float = MAX_SPEED) -> float:
    """Speed in km/h within the limits, rounded to the belt resolution of 0.1."""
    if isinstance(value, bool):
        raise CommandError("err_invalid_speed")
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise CommandError("err_invalid_speed")
    if speed != speed:  # NaN
        raise CommandError("err_invalid_speed")
    return round(min(high, max(low, speed)), 1)


def is_walkingpad_name(name: str) -> bool:
    name = name.lower()
    return "walkingpad" in name or name.startswith("ks-")


def display_ok(mask) -> bool:
    return (not isinstance(mask, bool) and isinstance(mask, int) and 0 <= mask <= DISPLAY_ALL
            and bin(mask).count("1") >= DISPLAY_MIN_FIELDS)


def pref_frame(name: str, value, protocol: int = 0):
    """Validate a belt preference. Returns the cleaned value and its command frame.

    Below protocol version 20 settings go as A6 keys, from 20 on as B3 properties.
    """
    if name in ("inteli", "child_lock", "units_miles", "auto_stop"):
        if not isinstance(value, bool):
            raise CommandError("err_invalid_value")
        raw = int(value)
    elif name == "sensitivity":
        if isinstance(value, bool) or value not in (1, 2, 3):
            raise CommandError("err_invalid_value")
        raw = value
    elif name in ("start_speed", "max_speed"):
        value = clamp_speed(value)
        raw = round(value * 10)
    elif name == "display":
        if not display_ok(value):
            raise CommandError("err_display_fields")
        raw = value
    else:
        raise CommandError("err_unknown_setting")
    if protocol >= NEW_PROTOCOL:
        return value, cmd_set_prop(PREF_PROPS[name], raw)
    if name == "auto_stop":
        raise CommandError("err_needs_new_protocol")
    key = {"inteli": PREF_START_INTEL, "child_lock": PREF_CHILD_LOCK, "units_miles": PREF_UNITS,
           "sensitivity": PREF_SENSITIVITY, "start_speed": PREF_START_SPEED, "max_speed": PREF_MAX_SPEED,
           "display": PREF_DISPLAY}[name]
    return value, cmd_set_pref(key, raw)
