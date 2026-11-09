"""The dashboard's web server: the page and a small JSON API, on 127.0.0.1 only.

    GET  /                     the page (static/index.html, app.css, app.js)
    GET  /api/state            settings, their options, devices, checks, live state
    GET  /api/level            the microphone level, ~10 times a second while the page is open
    GET  /api/stats            statistics from history.jsonl
    POST /api/settings         {"changes": {key: value}}  apply and save
    POST /api/reset            every setting back to its default
    POST /api/test             {"what": "chime" | "speaker" | "welcome"}
    POST /api/history/archive  start the statistics afresh (the old history is kept, renamed)
    POST /api/shutdown         a standalone dashboard steps aside for a starting Bol OS

Only this computer can reach it, and other web pages cannot use it: the Host header must name this server
(no DNS rebinding) and every POST needs a JSON body with the X-Bolos header, which a cross-site request cannot
send without a CORS preflight this server never approves.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from bolos.controls import Controls
from bolos.dashboard.stats import summarize
from bolos.history import read_entries
from bolos.user_settings import SettingError
from bolos.wake import MODEL_PATH as WAKE_MODEL

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
PORTS_TO_TRY = 10
STATIC = Path(__file__).resolve().parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/favicon.svg": ("favicon.svg", "image/svg+xml")}
MAX_BODY = 64 * 1024
APP = "bolos-dashboard"


class Dashboard:
    def __init__(self, controls: Controls, history_path: Path | None, port: int = DEFAULT_PORT):
        self.controls = controls
        self.history_path = history_path
        self.port = port
        self.server: ThreadingHTTPServer | None = None
        self.stopped = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> str:
        """Serve in the background; returns the URL. A standalone dashboard on the port steps aside for a
        running Bol OS; anything else on it moves this one to the next free port."""
        if self.controls.running and ping(self.port) == "standalone":
            post(self.port, "/api/shutdown", {})
            deadline = time.monotonic() + 3.0
            while ping(self.port) is not None and time.monotonic() < deadline:
                time.sleep(0.1)
        handler = type("Handler", (_Handler,), {"dashboard": self})
        error = None
        for port in range(self.port, self.port + PORTS_TO_TRY):
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
                break
            except OSError as e:
                error = e
        if self.server is None:
            raise OSError(f"No free port for the dashboard from {self.port}: {error}")
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="dashboard").start()
        return self.url

    def close(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        self.stopped.set()

    # The API

    def state(self) -> dict:
        from bolos.audio import devices

        c = self.controls
        values = c.store.values()
        changed = c.store.changed()
        options = [{**asdict(o), "changed": o.key in changed} for o in c.store.options.values()]
        listed = {}
        for kind in ("input", "output"):
            try:
                listed[kind] = [{"name": d.name, "default": d.default} for d in devices(kind)]
            except Exception:
                log.warning("Could not list the %s devices", kind, exc_info=True)
                listed[kind] = []
        return {"running": c.running, "dry_run": c.dry_run, "values": values, "options": options,
                "devices": listed, "checks": [asdict(check) for check in c.checks()], "live": dict(c.live),
                "restart_needed": c.restart_needed(), "history": str(self.history_path or ""),
                "wake_model": WAKE_MODEL.exists()}

    def stats(self) -> dict:
        return summarize(read_entries(self.history_path))

    def archive_history(self) -> dict:
        path = self.history_path
        if path is None or not path.exists():
            return {"archived": None}
        target = path.with_name(f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
        path.replace(target)
        return {"archived": target.name}


class _Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard
    server_version = "BolOS"

    def log_message(self, format, *args):  # noqa: A002  (the base class's name)
        log.debug("dashboard: " + format, *args)

    def _host_ok(self) -> bool:
        port = self.dashboard.port
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802  (the base class's name)
        if not self._host_ok():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        path = self.path.split("?", 1)[0]
        d = self.dashboard
        try:
            if path in FILES:
                name, content_type = FILES[path]
                return self._send(HTTPStatus.OK, (STATIC / name).read_bytes(), content_type)
            if path == "/api/ping":
                return self._json({"app": APP, "running": d.controls.running})
            if path == "/api/state":
                return self._json(d.state())
            if path == "/api/level":
                return self._json(d.controls.level())
            if path == "/api/stats":
                return self._json(d.stats())
        except Exception:
            log.exception("Dashboard request failed: %s", path)
            return self._json({"error": "Something went wrong; see the Bol OS log"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._json({"error": "bad length"}, HTTPStatus.BAD_REQUEST)
        # On Windows, closing a socket with unread request bytes can reset it before the client sees the reply.
        self.connection.settimeout(2.0)
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            return self._json({"error": "request timed out"}, HTTPStatus.REQUEST_TIMEOUT)
        if not self._host_ok() or self.headers.get("X-Bolos") != "1":
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        if self.headers.get_content_type() != "application/json":
            return self._json({"error": "expected JSON"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        if len(raw) != length:
            return self._json({"error": "incomplete body"}, HTTPStatus.BAD_REQUEST)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "bad JSON"}, HTTPStatus.BAD_REQUEST)
        if not isinstance(body, dict):
            return self._json({"error": "bad JSON"}, HTTPStatus.BAD_REQUEST)
        d = self.dashboard
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/settings":
                changes = body.get("changes")
                if not isinstance(changes, dict):
                    raise SettingError("No changes")
                return self._json({"changed": d.controls.update(changes)})
            if path == "/api/reset":
                return self._json({"changed": d.controls.reset()})
            if path == "/api/test":
                return self._json({"playing": d.controls.test(str(body.get("what")))})
            if path == "/api/history/archive":
                return self._json(d.archive_history())
            if path == "/api/shutdown":
                if d.controls.running:
                    return self._json({"error": "Bol OS is running"}, HTTPStatus.CONFLICT)
                self._json({"ok": True})
                threading.Thread(target=d.close, daemon=True).start()
                return None
        except SettingError as e:
            return self._json({"error": str(e)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except Exception:
            log.exception("Dashboard request failed: %s", path)
            return self._json({"error": "Something went wrong; see the Bol OS log"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def ping(port: int, timeout: float = 0.5) -> str | None:
    """"running" or "standalone" when a Bol OS dashboard answers on this port, else None."""
    try:
        with urlopen(Request(f"http://127.0.0.1:{port}/api/ping"), timeout=timeout) as response:
            data = json.loads(response.read())
    except (OSError, URLError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("app") != APP:
        return None
    return "running" if data.get("running") else "standalone"


def post(port: int, path: str, body: dict, timeout: float = 2.0) -> dict | None:
    request = Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-Bolos": "1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, URLError, ValueError):
        return None
