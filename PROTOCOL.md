# WalkingPad A1 Pro Bluetooth protocol

What deskwalk sends to and reads from the belt. Only commands listed here are used.

Sources:
- [ph4-walkingpad](https://github.com/ph4r05/ph4-walkingpad) by Dusan Klinec (MIT licence) for the basic frames.
- A static analysis of the official KS Fit app 6.6.0 (no Bluetooth capture) for device info, settings,
  stored walks, the status fields and the timing.

deskwalk does not use code from either source. It sends the same frames with `bleak`.

Status column: **tested** = works on a real A1 Pro, **app** = used like this by the official app,
not yet tested with deskwalk on a real belt.

## Models

| BLE name | Model | Protocol |
|---|---|---|
| `WalkingPad`, `KS-ST-A1P` | A1 Pro (since 2020) | classic, service `fe00`, this document |
| `KS-HD-A1P` | A1 Pro ERP (since 07/2025) | FTMS, service `1826`, not supported |

deskwalk reports an unsupported model when the belt has service `1826` but no `fe01`/`fe02`.

## Transport

Bluetooth Low Energy, GATT, service `0000fe00-0000-1000-8000-00805f9b34fb`.

| Characteristic | UUID | Use |
|---|---|---|
| `fe01` | `0000fe01-0000-1000-8000-00805f9b34fb` | notify: frames from the belt |
| `fe02` | `0000fe02-0000-1000-8000-00805f9b34fb` | write: frames to the belt, without response if the belt allows it |

A connection counts only when both characteristics exist and a status request is answered on `fe01`.
On the A1 Pro the first request right after subscribing to `fe01` gets no reply, the second one does.
The belt accepts one connection at a time, so the phone app has to be disconnected.

## Frames

```
to the belt:    F7 <payload ...> <checksum> FD
from the belt:  F8 <payload ...> <checksum> FD      (answers to A2 to A7 are 20 bytes)
checksum = sum(payload bytes) mod 256
```

## Commands

| Command | Payload | Example | Status |
|---|---|---|---|
| Request status | `A2 00 00` | `F7 A2 00 00 A2 FD` | tested |
| Set speed (km/h x 10) | `A2 01 <speed>` | 3.0 km/h: `F7 A2 01 1E C1 FD` | tested |
| Stop belt (speed 0) | `A2 01 00` | `F7 A2 01 00 A3 FD` | tested |
| Start / pause toggle | `A2 04 01` | `F7 A2 04 01 A7 FD` | tested |
| Set mode | `A2 02 <mode>` | manual: `F7 A2 02 01 A5 FD` | app |
| Novice guide step | `A2 03 <step>` | finish: `F7 A2 03 07 AC FD` | app |
| Clock and device info | `A5 <local time, 4 bytes> 00 00` | | app |
| Request settings | `A6 00 00 00 00 00` | `F7 A6 00 00 00 00 00 A6 FD` | app |
| Set preference | `A6 <key> 00 00 00 <value>` | start speed 3.0: `F7 A6 04 00 00 00 1E C8 FD` | app |
| Request stored walks | `A7 AA <cursor>` | newest: `F7 A7 AA FF 50 FD` | app |

Modes: `0` automatic, `1` manual, `2` standby, `3` novice guide.

`A2 04 01` toggles start and pause. deskwalk stops with speed 0, as ph4-walkingpad does.

The local time in `A5` is Unix seconds plus the time zone offset, big-endian. The last two bytes are the
user id of the official app, `00 00` without login. The belt stamps its stored walks with this clock.

Preference keys:

| Key | Setting | Value |
|---|---|---|
| `02` | calibration run | `1` start, `0` stop |
| `03` | max speed | km/h x 10 |
| `04` | start speed | km/h x 10 |
| `05` | start mode | `0` remote control, `1` intelligent (smart start/stop) |
| `06` | sensitivity (automatic mode) | `1` high, `2` medium, `3` low |
| `07` | display | bit mask, see below |
| `08` | units on the belt display | `0` metric, `1` imperial |
| `09` | child lock | `0` off, `1` on |

Display mask: bit 0 time, 1 speed, 2 distance, 3 calories, 4 steps. At least 3 bits, all = `1F`.

A goal cannot be set, only read (see the settings answer).

## Answers

### Status `F8 A2`

```
offset  0   1   2      3      4     5..7   8..10  11..13  14  15     16      17
        F8  A2  state  speed  mode  time   dist   steps   -   error  button  -
```

| Field | Meaning |
|---|---|
| state | `0` ready, `1` running, `2` paused, `3` stopped, `4` error, `5` sleep, `6` to `9` countdown, `11` or `FF` safety key off |
| speed | km/h x 10 |
| mode | see above |
| time | seconds since start, 3 bytes big-endian |
| dist | units of 10 m, 3 bytes big-endian |
| steps | step counter of the belt, 3 bytes big-endian |
| error | error code, `0` none (meanings in the manual: E01, E02, E05, E06, E08, E10, E11) |
| button | last button on the remote: `1` mode, `2` plus, `3` stop, `4` minus, `81` mode long |

The counters reset when the belt starts. deskwalk adds up the differences, so a reset or a
dropped connection does not lose distance. A stop with button `3` counts as a stop from the remote
control, so *Keep running* does not restart the belt then.

### Device info `F8 A5`

| Byte | Meaning |
|---|---|
| 3, 4 | model, 2 ASCII characters, for example `A1` |
| 7 | upper limit for the max speed setting, km/h x 10 (`0` = 12.0) |
| 8 | minimum speed, km/h x 10 (at least 0.5; `0` = 1.0) |
| 15 | firmware version. The official app refuses to control an `A1` below 28 |
| 16 | protocol version. From 20 on, settings go as `B3` properties instead of `A6` |

### Settings `F8 A6`

| Byte | Meaning |
|---|---|
| 2 | goal type: `0` time, `1` distance, `2` calories, `3` count |
| 3 to 5 | goal value |
| 7 | max speed, km/h x 10 |
| 8 | start speed, km/h x 10 |
| 9 | start mode |
| 10 | sensitivity |
| 11 | display mask |
| 12 | child lock |
| 13 | units |
| 14 | max walking speed, km/h x 10 |

The belt does not confirm a setting. deskwalk reads the settings again after every change.

### Stored walk `F8 A7`

Ask with cursor `FF` for the newest walk, then with the cursor from byte 17 of the last answer,
until a frame with only zeros comes. Not while the belt runs.

Protocol version below 20:

| Bytes | Meaning |
|---|---|
| 2 to 4 | counter A |
| 5 to 7 | counter B |
| 8 to 10 | duration in seconds |
| 11 to 13 | distance in 10 m units |
| 14 to 16 | steps |
| 17 | cursor for the next request |

Start of the walk = now - A + B, as the official app computes it.

Protocol version 20 and newer: bytes 2 to 5 local start time, 6 to 7 duration, 8 to 9 distance,
10 to 11 steps, 12 to 15 calories x 10.

### Other answers

- `F8 E0 <code>`: error reported by the belt.
- `F8 B2 <n> [id value] ...`: properties, protocol version 20 and newer.

## Protocol version 20 and newer

Settings are written as `F7 B3 <length + 1> <id> <value, big-endian> <checksum> FD` and read with
`F7 B1 <n> <id ...>`. Property ids used by deskwalk:

| Id | Setting | Length |
|---|---|---|
| `03` | display | 1 |
| `06` | novice guide | 1 |
| `07` | max walking speed | 1 |
| `10` | child lock | 1 |
| `11` | start speed | 1 |
| `12` | sensitivity | 1 |
| `13` | start mode | 1 |
| `17` | units | 1 |
| `1E` | stop without load (auto stop) | 1 |

Auto stop exists only here, not on older firmware.

## Novice limit

New belts stay at 3 km/h until 2 km are walked and the belt is restarted, or until the novice guide in
the official app is done. deskwalk sends the last guide step (`A2 03 07`), sets max speed 6 km/h and
reads the settings back. Whether that is enough is not tested yet.

## Timing

- At least 0.7 s between two frames. The official app sends one frame per second.
- Queued speed commands are merged, only the last one is sent.
- A command without any answer within 1.5 s is sent once more, as the official app does.
- deskwalk requests the status once per second. No reply for 5 s counts as a lost connection.
- After `Start` the belt counts down about 3 s (state 6 to 9). A speed sent during the countdown is
  ignored, so deskwalk waits for the running state first.
- From standby or the novice guide, deskwalk switches to manual mode first and waits until the belt
  reports it, then starts.

## Not used

Firmware update (`A8`, `A9`): an interrupted or wrong update can make the belt unusable.
