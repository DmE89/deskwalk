"""Bluetooth worker: owns the link to the belt in its own thread with its own asyncio loop."""

import asyncio
import concurrent.futures
import copy
import inspect
import logging
import threading
import time
from collections import deque

from .fitness import add_calibration, calibration_point
from .links import BleLink, SimLink
from .protocol import (MAX_SPEED, MIN_SPEED, MODE_BY_NAME, MODE_MANUAL, MODE_NAMES, STATE_STARTING, CommandError,
                       clamp_speed, cmd_ask_status, cmd_set_mode, cmd_set_speed, cmd_start, parse_status,
                       pref_frame)
from .sessions import SessionTracker
from .store import Store

log = logging.getLogger("deskwalk")

CMD_SPACING = 0.7        # s between two commands, the belt drops faster ones
POLL_INTERVAL = 1.0      # s between status requests
DEAD_AFTER = 5.0         # s without a status reply = connection lost
FIRST_REPLY_TIMEOUT = 5.0
RECONNECT_DELAYS = [2, 5, 10, 30]
MAX_AUTO_RESTARTS = 3    # "keep running": at most this many restarts ...
AUTO_RESTART_WINDOW = 60  # ... within this many seconds
BELT_COMMANDS = {"start", "stop", "speed", "mode", "pref", "apply_prefs", "meeting", "pause", "program_start"}
COUNTERS = ("time", "dist", "steps")


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


def counter_deltas(new: dict, last: dict | None) -> dict:
    """Differences of the belt counters since the last status.

    The belt resets all counters when it starts. If any counter went down, the belt started
    again in between, so the new values count in full. Without a known previous value
    (first status after an app start without an open session) nothing is counted, because
    the counters may still show an earlier walk that is already recorded.
    """
    if last is None:
        return {k: 0 for k in COUNTERS}
    if any(new[k] < last[k] for k in COUNTERS):
        return {k: new[k] for k in COUNTERS}
    return {k: new[k] - last[k] for k in COUNTERS}


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
        baseline = tracker.device_baseline()
        self._last_dev = baseline or None
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
        if not self.ready.wait(5):
            raise RuntimeError("Bluetooth thread did not start")

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

    def call(self, name, args, timeout=25) -> dict:
        try:
            return self.submit(name, args).result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise CommandError("err_timeout")

    def shutdown(self, timeout=6.0):
        """Called from the main thread on exit: stop reconnecting and close the Bluetooth link."""
        if not self.loop or not self.loop.is_running():
            return
        self.want_connected = False
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            fut.result(timeout=timeout)
        except Exception as exc:
            log.warning("Bluetooth not closed cleanly: %s", exc)

    async def _shutdown(self):
        if self._conn_task:
            self._conn_task.cancel()
        self._wake.set()
        await self._cleanup()

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
        handler = getattr(self, f"_cmd_{name}", None) if isinstance(name, str) and name.isidentifier() else None
        try:
            if handler is None:
                raise CommandError("err_unknown_command", name=str(name)[:40])
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
                fut.set_exception(CommandError("err_bluetooth", detail=str(exc) or type(exc).__name__))

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
        settings.pop("address", None)  # the Bluetooth address never leaves the server
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
        """Send one frame, at least CMD_SPACING after the previous one."""
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
        while self.belt_queue and not self.belt_queue.empty():
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
            deltas = counter_deltas(st, self._last_dev)
            self._last_dev = {k: st[k] for k in COUNTERS}
            self.stats["packets"] += 1
            if self._poll_sent is not None:
                self.stats["latency_ms"] = round((time.monotonic() - self._poll_sent) * 1000)
                self._poll_sent = None
            self.state["belt"] = {"state": st["state"], "running": speed > 0, "speed": speed, "mode": mode,
                                  "dev_time": st["time"], "dev_dist": st["dist"] / 100, "dev_steps": st["steps"]}
            self.state["last_status"] = now
            if speed > 0 and speed == self._prev_speed:  # two equal readings: not ramping
                self._stable_speed = speed
                self.state["last_speed"] = speed
            self.tracker.update(speed, deltas["time"], deltas["dist"] / 100, deltas["steps"], now,
                                dict(self._last_dev))
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
        # wait for the countdown of the belt (about 3 s): only a fresh status after the start counts
        started = await self._poll_until(
            lambda: (self.state["last_status"] or 0) > sent + 0.3 and belt().get("running")
            and belt().get("state") != STATE_STARTING, 10)
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
                await self._send(pref_frame(name, value)[1])
                sent += 1
            except Exception as exc:
                log.warning("Preference %s not sent: %s", name, exc)
        if sent:
            self.event("ev_prefs_sent", count=sent)

    # commands from the UI
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
        value, data = pref_frame(name, value)
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
        if any(isinstance(v, bool) for v in (speed, steps, seconds)):
            raise CommandError("err_invalid_value")
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
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(points):
                raise CommandError("err_invalid_value")
            points.pop(index)
            self.store.save()
        return {"key": "msg_saved"}

    async def _cmd_delete_session(self, id):
        try:
            session_id = int(id)
        except (TypeError, ValueError):
            raise CommandError("err_invalid_value")
        with self.lock:
            ok = self.tracker.delete(session_id)
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
