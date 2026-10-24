"""HTTP server: static files from web/, the JSON API and Server-Sent Events.

Security: the server controls a treadmill, so no other website may send commands to it.
- Host header: only IP addresses, localhost and this computer's name (blocks DNS rebinding).
- POST: JSON only (a cross-site form or no-cors fetch cannot send that without a preflight,
  and the server never allows cross-origin requests), plus an Origin and Sec-Fetch-Site check.
- Static files: only from web/, no hidden files, no path traversal.
"""

import ipaddress
import json
import logging
import mimetypes
import socket
import time
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .protocol import CommandError
from .sessions import SessionTracker
from .settings import settings_changes
from .store import Store
from .worker import Worker

log = logging.getLogger("deskwalk")

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 256_000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".woff2": "font/woff2", ".webmanifest": "application/manifest+json", ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self'; connect-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


class App:
    def __init__(self, data_path, sim=False, address=None):
        self.store = Store(Path(data_path))
        self.tracker = SessionTracker(self.store)
        self.worker = Worker(self.store, self.tracker, sim=sim, address=address)
        self.started = time.time()
        self.memory_log = None
        self.allowed_names = {"localhost"}
        try:
            host = socket.gethostname().lower()
            self.allowed_names |= {host, host.split(".")[0], host.split(".")[0] + ".local"}
        except OSError:
            pass

    def state(self) -> dict:
        state = self.worker.snapshot()
        with self.worker.lock:  # the tracker is changed in the Bluetooth thread under this lock
            state.update(self.tracker.snapshot())
        state["app_started_ts"] = self.started
        state["version"] = __version__
        return state

    def host_allowed(self, host_header: str | None) -> bool:
        """IP literals, localhost and the name of this computer. Anything else may be DNS rebinding."""
        if not host_header:
            return False
        host = host_header.strip().lower()
        if host.startswith("["):
            host = host[1:host.find("]")] if "]" in host else ""
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        return host in self.allowed_names or host.endswith(".localhost")


def static_path(url_path: str) -> Path | None:
    """Map a URL path to a file in web/, or None."""
    path = unquote(url_path)
    if path == "/":
        path = "/index.html"
    parts = [p for p in path.split("/") if p]
    if not parts or any(p.startswith(".") or "\\" in p or "\0" in p for p in parts):
        return None
    file = (WEB_ROOT / Path(*parts)).resolve()
    try:
        file.relative_to(WEB_ROOT)
    except ValueError:
        return None
    if not file.is_file() or file.suffix.lower() not in CONTENT_TYPES:
        return None
    return file


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "deskwalk"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # no access log

    # responses
    def _headers(self, status, content_type, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status, body: bytes, content_type, extra=None):
        self._headers(status, content_type, len(body), {"Cache-Control": "no-store", **(extra or {})})
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, exc: CommandError, status=409):
        self._json({"ok": False, "key": exc.key, "args": exc.params}, status)

    def _plain(self, status, text):
        self._send(status, text.encode(), "text/plain; charset=utf-8")

    def _guard(self) -> bool:
        if not self.app.host_allowed(self.headers.get("Host")):
            self._plain(421, "unknown host")
            return False
        return True

    # GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        try:
            if url.path == "/events":
                self._events()
            elif url.path == "/history":
                self._history(parse_qs(url.query))
            else:
                self._static(url.path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("GET %s failed", url.path)
            self._plain(500, "internal error")

    def _history(self, query):
        with self.app.worker.lock:
            if "session" in query:
                result = self.app.tracker.session_detail(query["session"][0])
            else:
                result = self.app.tracker.history()
        if result is None:
            if query.get("session", [""])[0] == "current":
                return self._json(None)  # no session running is a normal state, not an error
            return self._json({"ok": False, "key": "err_not_found_session"}, 404)
        self._json(result)

    def _static(self, url_path):
        file = static_path(url_path)
        if not file:
            return self._plain(404, "not found")
        stat = file.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        cache = {"Cache-Control": "no-cache", "ETag": etag, "Last-Modified": formatdate(stat.st_mtime, usegmt=True)}
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for key, value in cache.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        since = self.headers.get("If-Modified-Since")
        if since and not self.headers.get("If-None-Match"):
            try:
                if int(stat.st_mtime) <= parsedate_to_datetime(since).timestamp():
                    self.send_response(304)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            except (TypeError, ValueError):
                pass
        body = file.read_bytes()
        content_type = CONTENT_TYPES.get(file.suffix.lower()) or mimetypes.guess_type(file.name)[0]
        self._headers(200, content_type, len(body), cache)
        if self.command != "HEAD":
            self.wfile.write(body)

    # POST
    def _origin_ok(self) -> bool:
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # not a browser request (curl, scripts)
        parsed = urlparse(origin)
        return parsed.netloc.lower() == (self.headers.get("Host") or "").lower()

    def _body(self) -> dict:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise CommandError("err_invalid_value")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise CommandError("err_invalid_value")
        if length < 0 or length > MAX_BODY:
            raise CommandError("err_invalid_value")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CommandError("err_invalid_value")
        if not isinstance(data, dict):
            raise CommandError("err_invalid_value")
        return data

    def do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if not self._origin_ok():
            self.close_connection = True
            return self._plain(403, "forbidden")
        try:
            data = self._body()
            if path == "/cmd":
                name = str(data.pop("cmd", ""))
                result = self.app.worker.call(name, data)
                self._json({"ok": True, **result, "state": self.app.state()})
            elif path == "/settings":
                changes = settings_changes(data)
                with self.app.worker.lock:
                    self.app.store.update_settings(**changes)
                    self.app.tracker.invalidate()
                self._json({"ok": True, "key": "msg_saved", "state": self.app.state()})
            else:
                self._plain(404, "not found")
        except CommandError as exc:
            self.close_connection = True
            self._error(exc, 400 if path == "/settings" else 409)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("POST %s failed", path)
            self.close_connection = True
            self._json({"ok": False, "key": "err_internal"}, 500)

    def _events(self):
        """Server-Sent Events: the full state every second, new log lines when there are some."""
        self.close_connection = True
        self._headers(200, "text/event-stream", extra={"Cache-Control": "no-store", "Connection": "close"})
        seq = 0
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while True:
                msg = f"event: state\ndata: {json.dumps(self.app.state(), separators=(',', ':'))}\n\n"
                lines, seq = self.app.memory_log.since(seq)
                if lines:
                    msg += f"event: log\ndata: {json.dumps(lines)}\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    Handler.app = app
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # sends nothing, only picks the interface
            return s.getsockname()[0]
    except OSError:
        return None
