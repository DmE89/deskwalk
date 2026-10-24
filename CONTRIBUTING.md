# Contributing

Thanks for helping. The most useful contribution is a test on a real belt: open a
[belt test report](https://github.com/DmE89/deskwalk/issues/new/choose) with your model, firmware and
what works.

## Setup

```bash
git clone https://github.com/DmE89/deskwalk.git
cd deskwalk
python3 server.py --sim                   # simulated belt, no Bluetooth needed
python3 -m unittest discover -s tests     # all tests run without hardware
```

## Rules

- Keep it small. The only dependency is `bleak`. No JavaScript libraries, web fonts from servers or build steps.
- Code, comments, commit messages and UI texts in English. Every UI text goes into `web/js/i18n.js`,
  in English and German.
- Do not invent Bluetooth commands. A new command needs a source or a test on a real belt, and an entry
  in `PROTOCOL.md` with its status (tested or not).
- Never send a toggle command twice (start/pause). Never add a firmware update.
- Never commit personal data: Bluetooth addresses, `data.json`, log files.
- UI: check desktop and phone width, dark and light theme, keyboard use.

## Pull requests

Small pull requests are easier to review. Describe what changes and how you tested it. The CI runs the
unit tests on Python 3.10 to 3.13 and a syntax check of the browser modules.
