# deskwalk

Minimal web controller for the Kingsmith WalkingPad A1 Pro. A small Python server talks to the
belt via Bluetooth, the browser shows the UI. Works on a laptop and on a phone in the same Wi-Fi.

- Live speed, time, distance, steps, calories and cadence
- Start, stop, speed buttons and slider, quick speeds, meeting and pause buttons
- Sessions with history, daily goal ring, streaks and records
- Interval programs, step calibration, CSV and TCX export
- Belt settings (smart start/stop, sensitivity, child lock, start and max speed)
- Movement reminder, maintenance counter for oiling
- English or German, metric or imperial, system, dark or light theme

No JavaScript libraries, no web fonts, no build step. The only dependency is `bleak`.

## Start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

Open <http://127.0.0.1:5000>. On the phone use the Wi-Fi address that the server prints.
Switch the belt on and disconnect the phone app first; the belt accepts only one Bluetooth connection.

Without a belt: `python3 server.py --sim` runs a simulated belt (no `bleak` needed).

Options: `--host 127.0.0.1` (this computer only), `--port`, `--data`, `--address` (skip the scan).

**Note:** anyone in your network can control the belt while the server listens on `0.0.0.0`.
Use `--host 127.0.0.1` if you do not want that.

## Keys

| Key | Action |
|---|---|
| Space | start / stop |
| ↑ / ↓ | speed ± 0.1 |
| M | meeting speed, press again for the previous speed |
| P | pause, press again to continue at the same speed |

## Keep walking after stepping off

Set *Smart start/stop* to *Off* in the settings. If the belt still stops, *Keep running*
restarts it at the last speed (at most 3 times per minute). A stop from the remote control
then restarts the belt as well, so stop it from the app or with the space bar.

## Data

Everything is stored in `data.json` next to `server.py` (git-ignored), log lines go to `deskwalk.log`.
A session starts when the belt starts moving and ends after 2 minutes of standstill. If the
Bluetooth connection drops, the session continues after reconnecting.

Browser notifications for the movement reminder need `http://127.0.0.1` or HTTPS. On a phone
using the Wi-Fi address the reminder shows inside the page instead.

## Development

```bash
python3 test_protocol.py
```

The Bluetooth protocol is described in [PROTOCOL.md](PROTOCOL.md).

## Licence

GPL-3.0, see [LICENSE](LICENSE).
