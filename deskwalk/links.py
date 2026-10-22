"""Links to the belt: real Bluetooth via bleak, and a simulated belt for development."""

import asyncio
import time

from .protocol import (FE01, FE02, MODE_STANDBY, PREF_START_SPEED, STATE_RUNNING, STATE_STARTING,
                       STATE_STOPPED, is_walkingpad_name)


class BleLink:
    """Bluetooth link via bleak. A connection counts only when fe01 and fe02 exist
    and notifications on fe01 run (the worker checks this with a first status reply)."""

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
            if name and is_walkingpad_name(name):  # devices without a name are skipped
                return device
        return None

    async def connect(self, device):
        from bleak import BleakClient
        self.name, self.address = device.name, device.address
        self.client = BleakClient(device, disconnected_callback=lambda _client: self.on_disconnect(), timeout=20.0)
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
        self.last_walk = (0, 0, 0)  # time, distance in 10 m, steps of the last finished walk
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
            if self.speed == 0 and self.time:
                self.last_walk = (int(self.time), int(self.dist_m / 10), int(self.steps))
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
            return self.record_frame()
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

    def record_frame(self) -> bytes:
        seconds, dist, steps = self.last_walk
        body = bytes([0xF8, 0xA7, 0, 0, 0, 0, 0, 0]) + seconds.to_bytes(3, "big") + dist.to_bytes(3, "big")
        body += steps.to_bytes(3, "big")
        return body + bytes([sum(body[1:]) % 256, 0xFD])

    def status_frame(self) -> bytes:
        state = STATE_STARTING if self.start_at else (STATE_RUNNING if self.speed > 0 else STATE_STOPPED)
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
