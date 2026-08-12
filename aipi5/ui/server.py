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

from aipi5.call import signaling as call_signaling

log = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parent / "web" / "index.html"

# The most a client can pull in one request. The page asks again immediately
# when it gets a full page, so a browser that has been closed for hours
# catches up in batches rather than in one large response.
MAX_LIMIT = 500

# A POST body larger than this is not one of ours. `{"action":"camera"}` is
# nineteen bytes; the margin is for whitespace and a future field.
MAX_BODY = 1024

# The call routes are the exception: an SDP offer for a video call with the
# codecs Chromium offers runs to several kilobytes, so they get their own,
# larger, still-bounded limit rather than raising MAX_BODY for everything.
CALL_MAX_BODY = 64 * 1024

# How fast the camera page's preview is refreshed.
#
# This is a budget, not a target, and the thing being budgeted is the camera
# lock rather than the network. One preview frame costs a read (~60 ms, of
# which most is waiting for a live frame) plus an encode, and the person
# detector wants that same lock twice a second. At 6 fps the preview holds it
# for roughly 40% of the time, which leaves the detector its 500 ms cadence
# with room to spare; at 15 it would start delaying presence, and presence
# arriving late is the screensaver lifting after somebody has already given up
# and walked away.
#
# It is also enough. This is a webcam pointed at a room on a 1280x800 panel —
# 6 fps looks live, and the alternative costs the thing the screen is for.
PREVIEW_FPS = 6

