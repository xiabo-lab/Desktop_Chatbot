"""The HTTP server behind the touchscreen.

One static page and four small JSON routes, served from a daemon thread inside
the assistant process. Same shape as AIA's, and for the same reasons: this
shares a Pi 5 with a wake recogniser, a speech recogniser, a person detector
and a music player, all of which want the same four cores, so the display costs
one thread and a poll.

**Loopback only, and think before changing it.** It serves a transcript of
everything said in the room, the weather at a named address, and a button that
turns on a camera. There is no authentication in front of any of it. The page
is opened in Chromium on the device itself; nothing needs to reach it from
elsewhere, and `ssh -L` covers the case that does.

**One route accepts input, and it accepts a name from a list.** AIA's UI is
strictly read-only and says so in its 405; this one has buttons on the screen,
which is section 23's requirement, so `POST /api/action` exists. Everything
that makes it safe is in `aipi5/ui/state.py`: the body is parsed for one field,
that field is checked against a tuple, and the result is a queued string. No
action in that tuple is destructive.

Failure here is never fatal. A port already in use, a missing page — logged,
and the assistant carries on listening, exactly as AIA does.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parent / "web" / "index.html"

# The most a client can pull in one request. The page asks again immediately
# when it gets a full page, so a browser that has been closed for hours
# catches up in batches rather than in one large response.
MAX_LIMIT = 500

# A POST body larger than this is not one of ours. `{"action":"camera"}` is
# nineteen bytes; the margin is for whitespace and a future field.
MAX_BODY = 1024


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "AIPI5"

    ui: "WebUI" = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:
        """Access logs to DEBUG.

        The page polls twice a second. At the default this would put ~170,000
        lines a day into the journal people read to find out why a turn failed.
        """
        log.debug("%s %s", self.address_string(), fmt % args)

    # ── responses ────────────────────────────────────────────────────

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("client went away mid-response")

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # ── routes ───────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        route = urlparse(self.path)
        params = parse_qs(route.query)

        if route.path in ("/", "/index.html"):
            self._page()
        elif route.path == "/api/state":
            self._state()
        elif route.path == "/api/feed":
            self._feed(params)
        elif route.path == "/api/system":
            self._system()
        elif route.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/action":
            self._json({"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._json({"error": "too large"}, 413)
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._json({"error": "expected JSON"}, 400)
            return

        action = str(payload.get("action", "")) if isinstance(payload, dict) else ""
        # The membership check is inside `UiState.request`, deliberately —
        # one place decides what an action is, and it is the same place that
        # holds the list.
        if self.ui.state.request(action):
            self._json({"ok": True, "action": action})
        else:
            self._json({"ok": False, "error": "not accepted"}, 400)

    def _page(self) -> None:
        body = self.ui.page()
        if body is None:
            self._json({"error": "the page is missing from this deployment"}, 500)
            return
        self._send(200, body, "text/html; charset=utf-8")

    def _state(self) -> None:
        payload = self.ui.state.snapshot()
        # The Pi's clock, not the browser's. The screensaver draws the time and
        # the two can disagree — a Chromium with no network time on a device
        # that has it, or the reverse.
        payload["now"] = time.time()
        self._json(payload)

    def _feed(self, params: dict) -> None:
        try:
            since = int(params.get("since", ["0"])[0])
        except ValueError:
            since = 0
        try:
            limit = int(params.get("limit", ["60"])[0])
        except ValueError:
            limit = 60
        limit = max(1, min(limit, MAX_LIMIT))

        self._json({
            "messages": self.ui.history.recent(since_id=since, limit=limit),
            "now": time.time(),
        })

    def _system(self) -> None:
        try:
            self._json(self.ui.info())
        except Exception:
            log.exception("could not build the system snapshot")
            self._json({"error": "system information is unavailable"}, 500)


class WebUI:
    """Owns the HTTP server thread.

    `info` is a callable rather than a snapshot because the settings page shows
    live things — whether the camera is running, whether the model answered,
    how many frames the detector has seen — and a value captured at
    construction would report the state of the world at boot forever.
    """

    def __init__(self, cfg, *, state, history, info):
        self.cfg = cfg
        self.state = state
        self.history = history
        self.info = info
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._page: bytes | None = None

    def page(self) -> bytes | None:
        """The single page, read once and held.

        One file at a fixed path. There is no static directory and no path
        joining anywhere in this module, so there is nothing for a crafted URL
        to traverse into.
        """
        if self._page is None:
            try:
                self._page = PAGE.read_bytes()
            except OSError as exc:
                log.error("could not read %s: %s", PAGE, exc)
                return None
        return self._page

    def start(self) -> bool:
        handler = type("_BoundHandler", (_Handler,), {"ui": self})
        try:
            self._server = ThreadingHTTPServer((self.cfg.host, self.cfg.port), handler)
        except OSError as exc:
            # Usually a previous instance that has not finished dying. Not a
            # reason to refuse to listen to anybody.
            log.warning("could not start the UI server on %s:%d — %s",
                        self.cfg.host, self.cfg.port, exc)
            self._server = None
            return False

        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="aipi5-web", daemon=True)
        self._thread.start()
        log.info("UI at %s", self.cfg.url)
        return True

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            log.debug("shutting down the UI server failed", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
