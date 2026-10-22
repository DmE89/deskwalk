"""Bluetooth protocol of the WalkingPad A1 Pro. Every frame here is documented in PROTOCOL.md."""

FE01 = "0000fe01-0000-1000-8000-00805f9b34fb"  # notify: status frames from the belt
FE02 = "0000fe02-0000-1000-8000-00805f9b34fb"  # write: command frames to the belt

MODE_AUTO, MODE_MANUAL, MODE_STANDBY = 0, 1, 2
MODE_NAMES = {MODE_AUTO: "auto", MODE_MANUAL: "manual", MODE_STANDBY: "standby"}
MODE_BY_NAME = {v: k for k, v in MODE_NAMES.items()}

STATE_STOPPED, STATE_RUNNING, STATE_STANDBY, STATE_STARTING = 0, 1, 5, 9

PREF_MAX_SPEED = 3
PREF_START_SPEED = 4
PREF_START_INTEL = 5
PREF_SENSITIVITY = 6  # 1 = high, 2 = medium, 3 = low
PREF_CHILD_LOCK = 9

MIN_SPEED, MAX_SPEED = 0.5, 6.0  # km/h


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
        "state": data[2],                              # see STATE_*
        "speed": data[3] / 10,                         # km/h
        "mode": data[4],                               # see MODE_*
        "time": int.from_bytes(data[5:8], "big"),      # s since start
        "dist": int.from_bytes(data[8:11], "big"),     # units of 10 m
        "steps": int.from_bytes(data[11:14], "big"),   # belt step counter
        "button": data[16],
    }


def clamp_speed(value) -> float:
    """Speed in km/h within the belt limits, rounded to the belt resolution of 0.1."""
    if isinstance(value, bool):
        raise CommandError("err_invalid_speed")
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise CommandError("err_invalid_speed")
    if speed != speed:  # NaN
        raise CommandError("err_invalid_speed")
    return round(min(MAX_SPEED, max(MIN_SPEED, speed)), 1)


def is_walkingpad_name(name: str) -> bool:
    name = name.lower()
    return "walkingpad" in name or name.startswith("ks-")


def pref_frame(name: str, value):
    """Validate a belt preference. Returns the cleaned value and its command frame."""
    if name in ("inteli", "child_lock"):
        if not isinstance(value, bool):
            raise CommandError("err_invalid_value")
        return value, cmd_set_pref(PREF_START_INTEL if name == "inteli" else PREF_CHILD_LOCK, int(value))
    if name == "sensitivity":
        if isinstance(value, bool) or value not in (1, 2, 3):
            raise CommandError("err_invalid_value")
        return value, cmd_set_pref(PREF_SENSITIVITY, value)
    if name in ("start_speed", "max_speed"):
        value = clamp_speed(value)
        return value, cmd_set_pref(PREF_START_SPEED if name == "start_speed" else PREF_MAX_SPEED, round(value * 10))
    raise CommandError("err_unknown_setting")
