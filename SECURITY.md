# Security

deskwalk controls a treadmill, so a security bug can have physical effects. Please report it privately
through [GitHub security advisories](https://github.com/DmE89/deskwalk/security/advisories/new)
instead of a public issue.

## What deskwalk does

- The server listens on `0.0.0.0:5000` by default, so devices in the same network can use it.
  `--host 127.0.0.1` limits it to the computer itself. There is no login.
- Commands are accepted only as `application/json` from the same origin. Requests with a foreign
  `Origin` or `Sec-Fetch-Site: cross-site` are refused, so other websites cannot start the belt.
- The `Host` header must be an IP address, `localhost` or the name of the computer (DNS rebinding).
- Static files are served only from `web/`, without hidden files or path traversal.
- Every page has a Content Security Policy without inline scripts, plus `nosniff`, frame denial,
  referrer and permissions policy.
- The Bluetooth address of the belt stays in `data.json` and is never sent to the browser.

## Not in scope

Running deskwalk on a network you do not trust. Use `--host 127.0.0.1` or a firewall there.
