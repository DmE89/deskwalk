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
from .links import UnsupportedModel
from .protocol import (BUTTON_STOP, MAX_SPEED, MIN_SPEED, MODE_BY_NAME, MODE_MANUAL, MODE_NAMES, NEW_PROTOCOL,
                       PREF_CALIBRATION, PREF_PROPS, CommandError, clamp_speed, cmd_ask_props, cmd_ask_records,
                       cmd_ask_settings, cmd_ask_status, cmd_hello, cmd_set_mode, cmd_set_pref, cmd_set_prop, cmd_set_speed,
                       cmd_start, cmd_tutorial, frame_type, parse_device_info, parse_error, parse_props,
                       parse_record, parse_settings, parse_status, pref_frame, record_start_ts, state_name)
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
BELT_COMMANDS = {"start", "stop", "speed", "mode", "pref", "apply_prefs", "meeting", "pause", "program_start",
                 "read_records", "calibration_run", "read_settings", "guide_start", "guide_next", "guide_cancel"}
ACK_TIMEOUT = 1.5        # s: no frame after a command, so it is sent once more (as the official app does)
MAX_RECORDS = 30         # stored walks read in one go
MODE_GUIDE = 3
# novice guide as in the official app: walk 30 s at 3 km/h, then 30 s at 4 km/h, skip the automatic part
GUIDE_HOLD = 30
GUIDE_WALKS = [{"speed": 3.0, "begin": 1, "end": None}, {"speed": 4.0, "begin": 3, "end": 4}]
GUIDE_AUTO_BEGIN, GUIDE_AUTO_END, GUIDE_FINISH = 5, 6, 7
NOVICE_LIMIT = 3.0       # km/h: new belts stay at this speed until unlocked (see the manual)
NOVICE_AFTER = 8.0       # s after a faster speed command before the hint shows
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
        self.guide = None
        self.events = deque(maxlen=30)
        self.stats = {"connects": 0, "drops": 0, "connected_since": None, "latency_ms": None, "packets": 0}
        self.state = {"conn": "disconnected", "message": {"key": ""}, "device": None, "belt": None,
                      "last_status": None, "last_speed": 2.0, "novice_limit": False,
                      "device_info": None, "belt_settings": None, "belt_records": None, "belt_error": None,
                      "unsupported": False}
        self._rx = None
        self._waiters = {}
        self._last_record = None

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
        self._rx = asyncio.Event()
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
            "limits": self._limits(s),
            "meeting": {"active": self.meeting_restore is not None, "restore_speed": self.meeting_restore},
            "paused": {"active": self.paused_speed is not None, "speed": self.paused_speed},
            "program": program,
            "guide": dict(self.guide) if self.guide else None,
            "events": events,
        }

    @staticmethod
    def _limits(state) -> dict:
        """Speed range: minimum from the device info, maximum from the settings of the belt."""
        info, prefs = state.get("device_info") or {}, state.get("belt_settings") or {}
        low = max(MIN_SPEED, info.get("min_speed") or MIN_SPEED)
        high = min(MAX_SPEED, prefs.get("max_speed") or MAX_SPEED)
        return {"min": low, "max": max(low, high)}

    def _protocol(self) -> int:
        return (self.state.get("device_info") or {}).get("protocol") or 0

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
                    self._guide_tick(dt)
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

    async def _send_acked(self, data: bytes, retry: bool = True):
        """Send a command. If no frame arrives within ACK_TIMEOUT, send it once more.

        Never retry a toggle (start/pause): sent twice it would start and pause again.
        """
        self._rx.clear()
        await self._send(data)
        if not retry:
            return
        try:
            await asyncio.wait_for(self._rx.wait(), timeout=ACK_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("No answer to %s, sending again", data.hex(" "))
            await self._send(data)

    async def _request(self, data: bytes, kind: int, timeout=3.0):
        """Send a request and wait for the answer frame of that type. Returns it or None."""
        fut = self.loop.create_future()
        self._waiters.setdefault(kind, []).append(fut)
        try:
            await self._send(data)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if fut in self._waiters.get(kind, []):
                self._waiters[kind].remove(fut)

    async def _read_settings(self):
        if self._protocol() >= NEW_PROTOCOL:
            await self._request(cmd_ask_props(*PREF_PROPS.values()), 0xB2)
        else:
            await self._request(cmd_ask_settings(), 0xA6)

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
            # A5 first, as the official app does: sets the clock of the belt, answer is the device info
            await self._send(cmd_hello())
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
        except UnsupportedModel as exc:
            self.link = None
            self._set(conn="error", unsupported=True)
            self._message("err_unsupported_model", name=str(exc))
            self.event("err_unsupported_model", logging.ERROR, name=str(exc))
            try:
                await asyncio.wait_for(link.disconnect(), timeout=5)
            except Exception:
                pass
            return False
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
        if not self.state["device_info"]:
            await self._request(cmd_hello(), 0xA5, timeout=2.5)
        info = self.state["device_info"]
        if info:
            self.event("ev_device_info", model=info["model"], firmware=info["firmware"], protocol=info["protocol"])
            if info["firmware_too_old"]:
                self.event("ev_firmware_old", logging.WARNING, model=info["model"], firmware=info["firmware"])
        if self.store.settings["apply_on_connect"]:
            await self._apply_prefs()
        await self._read_settings()
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
        if self.guide:
            self.guide = None
            self.event("ev_guide_cancelled", logging.WARNING)
        self._set(device_info=None, belt_settings=None, belt_records=None)
        if was_connected and self.want_connected:
            self.event("ev_connection_lost", logging.WARNING)

    # status from the belt (runs in the Bluetooth thread)
    def _on_data(self, data: bytes):
        self._rx.set()
        kind = frame_type(data)
        if kind == 0xA2:
            st = parse_status(data)
            if st:
                self._on_status(st)
        elif kind == 0xA5:
            info = parse_device_info(data)
            if info:
                self._set(device_info=info)
        elif kind == 0xA6:
            prefs = parse_settings(data)
            if prefs:
                self._set(belt_settings=prefs)
        elif kind == 0xA7:
            self._last_record = parse_record(data, self._protocol())
        elif kind == 0xB2:
            props = parse_props(data)
            if props:
                self._merge_props(props)
        elif kind == 0xE0:
            code = parse_error(data)
            self._set(belt_error={"code": code, "ts": time.time()})
            self.event("ev_belt_error", logging.WARNING, code=f"E{code:02d}")
        for fut in self._waiters.pop(kind, []):
            if not fut.done():
                fut.set_result(data)

    def _merge_props(self, props: dict):
        """Properties (protocol 20 and newer) into the same shape as the A6 settings."""
        with self.lock:
            prefs = dict(self.state.get("belt_settings") or {})
            mapping = {"start_type": ("inteli", bool), "sensitivity": ("sensitivity", int),
                       "child_lock": ("child_lock", bool), "unit": ("units_miles", bool), "panel": ("display", int),
                       "noload_stop": ("auto_stop", bool), "start_speed": ("start_speed", lambda v: v / 10 or None),
                       "max_walk": ("max_speed", lambda v: v / 10 or None)}
            for name, value in props.items():
                if name in mapping:
                    key, conv = mapping[name]
                    prefs[key] = conv(value)
            self.state["belt_settings"] = prefs

    def _novice_limited(self, speed, mode) -> bool:
        """The belt holds 3.0 km/h although a higher speed was sent some seconds ago."""
        if self.guide:
            return False
        prefs = self.state.get("belt_settings") or {}
        if prefs.get("max_speed") is not None and prefs["max_speed"] <= NOVICE_LIMIT:
            return True
        wanted, at = self._commanded
        return (mode == "manual" and wanted is not None and wanted > NOVICE_LIMIT + 0.05
                and abs(speed - NOVICE_LIMIT) < 0.05 and time.monotonic() - at > NOVICE_AFTER)

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
            prev_error = (self.state.get("belt") or {}).get("error", 0)
            self.state["belt"] = {"state": st["state"], "state_name": state_name(st["state"]), "running": speed > 0,
                                  "speed": speed, "mode": mode, "error": st["error"], "button": st["button"],
                                  "dev_time": st["time"], "dev_dist": st["dist"] / 100, "dev_steps": st["steps"]}
            self.state["last_status"] = now
            self.state["novice_limit"] = self._novice_limited(speed, mode)
            if speed > 0 and speed == self._prev_speed:  # two equal readings: not ramping
                self._stable_speed = speed
                self.state["last_speed"] = speed
            self.tracker.update(speed, deltas["time"], deltas["dist"] / 100, deltas["steps"], now,
                                dict(self._last_dev))
            if self._prev_speed == 0 and speed > 0 and self.program:
                self.program["need_speed"] = True  # back to the segment speed after a stop
        if st["error"] and st["error"] != prev_error:
            self.event("ev_belt_error", logging.WARNING, code=f"E{st['error']:02d}")
        stopped = (self._prev_speed > 0 and speed == 0 and mode != "auto" and now > self._expect_stop_until)
        self._prev_speed = speed
        if not stopped:
            return
        # the remote control reports its last button: a stop from there is wanted, not "by itself"
        if st["button"] == BUTTON_STOP:
            self.event("ev_stopped_by_remote")
            return
        self.event("ev_stopped_by_itself", logging.WARNING)
        if self.store.settings["keep_running"] and not self.guide:
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
        await self._send_acked(cmd_set_speed(speed))
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
        if belt().get("mode") in ("standby", "tutorial"):
            # as the official app: switch to manual first and wait until the belt reports it
            await self._send_acked(cmd_set_mode(MODE_MANUAL))
            await self._poll_until(lambda: belt().get("mode") == "manual", 5)
        sent = time.time()
        await self._send(cmd_start())
        self._expect_stop_until = 0
        self.event("ev_start")
        if not speed:
            return {"key": "msg_starting"}
        # wait for the countdown of the belt (about 3 s): only a fresh status after the start counts
        started = await self._poll_until(
            lambda: (self.state["last_status"] or 0) > sent + 0.3 and belt().get("running")
            and belt().get("state_name") != "countdown", 10)
        if not started:
            return {"key": "msg_starting"}
        await self._set_speed(speed)
        return {"key": "msg_running_at", "args": {"speed": speed}}

    async def _stop_belt(self):
        self._require_connected()
        self._expect_stop_until = time.time() + 15
        if self.running():
            self._set(last_speed=self._current_speed())
        await self._send_acked(cmd_set_speed(0))

    async def _apply_prefs(self):
        sent = 0
        for name, value in self.store.settings["prefs"].items():
            if value is None:
                continue
            try:
                await self._send(pref_frame(name, value, self._protocol())[1])
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
        limits = self._limits(self.state)
        speed = clamp_speed(speed, limits["min"], limits["max"])
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
        await self._send_acked(cmd_set_mode(MODE_BY_NAME[mode]))
        self.event("ev_mode", mode=mode)
        return {"key": "msg_mode_changed"}

    async def _cmd_pref(self, name, value):
        value, data = pref_frame(name, value, self._protocol())
        self._require_connected()
        await self._send_acked(data)
        with self.lock:
            if name in self.store.settings["prefs"]:
                self.store.settings["prefs"][name] = value
                self.store.save()
        self.event("ev_pref", name=name, value=value)
        await self._read_settings()  # the belt confirms nothing, so read the setting back
        return {"key": "msg_pref_sent"}

    async def _cmd_read_settings(self):
        self._require_connected()
        await self._read_settings()
        if not self.state["belt_settings"]:
            raise CommandError("err_no_settings")
        return {"key": "msg_settings_read"}

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
        limits = self._limits(self.state)
        meeting = clamp_speed(self.store.settings["meeting_speed"], limits["min"], limits["max"])
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
            self.store.update_settings(oil_last_km=self.store.settings["belt_km"], oil_last_ts=time.time())
        return {"key": "msg_oiled"}

    # stored walks in the belt
    async def _cmd_read_records(self):
        """Read all stored walks, newest first. Not while the belt runs (the official app forbids it)."""
        self._require_connected()
        if self.running():
            raise CommandError("err_belt_running")
        now, cursor, records, seen = time.time(), 0xFF, [], set()
        for _ in range(MAX_RECORDS):
            self._last_record = None
            if not await self._request(cmd_ask_records(cursor), 0xA7):
                break
            rec = self._last_record
            if not rec or rec["empty"]:
                break
            start = record_start_ts(rec, now)
            signature = f"{rec['time']}-{rec['dist']}-{rec['steps']}-{round(start / 600) if start else 'x'}"
            if signature in seen:
                break
            seen.add(signature)
            with self.lock:
                imported = signature in self.store.settings["imported_records"]
                known = self.tracker.matches_session(start, rec["time"], rec["dist"] / 100)
            records.append({"time": rec["time"], "dist": rec["dist"], "steps": rec["steps"], "start_ts": start,
                            "signature": signature, "imported": imported, "known": known, "raw": rec["raw"]})
            if rec["cursor"] in (cursor, 0):
                break
            cursor = rec["cursor"]
        self._set(belt_records={"read_at": now, "items": records})
        self.event("ev_records_read", count=len(records))
        return {"key": "msg_records_read", "args": {"count": len(records)}}

    async def _cmd_import_record(self, signature):
        listing = self.state.get("belt_records") or {}
        rec = next((r for r in listing.get("items", []) if r["signature"] == signature), None)
        if not rec:
            raise CommandError("err_no_record")
        with self.lock:
            done = self.store.settings["imported_records"]
            if signature in done or rec["known"]:
                raise CommandError("err_record_imported")
            session = self.tracker.import_record(rec, rec["start_ts"] or time.time() - rec["time"])
            self.store.update_settings(imported_records=(done + [signature])[-200:])
            rec["imported"] = True
        self.event("ev_record_imported", km=round(session["distance_km"], 2))
        return {"key": "msg_record_imported"}

    # experimental belt functions
    async def _cmd_calibration_run(self, on):
        """Calibration of the belt (A6 key 2). Nobody may stand on the belt."""
        if not isinstance(on, bool):
            raise CommandError("err_invalid_value")
        self._require_connected()
        if on and self.running():
            raise CommandError("err_belt_running")
        self._expect_stop_until = time.time() + 600 if on else time.time() + 15
        await self._send_acked(cmd_set_pref(PREF_CALIBRATION, int(on)))
        self.event("ev_calibration_on" if on else "ev_calibration_off")
        return {"key": "msg_calibration_on" if on else "msg_calibration_off"}

    # novice guide
    def _guide_tick(self, dt):
        g = self.guide
        if not g or g["phase"] != "walk" or not self._connected():
            return
        speed = self._belt().get("speed") or 0
        if self.running() and speed >= g["speed"] - 0.05:
            g["held"] = min(GUIDE_HOLD, g["held"] + dt)
        if g["held"] >= GUIDE_HOLD and not g.get("queued"):
            g["queued"] = True
            self.belt_queue.put_nowait(("guide_next", {}, concurrent.futures.Future()))

    async def _send_tutorial(self, step):
        await self._send_acked(cmd_set_prop(0x06, step) if self._protocol() >= NEW_PROTOCOL else cmd_tutorial(step))

    async def _cmd_guide_start(self):
        """Novice guide: lifts the 3 km/h limit of new belts. The user walks on the belt."""
        self._require_connected()
        if self.guide:
            raise CommandError("err_guide_running")
        self.program = None
        self.meeting_restore = self.paused_speed = None
        await self._send_acked(cmd_set_mode(MODE_GUIDE))
        walk = GUIDE_WALKS[0]
        await self._send_tutorial(walk["begin"])
        self.guide = {"phase": "walk", "index": 0, "count": len(GUIDE_WALKS), "speed": walk["speed"],
                      "held": 0.0, "hold": GUIDE_HOLD}
        self.event("ev_guide_started")
        if self.running():
            await self._set_speed(walk["speed"])
        else:
            sent = time.time()
            await self._send(cmd_start())
            belt = self._belt
            await self._poll_until(lambda: (self.state["last_status"] or 0) > sent + 0.3 and belt().get("running")
                                   and belt().get("state_name") != "countdown", 10)
            await self._set_speed(walk["speed"])
        return {"key": "msg_guide_started"}

    async def _cmd_guide_next(self):
        g = self.guide
        if not g or g["phase"] != "walk":
            raise CommandError("err_no_guide")
        walk = GUIDE_WALKS[g["index"]]
        if walk["end"] is not None:
            await self._send_tutorial(walk["end"])
        if g["index"] + 1 < len(GUIDE_WALKS):
            nxt = GUIDE_WALKS[g["index"] + 1]
            await self._send_tutorial(nxt["begin"])
            g.update(index=g["index"] + 1, speed=nxt["speed"], held=0.0, queued=False)
            await self._set_speed(nxt["speed"])
            self.event("ev_guide_step", index=g["index"] + 1, speed=nxt["speed"])
            return {"key": "msg_ok"}
        # the automatic part is skipped, as the official app allows it
        await self._send_tutorial(GUIDE_AUTO_BEGIN)
        await self._send_tutorial(GUIDE_AUTO_END)
        g["phase"] = "stopping"
        await self._stop_belt()
        await self._poll_until(lambda: not self.running(), 15)
        await asyncio.sleep(2)  # the official app waits 2 s after the stop
        await self._send_tutorial(GUIDE_FINISH)
        await self._send_acked(cmd_set_mode(MODE_MANUAL))
        await self._read_settings()
        prefs = self.state.get("belt_settings") or {}
        self.event("ev_guide_done", max_speed=prefs.get("max_speed"))
        self.guide = None
        return {"key": "msg_guide_done"}

    async def _cmd_guide_cancel(self):
        if not self.guide:
            raise CommandError("err_no_guide")
        self.guide = None
        await self._send_acked(cmd_set_mode(MODE_MANUAL))
        self.event("ev_guide_cancelled")
        return {"key": "msg_guide_cancelled"}

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
