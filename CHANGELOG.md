# Changelog

## Unreleased

- *Hold the speed* (experimental): sends the walking speed again as soon as the belt slows down by
  itself, for firmware that stops without load even with smart start/stop off.
- History and records update live while a session runs.
- The log shows every state change of the belt and every slowdown that the app did not ask for.

## 1.2.0 (2026-10-24)

- Novice guide like the official app, to lift the 3 km/h limit of new belts.
- Settings are read from the belt and after every change.
- Device info on connect: model, firmware, protocol version and speed range.
- Stored walks of the belt can be read and imported, walks recorded live are marked.
- Error codes, safety key and countdown in the status. A stop from the remote control no longer
  triggers *Keep running*.
- Settings as B3 properties for protocol version 20 and newer, including *Stop when nobody walks*.
- Belt centering run in the maintenance card, oiling reminder after days or kilometres.
- Session list in pages of 20.
- Fixed: display mask has 5 fields, the start toggle is never sent twice, no goal setter.

## 1.1.0 (2026-10-22)

- Split into the `deskwalk` package and the `web` folder with local fonts and icons.
- Security: same-origin JSON only, Host header check, Content Security Policy.
- Fixed double counting after an app restart, resuming of old open sessions, unreadable data files.
- Accessibility: keyboard focus, aria states, skip link, contrast.

## 1.0.0 (2026-10-22)

- First version: live data, start, stop, speed, sessions, history, records, programs, calibration,
  export, English and German, metric and imperial, dark and light theme.
