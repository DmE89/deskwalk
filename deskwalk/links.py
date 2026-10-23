"""Links to the belt: real Bluetooth via bleak, and a simulated belt for development."""

import asyncio
import time

from .protocol import (DISPLAY_ALL, FE01, FE02, FTMS, MODE_MANUAL, MODE_STANDBY, MODE_TUTORIAL, PREF_CALIBRATION,
                       PREF_CHILD_LOCK, PREF_DISPLAY, PREF_MAX_SPEED, PREF_SENSITIVITY, PREF_START_INTEL,
                       PREF_START_SPEED, PREF_UNITS, is_walkingpad_name)


class UnsupportedModel(ConnectionError):
    """The belt uses another protocol (for example FTMS on the KS-HD-A1P)."""


class BleLink:
    """Bluetooth link via bleak. A connection counts only when fe01 and fe02 exist
    and notifications on fe01 run (the worker checks this with a first status reply)."""

    def __init__(self, on_data, on_disconnect):
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.client = None
        self.name = None
        self.address = None
        self._response = True

    async def find(self, address=None):
        from bleak import BleakScanner  # imported here so tests and --sim run without bleak
        if address:
            device = await BleakScanner.find_device_by_address(address, timeout=8)
            if device:
                return device
        found = await BleakScanner.discover(timeout=10, return_adv=True)
        for device, adv in found.values():
            name = device.name or adv.local_name
            if name and is_walkingpad_name(name):  # devices without a name are skipped
                return device
        return None

    async def connect(self, device):
        from bleak import BleakClient
        self.name, self.address = device.name, device.address
        self.client = BleakClient(device, disconnected_callback=lambda _client: self.on_disconnect(), timeout=20.0)
        await self.client.connect()
        services = {service.uuid.lower() for service in self.client.services}
        chars = {c.uuid.lower(): c for service in self.client.services for c in service.characteristics}
        if FE01 not in chars or FE02 not in chars:
            if FTMS in services:
                raise UnsupportedModel(device.name or "FTMS")
            raise ConnectionError("characteristics fe01/fe02 not found")
        # as the official app: write without response when the belt allows it
        self._response = "write-without-response" not in chars[FE02].properties
        await self.client.start_notify(FE01, lambda _sender, data: self.on_data(bytes(data)))

    async def write(self, data: bytes):
        await self.client.write_gatt_char(FE02, data, response=self._response)

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
        self.prefs = {PREF_MAX_SPEED: 60, PREF_START_SPEED: 20, PREF_START_INTEL: 1, PREF_SENSITIVITY: 2,
                      PREF_DISPLAY: DISPLAY_ALL, PREF_CHILD_LOCK: 0, PREF_UNITS: 0}
        self.walks = []  # stored walks, newest first: (start_ts, seconds, distance in 10 m, steps)
        self._walk_start = None
        self.clock = None
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
            self._walk_start = time.time()
            self.target = self.prefs.get(PREF_START_SPEED, 20) / 10
        if self.speed < self.target:
            self.speed = min(self.target, self.speed + 1.0 * dt)
        elif self.speed > self.target:
            self.speed = max(self.target, self.speed - 2.0 * dt)
            if self.speed == 0 and self.time and self._walk_start:
                self.walks.insert(0, (self._walk_start, int(self.time), int(self.dist_m / 10), int(self.steps)))
                del self.walks[10:]
                self._walk_start = None
        if self.speed > 0:
            self.time += dt
            self.dist_m += self.speed / 3.6 * dt
            self.steps += self.speed / 3.6 / 0.6 * dt

    def handle(self, data: bytes) -> bytes | None:
        """Apply a command frame. Returns a status frame for status requests."""
        if len(data) < 4 or data[0] != 0xF7 or data[-1] != 0xFD or sum(data[1:-2]) % 256 != data[-2]:
            return None
        kind, sub = data[1], data[2]
        if kind == 0xA2 and sub == 0x00:
            return self.status_frame()
        if kind == 0xA7 and sub == 0xAA:
            return self.record_frame(data[3])
        if kind == 0xA5:
            self.clock = int.from_bytes(data[2:6], "big")
            return self.info_frame()
        if kind == 0xA6 and sub == 0x00:
            return self.settings_frame()
        if kind == 0xA2 and sub == 0x01:
            self.target = data[3] / 10
            if data[3] == 0:
                self.start_at = None
        elif kind == 0xA2 and sub == 0x02:
            self.mode = data[3]
            if self.mode == MODE_STANDBY:
                self.target = 0
        elif kind == 0xA2 and sub == 0x03:
            self.mode = MODE_TUTORIAL if data[3] < 7 else MODE_MANUAL
        elif kind == 0xA2 and sub == 0x04:
            if self.speed == 0 and not self.start_at:
                self.time = self.dist_m = self.steps = 0  # the belt resets its counters on start
                self.start_at = time.monotonic() + self.COUNTDOWN
                self.target = 0
        elif kind == 0xA6 and sub == PREF_CALIBRATION:
            self.target = 4.0 if data[6] else 0.0
        elif kind == 0xA6:
            self.prefs[sub] = int.from_bytes(data[4:7], "big")
            if sub == PREF_MAX_SPEED:
                self.target = min(self.target, self.prefs[sub] / 10)
        # the real belt is assumed to answer every command with a status frame (the official app
        # counts any frame as confirmation)
        return self.status_frame() if kind in (0xA2, 0xA6) else None

    @staticmethod
    def _finish(body: bytes) -> bytes:
        body = body.ljust(18, b"\x00")
        return body + bytes([sum(body[1:]) % 256, 0xFD])

    def record_frame(self, cursor: int) -> bytes:
        """Old firmware layout: counters A and B, then seconds, distance, steps, then the next cursor."""
        index = 0 if cursor == 0xFF else cursor
        if index >= len(self.walks):
            return self._finish(bytes([0xF8, 0xA7]))
        start, seconds, dist, steps = self.walks[index]
        age = max(0, int(time.time() - start))  # start = now - A + B with B = 0
        body = bytes([0xF8, 0xA7]) + age.to_bytes(3, "big") + bytes(3) + seconds.to_bytes(3, "big")
        body += dist.to_bytes(3, "big") + steps.to_bytes(3, "big") + bytes([index + 1])
        return self._finish(body)

    def info_frame(self) -> bytes:
        body = bytes([0xF8, 0xA5, 0]) + b"A1" + bytes([0, 0, 120, 5]) + bytes(6) + bytes([30, 10])
        return self._finish(body)

    def settings_frame(self) -> bytes:
        p = self.prefs
        body = bytes([0xF8, 0xA6, 0, 0, 0, 0, 0, p[PREF_MAX_SPEED], p[PREF_START_SPEED], p[PREF_START_INTEL],
                      p[PREF_SENSITIVITY], p[PREF_DISPLAY], p[PREF_CHILD_LOCK], p[PREF_UNITS], 60])
        return self._finish(body)

    def status_frame(self) -> bytes:
        state = 9 if self.start_at else (1 if self.speed > 0 else 0)
        body = bytes([0xF8, 0xA2, state, int(round(self.speed * 10)), self.mode])
        body += int(self.time).to_bytes(3, "big") + int(self.dist_m / 10).to_bytes(3, "big")
        body += int(self.steps).to_bytes(3, "big") + bytes([0, 0, 0, 0])
        return body + bytes([sum(body[1:]) % 256, 0xFD])


class SimLink:
    """Same interface as BleLink, talking to one shared SimBelt."""

    belt = None

    def __init__(self, on_data, on_disconnect):
        if SimLink.belt is None:
            SimLink.belt = SimBelt()
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
