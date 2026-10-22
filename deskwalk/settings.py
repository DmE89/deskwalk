"""Validation of settings sent by the UI. All values arrive in metric units."""

import math
import time

from .protocol import MAX_SPEED, MIN_SPEED, CommandError, clamp_speed
from .store import CONFIG_KEYS


def number(data, key, lo, hi):
    value = data.get(key) if isinstance(data, dict) else None
    if isinstance(value, bool):
        raise CommandError("err_invalid_value", field=key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise CommandError("err_invalid_value", field=key)
    if not math.isfinite(value):
        raise CommandError("err_invalid_value", field=key)
    if not lo <= value <= hi:
        raise CommandError("err_out_of_range", field=key, min=lo, max=hi)
    return value


def clean_programs(raw) -> list:
    if not isinstance(raw, list) or len(raw) > 30:
        raise CommandError("err_invalid_value", field="programs")
    programs, ids = [], set()
    for i, prog in enumerate(raw):
        if not isinstance(prog, dict):
            raise CommandError("err_invalid_value", field="programs")
        name = str(prog.get("name") or "").strip()[:40]
        if not name:
            raise CommandError("err_program_name")
        pid = str(prog.get("id") or "")
        if not pid.replace("-", "").replace("_", "").isalnum() or len(pid) > 40 or pid in ids:
            pid = f"p{int(time.time() * 1000):x}{i}"
        ids.add(pid)
        segs = prog.get("segments")
        if not isinstance(segs, list) or not 1 <= len(segs) <= 50:
            raise CommandError("err_invalid_value", field="segments")
        programs.append({"id": pid, "name": name, "segments": [
            {"speed": clamp_speed(number(s, "speed", MIN_SPEED, MAX_SPEED)),
             "seconds": int(round(number(s, "seconds", 15, 10800)))} for s in segs]})
    return programs


def settings_changes(data: dict) -> dict:
    """Validate a partial settings update."""
    changes = {}
    if "weight_kg" in data:
        changes["weight_kg"] = round(number(data, "weight_kg", 20, 300), 1)
    if "meeting_speed" in data:
        changes["meeting_speed"] = clamp_speed(number(data, "meeting_speed", MIN_SPEED, MAX_SPEED))
    if "goal" in data:
        goal = data["goal"] if isinstance(data["goal"], dict) else {}
        if goal.get("type") not in ("steps", "km"):
            raise CommandError("err_invalid_value", field="goal")
        if goal["type"] == "km":
            value = round(number(goal, "value", 0.1, 100), 2)
        else:
            value = int(round(number(goal, "value", 100, 100000)))
        changes["goal"] = {"type": goal["type"], "value": value}
    if "reminder" in data:
        rem = data["reminder"] if isinstance(data["reminder"], dict) else {}
        changes["reminder"] = {"enabled": bool(rem.get("enabled")), "minutes": int(number(rem, "minutes", 5, 600))}
    for key in ("oil_interval_km", "belt_km"):
        if key in data:
            changes[key] = round(number(data, key, 0, 100000), 3)
    for key in CONFIG_KEYS:
        if key in data:
            if not isinstance(data[key], bool):
                raise CommandError("err_invalid_value", field=key)
            changes[key] = data[key]
    if "programs" in data:
        changes["programs"] = clean_programs(data["programs"])
    return changes