# A preview that nobody is watching is still a reader of the camera. Chromium
# keeps an <img> stream open as long as the element exists, so this is what
# stops a page left on the camera view overnight from reading the camera
# forever: the stream ends itself, and the page reconnects if it is still
# there. Long enough not to blink during a conversation with the camera.
PREVIEW_MAX_S = 300.0


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
        elif route.path == "/api/weather":
            self._weather(params)
        elif route.path == "/api/news":
            self._news(params)
        elif route.path == "/api/camera/stream":
            self._camera_stream()
        elif route.path == "/api/call/poll":
            self._call_poll(params)
        elif route.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        # The Pi's half of a call. No token on any of these, and that is the
        # same reasoning as everything else on this server: it is bound to
        # loopback, so the only thing that can reach it is a process on this
        # device — which for these routes is the kiosk Chromium showing the
        # assistant's own screen. The phone's half, which *is* reachable from
        # the network, is `aipi5/call/server.py` and authenticates every route.
        if path.startswith("/api/call/"):
            self._call_post(path)
            return
        if path not in ("/api/action", "/api/shutdown"):
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

        if not isinstance(payload, dict):
            payload = {}

        if path == "/api/shutdown":
            self._shutdown_post(payload)
            return

        action = str(payload.get("action", ""))
        # The membership check is inside `UiState.request`, deliberately —
        # one place decides what an action is, and it is the same place that
        # holds the list.
        if self.ui.state.request(action):
            self._json({"ok": True, "action": action})
        else:
            self._json({"ok": False, "error": "not accepted"}, 400)

    def _shutdown_post(self, payload: dict) -> None:
        """The screen's two words about a countdown it did not start.

        `showing` is the screen saying the numbers are in front of somebody,
        and it is what the shutdown waits for — see `ShutdownCountdown`, which
        refuses to power the device off without it. `cancel` is a touch.

        Not an entry in `ACTIONS`, deliberately. Everything in that list is a
        request for the assistant to *do* something and is rate limited as
        such; these two are answers about something already happening, one of
        which must never be delayed by a cooldown.
        """
        countdown = getattr(self.ui, "countdown", None)
        if countdown is None:
            self._json({"ok": False, "error": "no countdown"}, 404)
            return
        try:
            token = int(payload.get("token", 0))
        except (TypeError, ValueError):
            token = 0
        event = str(payload.get("event", ""))
        if event == "showing":
            ok = countdown.showing(token)
        elif event == "cancel":
            ok = countdown.cancel(token)
        else:
            self._json({"ok": False, "error": "unknown event"}, 400)
            return
        self._json({"ok": ok})

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

        messages = self.ui.history.recent(since_id=since, limit=limit)

        # `?roles=user,aia` is what the Talk page asks for. The transcript
        # holds everything the assistant said in the room, including the
        # summaries the weather and news pages speak — that is the 24-hour
        # record and it should not lie about what was audible — but those
        # carry their own role and the conversation view is a conversation,
        # not a log. Filtered here rather than in the page so the page does
        # not receive text it has decided not to show.
        # `cursor` is the highest id *considered*, which is not the highest id
        # returned once a filter has removed the tail of the batch. The page
        # advances on this rather than on the last message it was given —
        # otherwise a weather summary as the newest row would leave the Talk
        # page asking for the same rows forever, and never seeing anything
        # after them.
        cursor = max((m.get("id", 0) for m in messages), default=since)

        wanted = params.get("roles", [""])[0]
        if wanted:
            allowed = {role.strip() for role in wanted.split(",") if role.strip()}
            messages = [m for m in messages if m.get("role") in allowed]

        self._json({"messages": messages, "cursor": cursor, "now": time.time()})

    def _system(self) -> None:
        try:
            self._json(self.ui.info())
        except Exception:
            log.exception("could not build the system snapshot")
            self._json({"error": "system information is unavailable"}, 500)

    def _weather(self, params: dict) -> None:
        """Today's weather, for the weather page.

        Served from the same `WeatherService` the spoken answer uses, so the
        page and the sentence cannot disagree — and from its cache, so opening
        the page does not become a request to Open-Meteo every time somebody
        looks at it. `?force=1` is the pull-to-refresh case.
        """
        if self.ui.weather is None:
            self._json({"error": "weather is not configured"}, 503)
            return
        force = params.get("force", ["0"])[0] not in ("0", "", "false")
        try:
            weather = self.ui.weather.current(force=force)
        except Exception:
            log.exception("could not read the weather")
            weather = None
        if weather is None:
            # 200 with an explicit null rather than an error status: the page
            # has a "can't reach the weather" state to render, and a 503 would
            # send it down the network-failure path instead.
            self._json({"weather": None, "now": time.time()})
            return
        self._json({"weather": weather.as_dict(), "now": time.time()})

    def _news(self, params: dict) -> None:
        """Today's local stories, for the news page."""
        if self.ui.news is None:
            self._json({"error": "news is not configured"}, 503)
            return
        force = params.get("force", ["0"])[0] not in ("0", "", "false")
        try:
            stories = self.ui.news.as_dicts(force=force)
        except Exception:
            log.exception("could not read the news")
            stories = []
        self._json({"stories": stories, "now": time.time()})

    # ── the Pi's half of a call ──────────────────────────────────────

    def _call_poll(self, params: dict) -> None:
        """The held GET the call page waits on. See aipi5/call/signaling.py."""
        if self.ui.call is None:
            self._json({"error": "calling is not enabled"}, 503)
            return
        try:
            since = int(params.get("since", ["0"])[0])
        except ValueError:
            since = 0
        messages, cursor = self.ui.call.hub.collect(call_signaling.PI, since)
        self._json({"messages": messages, "cursor": cursor,
                    "call": self.ui.call.hub.snapshot()})

    def _call_post(self, path: str) -> None:
        """Answer, hang up, send a signalling message, or borrow the camera."""
        if self.ui.call is None:
            self._json({"error": "calling is not enabled"}, 503)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > CALL_MAX_BODY:
            self._json({"error": "too large"}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._json({"error": "expected JSON"}, 400)
            return
        if not isinstance(payload, dict):
            payload = {}

        hub = self.ui.call.hub
        session = str(payload.get("session", ""))

        if path == "/api/call/out":
            # The Pi ringing a phone. Loopback-only, like everything else on
            # this server, so the only thing that can start one is the screen
            # in front of the device — which is the point: a call out is
            # somebody at the Pi choosing to ring somebody, not a remote
            # request that could open a phone's microphone.
            phones = self.ui.call.subscriptions.names()
            device = str(payload.get("device", "")) or (phones[0] if phones else "")
            if not device:
                self._json({"ok": False,
                            "error": "no phone has registered for calls"}, 409)
                return
            started, session, why = hub.call_out(device)
            if not started:
                self._json({"ok": False, "error": why}, 409)
                return
            self.ui.on_call_change()
            sent, detail = self.ui.call.push.ring(device, {
                "type": "call", "session": session,
                "title": "AIPI5 is calling",
                "body": "Tap to answer",
            })
            if not sent:
                # The ring is still up — the phone may have the app open and
                # see it by polling — but say plainly that the notification did
                # not go, because "it rang and nothing happened" otherwise has
                # no explanation anywhere.
                log.warning("calling %s but the notification failed: %s",
                            device, detail)
            self._json({"ok": True, "session": session, "device": device,
                        "notified": sent, "detail": detail,
                        "ice_servers": self.ui.call.ice_servers("aipi5")})
        elif path == "/api/call/answer":
            # The camera is taken *here*, on the way to picking up, rather than
            # by the page before it asks. The order matters: `lend` is what
            # makes `getUserMedia` able to succeed, so answering before
            # borrowing would give Chromium a device this process still holds.
            if self.ui.camera is not None:
                self.ui.camera.lend("a video call")
            ok = hub.answer(session)
            self.ui.on_call_change()
            # The Pi needs the same relays the phone was given, or it gathers
            # only host candidates and a call across the Internet has nothing
            # on this side to pair with. Sent with the answer rather than
            # baked into the page because TURN credentials expire.
            self._json({"ok": ok, "call": hub.snapshot(),
                        "ice_servers": self.ui.call.ice_servers("aipi5")},
                       200 if ok else 409)
        elif path == "/api/call/send":
            message = payload.get("message")
            if not isinstance(message, dict):
                self._json({"error": "expected a message"}, 400)
                return
            kind = str(message.get("type", ""))
            if kind == "connected":
                hub.connected(session)
                self.ui.on_call_change()
            elif kind == "reconnecting":
                hub.reconnecting(session)
                self.ui.on_call_change()
            elif kind in ("route", "note", "audio"):
                # The page telling the journal something a person will need
                # later: which candidate type carried the media, or which
                # device it could not open. Logged rather than forwarded —
                # these are for whoever reads the log on this device, not for
                # the phone. Truncated because it is page-supplied text.
                log.info("call %s: %s", kind,
                         str(message.get("detail", ""))[:200])
                self._json({"ok": True})
                return
            hub.post(call_signaling.PHONE, message, session)
            self._json({"ok": True})
        elif path == "/api/call/bye":
            hub.hang_up(session, str(payload.get("reason", "")) or "the Pi hung up")
            self.ui.on_call_change()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def _camera_stream(self) -> None:
        """The live preview, as multipart JPEG.

        `multipart/x-mixed-replace` rather than a websocket or a frame-at-a-
        time poll, because it is what an `<img src>` understands natively:
        the page needs no decoding code, no reconnection logic beyond an
        `onerror`, and a frame that arrives late delays nothing but itself.

        This holds one thread for as long as somebody is watching, which is
        what `ThreadingHTTPServer` is for and is why the poll routes above are
        unaffected by it.
        """
        camera = self.ui.camera
        if camera is None or not camera.available():
            self._json({"error": "no camera"}, 503)
            return

        boundary = "aipi5frame"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        interval = 1.0 / PREVIEW_FPS
        deadline = time.monotonic() + PREVIEW_MAX_S
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                frame = camera.preview_jpeg()
                if frame is None:
                    # The camera went away mid-stream — unplugged, or the
                    # assistant shutting down. End the response rather than
                    # spinning; the page reconnects if it is still open.
                    break
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                # Sleep the remainder, so a slow frame does not turn the
                # cadence into the interval plus however long the camera took.
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError):
            # Navigating away from the camera page. The normal ending.
            log.debug("preview client went away")
        except OSError as exc:
            log.debug("preview stream ended: %s", exc)


class WebUI:
    """Owns the HTTP server thread.

    `info` is a callable rather than a snapshot because the settings page shows
    live things — whether the camera is running, whether the model answered,
    how many frames the detector has seen — and a value captured at
    construction would report the state of the world at boot forever.
    """

    def __init__(self, cfg, *, state, history, info,
                 weather=None, news=None, camera=None, call=None,
                 on_call_change=lambda: None, countdown=None):
        self.cfg = cfg
        self.state = state
        # The shutdown countdown, which this module only ever answers about:
        # it is started by the voice loop and drawn by the page.
        self.countdown = countdown
        self.history = history
        self.info = info
        # The call server, or None when calling is off. This module knows only
        # that it has a `hub`; the TLS listener the phone talks to is somewhere
        # else entirely and is never reached from here.
        self.call = call
        self.on_call_change = on_call_change
        # The three services the dedicated pages read directly. Passed in
        # rather than reached through the assistant, so this module still knows
        # nothing about the voice loop — and optional, so a deployment with the
        # camera disabled serves a page that says so rather than failing to
        # start a server.
        self.weather = weather
        self.news = news
        self.camera = camera
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
