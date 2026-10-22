"""Sessions, daily totals, goal, streaks and records."""

import logging
import time
from datetime import date, timedelta

from .fitness import cadence, kcal, stride_m
from .protocol import CommandError
from .store import Store

log = logging.getLogger("deskwalk")

SESSION_END_AFTER = 120  # s standstill until a session ends
SAMPLE_EVERY = 5         # s between two history points while walking
MAX_SAMPLES = 720        # points kept per closed session, older ones are thinned out
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


def thin_samples(samples: list, limit: int = MAX_SAMPLES) -> list:
    """Keep at most `limit` points, evenly spread, always with the first and the last one."""
    if len(samples) <= limit:
        return samples
    step = (len(samples) - 1) / (limit - 1)
    return [samples[round(i * step)] for i in range(limit)]


class SessionTracker:
    """A session starts when the belt starts moving and ends after 2 minutes of standstill.

    If the Bluetooth link drops, the session stays open and continues after reconnecting
    (the belt keeps counting). It is closed after 30 minutes without a connection.
    All methods that change state run under store.lock.
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

        # only the newest open session can still be running, older ones are closed
        open_sessions = sorted((s for s in store.sessions if s.get("end_ts") is None), key=lambda s: s["start_ts"])
        for s in open_sessions[:-1]:
            self._close(s)
        if open_sessions:
            s = open_sessions[-1]
            if time.time() - s["last_ts"] > MAX_GAP:
                self._close(s)
            else:
                self.current = s
                self.stopped_since = s["last_ts"] if s["last_running_ts"] < s["last_ts"] else None
                log.info("Resuming open session %s", s["id"])
        store.save()

    def device_baseline(self) -> dict:
        """Last belt counters of the open session, so nothing is counted twice after an app restart."""
        c = self.current
        return {"time": c["dev_time"], "dist": c["dev_dist"], "steps": c["dev_steps"]} if c else {}

    # data from the belt (Bluetooth thread, under the store lock)
    def update(self, speed, d_time, d_dist_km, d_steps, now, dev):
        settings = self.store.settings
        running = speed > 0
        self.speed = speed
        if running:
            self._last_running_speed = speed
        if d_dist_km:
            settings["belt_km"] = round(settings["belt_km"] + d_dist_km, 3)
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
        # own steps = distance / stride length. The belt reports distance in 10 m units, so the
        # distance is taken from speed x running time, which is what the belt measures as well.
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
        self._days_cache = None
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
        s["samples"] = thin_samples([p for p in s["samples"] if p[0] <= offset])
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
        since_oil = max(0.0, belt_km - float(settings["oil_last_km"]))
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
                            "since_oil_km": round(since_oil, 2),
                            "oil_due": oil_interval > 0 and since_oil >= oil_interval},
        }

    def history(self) -> dict:
        days = self.days()
        since = (date.today() - timedelta(days=371)).isoformat()
        all_s = self.store.sessions
        recent = sorted(all_s, key=lambda s: s["start_ts"], reverse=True)[:200]
        closed = [s for s in all_s if s.get("end_ts")]
        longest = max(closed, key=lambda s: s["active_s"], default=None)
        best_day = max(days.values(), key=lambda d: d["km"], default=None)
        return {
            "days": [{"day": d, "km": round(v["km"], 3), "steps": round(v["steps"]), "kcal": round(v["kcal"]),
                      "seconds": round(v["seconds"]), "sessions": v["sessions"]}
                     for d, v in sorted(days.items()) if d >= since],
            "sessions": [{k: s[k] for k in SUMMARY_FIELDS} for s in recent],
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
