# deskwalk

Minimal web controller for the Kingsmith WalkingPad A1 Pro. A small Python server talks to the
belt via Bluetooth, the browser shows the UI. Works on a laptop and on a phone in the same Wi-Fi,
in any browser (Firefox, Safari, Chrome, Brave).

Not an official project. No affiliation with Kingsmith or WalkingPad.

- Live speed, time, distance, steps, calories and cadence
- Start, stop, speed buttons and slider, quick speeds, meeting and pause buttons
- Sessions with history, daily goal ring, streaks, year heat map and records
- Interval programs, step calibration, CSV and TCX export
- Belt settings read from the belt: smart start/stop, sensitivity, child lock, start and max speed,
  units and values on the belt display
- Import of walks stored in the belt, for walks with the remote control only
- Novice limit hint, error codes, firmware and model info
- Movement reminder, maintenance counter for oiling
- English or German, metric or imperial, system, dark or light theme
- Installable on the phone home screen

No JavaScript libraries, no build step, no external requests. The only dependency is `bleak`.

## Start

Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

Open <http://127.0.0.1:5000>. On the phone use the Wi-Fi address that the server prints.
Switch the belt on and disconnect the phone app first, the belt accepts only one Bluetooth connection.

Without a belt: `python3 server.py --sim` runs a simulated belt (no `bleak` needed).

Options: `--host 127.0.0.1` (this computer only), `--port`, `--data`, `--address` (skip the scan).

Anyone in your network can control the belt while the server listens on `0.0.0.0`.
Use `--host 127.0.0.1` if you do not want that. Other websites cannot send commands:
the server accepts only same-origin JSON requests and checks the `Host` header.

## Keep walking after stepping off

Set *Smart start/stop* to *Off* in the settings and walk in manual mode. If the belt still stops,
*Keep running* restarts it at the last speed (at most 3 times per minute). A stop from the remote
control is recognised by the button code the belt reports and does not restart it.

Newer firmware (protocol version 20 and up, shown in the status view) has its own *Stop when nobody
walks* setting, deskwalk shows it in the belt settings then.

## Keys

| Key | Action |
|---|---|
| Space | start / stop |
| Up / Down | speed plus or minus 0.1 |
| M | meeting speed, press again for the previous speed |
| P | pause, press again to continue at the same speed |

## Data

Everything is stored in `data.json` next to `server.py` (git-ignored), log lines go to `deskwalk.log`.
A session starts when the belt starts moving and ends after 2 minutes of standstill. If the
Bluetooth connection drops, the session continues after reconnecting. An unreadable `data.json`
is kept as `data.json.broken-<time>` and the app starts empty.

Browser notifications for the movement reminder need `http://127.0.0.1` or HTTPS. On a phone
using the Wi-Fi address the reminder shows inside the page instead.

## Project layout

```
server.py            entry point
deskwalk/            Python package
  protocol.py        Bluetooth frames, see PROTOCOL.md
  links.py           bleak link and simulated belt
  worker.py          Bluetooth thread, command queue, reconnect
  sessions.py        sessions, goal, streaks, records
  store.py           data.json
  settings.py        validation of settings from the UI
  web.py             HTTP server, security headers, static files
  fitness.py, logs.py
web/                 browser UI
  index.html
  css/               tokens, base, layout, components, views, responsive
  js/                ES modules, one per view
  assets/icons, assets/fonts
tests/
```

## Development

```bash
python3 -m unittest discover -s tests
```

The Bluetooth protocol is described in [PROTOCOL.md](PROTOCOL.md). The basic frames come from
[ph4-walkingpad](https://github.com/ph4r05/ph4-walkingpad) by Dusan Klinec (MIT), thank you.
Device info, settings and stored walks follow a static analysis of the official KS Fit app.

Tested with the A1 Pro on Linux. Reports about other models and systems are welcome.

## Licence

MIT, see [LICENSE](LICENSE). Fonts: Inter and JetBrains Mono, SIL Open Font License,
see `web/assets/fonts/`.
