# WalkingPad A1 Pro Bluetooth protocol

What deskwalk sends to and reads from the belt. Only commands listed here are used.
A new command needs a real test on the belt before it is added.

Source: the byte layout follows [ph4-walkingpad](https://github.com/ph4r05/ph4-walkingpad)
by Dusan Klinec (MIT licence). deskwalk does not use that library; it sends the same frames with `bleak`.

## Transport

Bluetooth Low Energy, GATT. The belt advertises a name containing `WalkingPad` (some units use a `KS-` prefix).

| Characteristic | UUID | Use |
|---|---|---|
| `fe01` | `0000fe01-0000-1000-8000-00805f9b34fb` | notify: status frames from the belt |
| `fe02` | `0000fe02-0000-1000-8000-00805f9b34fb` | write: command frames to the belt |

A connection counts only when both characteristics exist and a status request is answered on `fe01`.
On the A1 Pro the first request right after subscribing to `fe01` gets no reply; the second one does.
deskwalk therefore repeats the request once per second for up to 5 s.
The belt accepts one connection at a time, so the phone app has to be disconnected.

## Command frames

```
F7  <payload ...>  <checksum>  FD
checksum = sum(payload bytes) mod 256
```

| Command | Payload | Example | Tested on the A1 Pro |
|---|---|---|---|
| Request status | `A2 00 00` | `F7 A2 00 00 A2 FD` | yes |
| Set speed (km/h × 10) | `A2 01 <speed>` | 3.0 km/h: `F7 A2 01 1E C1 FD` | yes |
| Stop belt (speed 0) | `A2 01 00` | `F7 A2 01 00 A3 FD` | yes, with the previous app version |
| Start belt | `A2 04 01` | `F7 A2 04 01 A7 FD` | yes, with the previous app version |
| Set mode | `A2 02 <mode>` | manual: `F7 A2 02 01 A5 FD` | not yet |
| Set preference | `A6 <key> 00 <value, 3 bytes big-endian>` | start speed 3.0: `F7 A6 04 00 00 00 1E C8 FD` | sent without error, effect not readable |

Modes: `0` automatic, `1` manual, `2` standby.

Preference keys used by deskwalk:

| Key | Setting | Value |
|---|---|---|
| `03` | max speed | km/h × 10 |
| `04` | start speed | km/h × 10 |
| `05` | smart start/stop | `0` off, `1` on |
| `06` | sensitivity | `1` high, `2` medium, `3` low |
| `09` | child lock | `0` off, `1` on |

The belt does not report its preferences. deskwalk only shows the value it sent last.

## Experimental, not yet tested on the A1 Pro

Known from ph4-walkingpad. deskwalk offers them in the status view, marked as experimental.
Please report results.

| Command | Payload | Example |
|---|---|---|
| Ask for the last walk | `A7 AA FF` | `F7 A7 AA FF 50 FD` |
| Units on the belt display | `A6 08 00 <0 km, 1 mi>` | miles: `F7 A6 08 00 00 00 01 AF FD` |
| Values on the belt display | `A6 07 00 <bit mask 0 to 127>` | all: `F7 A6 07 00 00 00 7F 2C FD` |
| Goal on the belt | `A6 01 <type> <value, 3 bytes>` | type `0` none, `1` distance, `2` calories, `3` time |

Which display bit shows which value, and the unit of the goal value, are not known yet.

New belts stay at 3 km/h until 2 km are walked and the belt is restarted, or until the novice guide
in the official app is done. The command for that guide is not known. deskwalk tries max speed 6 km/h
(key `03`), which may or may not lift the limit.

The answer to "ask for the last walk" is a frame starting with `F8 A7`:

```
offset  0   1   2..7   8..10  11..13  14..16  ...
        F8  A7  -      time   dist    steps
```

time in seconds, dist in units of 10 m, steps from the belt counter (offsets as in ph4-walkingpad,
whose own parser has a bug that drops the record).

## Status frame (notification on `fe01`)

```
offset  0   1   2      3      4     5..7   8..10  11..13  14..15  16      17   18
        F8  A2  state  speed  mode  time   dist   steps   -       button  crc  FD
```

| Field | Meaning |
|---|---|
| state | `0` stopped, `1` running, `5` standby, `9` starting (countdown) |
| speed | km/h × 10 |
| mode | `0` automatic, `1` manual, `2` standby |
| time | seconds since start, 3 bytes big-endian |
| dist | distance in units of 10 m, 3 bytes big-endian |
| steps | step counter of the belt, 3 bytes big-endian |

The counters reset when the belt starts. deskwalk adds up the differences, so a reset or a
dropped connection does not lose distance.

## Timing

- At least 0.7 s between two commands, otherwise the belt drops commands.
- Queued speed commands are merged; only the last one is sent.
- deskwalk requests the status once per second. No reply for 5 s counts as a lost connection.
- After `Start belt` the belt counts down about 3 s. A speed sent during the countdown is ignored,
  so deskwalk waits for a running status first.
