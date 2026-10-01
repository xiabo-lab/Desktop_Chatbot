"""The HTTP server behind the touchscreen.

One static page, a small local asset tree, and JSON routes, served from a
daemon thread inside the assistant process. Same shape as AIA's, and for the
same reasons: this
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
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from aipi5.calendar import BirthdayError
from aipi5.core.screen_settings import ScheduleError
from aipi5.call import signaling as call_signaling
from aipi5.files import web as files_web
from aipi5.files.store import FileError
from aipi5.photos import qr
from aipi5.tools.advice import should_go_outside

log = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parent / "web" / "index.html"
ASSET_ROOT = PAGE.parent / "assets"

# The Boxing and Yoga artwork is active. The MediaPipe files are retained from
# the former palm-click control for provenance but are no longer loaded; the
# replacement crossed-arms gesture comes from the Hailo pose stream. Keep this
# deliberately small rather than growing a general static server here.
ASSET_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".task": "application/octet-stream",
    ".wasm": "application/wasm",
    ".vrm": "model/gltf-binary",
    ".html": "text/html; charset=utf-8",
}


def asset_file(request_path: str) -> Path | None:
    """Resolve one ``/assets/`` URL without allowing it to leave the folder.

    URL decoding happens before the containment check, so both literal and
    percent-encoded ``..`` are refused.  Backslashes are separators on the
    development machine and ordinary filename characters on the Pi; resolving
    through ``Path`` makes the safe answer correct on both.
    """
    if not request_path.startswith("/assets/"):
        return None
    relative = unquote(request_path.removeprefix("/assets/"))
    try:
        candidate = (ASSET_ROOT / relative).resolve()
        candidate.relative_to(ASSET_ROOT.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None

# The most a client can pull in one request. The page asks again immediately
# when it gets a full page, so a browser that has been closed for hours
# catches up in batches rather than in one large response.
MAX_LIMIT = 500

# A POST body larger than this is not one of ours. `{"action":"camera"}` is
# nineteen bytes; the margin is for whitespace and a future field.
MAX_BODY = 1024
CALENDAR_MAX_BODY = 4096

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
#: The gesture reader's stream. Thirty is the camera's own rate on two
#: buffers with a continuous reader, measured -- see `aipi5/agent/hands.py`,
#: which explains why the assistant's own preview cannot go near it.
#: How long a write to a hand-feed viewer may block before the stream is
#: dropped. Generous next to a frame every 33 ms, and the only thing standing
#: between an abandoned stream and a thread held for the life of the process.
HAND_WRITE_TIMEOUT_S = 5.0

#: How long one hand-feed response may run. An hour: the stream is meant to
#: last as long as the browser is open, and an ending is a risk rather than
#: hygiene. Abandoned streams are dealt with by `HAND_WRITE_TIMEOUT_S`, which
#: is what a cap was standing in for.
HAND_STREAM_MAX_S = 3600.0

HAND_FPS = 30
HAND_FPS_MAX = 60
HAND_WIDTH = 480

#: The actions that need the camera, and so cannot run while a hand has it.
#: Games are not here: they ask for the camera through `CameraLease`, which
#: already refuses with the borrower's name, and their page is worth opening
#: to read the scores whether or not one can be started.
CAMERA_ACTIONS = frozenset({"camera", "call"})

#: What the kiosk's agent console may send. Three, and the shape of the list
#: matters more than its length: it is what the *page* may originate, not what
#: the runtime understands.
#:
#: `agent.gesture` is absent because it has its own route with its own bounds,
#: and `agent.open` is absent because it is the assistant's message about a
#: page somebody asked for out loud — a browser opened from a text box would
#: be a URL bar with a language model in it.
AGENT_MESSAGES = frozenset({"agent.ask", "agent.stop", "agent.answer"})

#: How long `/api/assistant/events` holds a GET open. Matched to the agent's
#: own long poll and comfortably inside `_Handler.timeout`.
ASSISTANT_POLL_S = 20.0

#: The longest request the compose box may send. The agent enforces its own
#: bound on the far side; this one stops a body being parsed at all.
MAX_ASK = 4000

#: How many model requests this server will start in a minute, across every
#: caller. Not a security control — `_same_origin` is what keeps a web page
#: out — but the backstop for the case that one gets past it or a script on
#: this device runs away: every one of these costs money at OpenAI and holds
#: the turn lock, so a loop is a bill and a device that will not answer anybody
#: standing in front of it. Twenty a minute is far more than a person can say
#: and far less than a loop can send.
ASK_LIMIT = 20
ASK_WINDOW_S = 60.0

PREVIEW_FPS = 6

#: As fast as `/api/camera/stream` may be asked to go. Hand tracking wants
#: twelve; beyond that the JPEG encode starts competing with inference for a
#: core, and the camera itself is configured for fifteen.
PREVIEW_FPS_MAX = 15

# How fast the game page is sent state.
#
# Matched to the pose rate rather than to the display: the simulation only
# moves when a pose frame arrives, so sending faster would be the same
# snapshot twice and sending slower would throw away hand positions that were
# paid for. The page draws at 60 by extrapolating between these — section 24.
GAME_STREAM_FPS = 30

# A game stream is bounded like the camera preview is, and for the same reason:
# an <img> or an EventSource left open by a page nobody is looking at is still
# a thread and still a reader. Long, because the bound that actually matters is
# the manager's own idle watchdog, and a stream that dropped mid-game would be
# far more annoying than one that outlives its usefulness by a few minutes.
GAME_STREAM_MAX_S = 1800.0

# The start screen's preview. Much slower than the camera page's 6 fps: this is
# a "yes, that is you" check while somebody positions themselves in front of
# the camera, not something being watched for motion, and every frame is a JPEG
# encode competing with pose inference for a core.
GAME_PREVIEW_FPS = 5
GAME_PREVIEW_MAX_S = 600.0

# What the preview may be raised to, by a page that asks for it.
#
# Yoga Coach is the reason this is a range rather than a number. Its live feed
# is not a "yes, that is you" check on a start screen — it is the only way the
# player can see what they are correcting while they are two metres away from
# the screen, and five frames a second reads as a broken camera rather than as
# a mirror. Twelve is where it starts looking live.
#
# Still a budget rather than a target, and the budget is a core: an encode of
# a 560-pixel-wide frame costs a few milliseconds, so fifteen of them is a few
# percent of one of the Pi's four. Capped here rather than trusted from the
# query string, because a page — or a curl — asking for 200 would take a core
# away from the inference this game is built on.
GAME_PREVIEW_FPS_MAX = 15
GAME_PREVIEW_WIDTH = 480
GAME_PREVIEW_WIDTH_MAX = 720

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

    def _same_origin(self, json_body: bool = True) -> bool:
        """Whether this POST came from the assistant's own page.

        **Loopback is not an authentication boundary here, and this server had
        been treating it as one.** The reasoning was that only a process on
        this device can reach 127.0.0.1 — true, and beside the point, because
        the kiosk Chromium is a process on this device and this device
        deliberately opens arbitrary websites. `open_known_app` puts YouTube on
        the screen and the agent's browser can be sent anywhere.

        A page cannot *read* a cross-origin response, which is what people
        remember about the same-origin policy. It can still send the request,
        and every route here is a side effect: `/api/shutdown` powers the
        machine off, `/api/files/delete` removes a file, `/api/assistant/ask`
        spends money at OpenAI. None of them needs its reply to do harm.

        Three checks, cheap and in order of how much they can be trusted:

        - `Sec-Fetch-Site`, which Chromium sets itself and a page cannot forge.
          `same-origin` and `none` (a typed address, a bookmark) pass.
        - `Origin`, sent on every cross-origin POST and, in current browsers,
          on same-origin ones too. It must be one of ours.
        - `Content-Type`, which must be JSON. A request a page can send without
          a CORS preflight is limited to a few content types, and JSON is not
          among them — so requiring it means a cross-origin caller has to ask
          permission first, and this server never grants it.

        What this deliberately is **not** is a per-process token. A token would
        have to reach the page somehow, and the page is served from this same
        loopback port to anything that asks — so any local process able to
        forge these headers can also fetch the token, and any process that
        cannot forge them is already stopped. It would be ceremony rather than
        a boundary, and the honest statement is that a *non-browser* process
        running as this user is not defended against here and cannot be: it
        can reach the microphone and the camera directly.
        """
        site = self.headers.get("Sec-Fetch-Site", "")
        if site and site not in ("same-origin", "none"):
            log.warning("refused a %s request to %s", site, self.path)
            return False

        origin = self.headers.get("Origin", "")
        if origin and origin not in self._own_origins():
            log.warning("refused a request to %s from %s", self.path, origin)
            return False

        kind = (self.headers.get("Content-Type", "") or "").split(";")[0].strip()
        if json_body and kind and kind != "application/json":
            # Empty is allowed: `fetch` with no body sends no content type, and
            # several of these routes take none.
            log.warning("refused a %s request to %s", kind or "typeless",
                        self.path)
            return False
        return True

    def _own_origins(self) -> set[str]:
        """The origins the assistant's own page is served from.

        Both spellings of loopback and the port actually bound, because the
        page is opened as `127.0.0.1` by the kiosk and as `localhost` by
        anybody debugging with an ssh tunnel.
        """
        try:
            port = self.server.server_address[1]
        except (AttributeError, IndexError):
            port = 8092
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                f"http://[::1]:{port}"}

    def _drain(self) -> None:
        """Read a request body that is about to be refused, and discard it.

        In bounded chunks, so a caller announcing a gigabyte cannot make this
        allocate one — the point is to leave the connection synchronised, not
        to keep what was sent.
        """
        try:
            remaining = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 8192))
            if not chunk:
                return
            remaining -= len(chunk)

    # ── routes ───────────────────────────────────────────────────────

    def do_GET(self) -> None:
        route = urlparse(self.path)
        params = parse_qs(route.query)

        if route.path in ("/", "/index.html"):
            self._page()
        elif route.path.startswith("/assets/"):
            self._asset(route.path)
        elif route.path == "/api/state":
            self._state()
        elif route.path == "/api/feed":
            self._feed(params)
        elif route.path == "/api/system":
            self._system()
        elif route.path == "/api/volume":
            self._volume()
        elif route.path == "/api/weather":
            self._weather(params)
        elif route.path == "/api/calendar/birthdays":
            self._calendar_birthdays()
        elif route.path == "/api/news":
            self._news(params)
        elif route.path == "/api/camera/stream":
            self._camera_stream(params)
        elif route.path == "/api/hand/stream":
            self._hand_stream(params)
        elif route.path == "/api/camera/capture":
            self._camera_capture()
        elif route.path == "/api/assistant/events":
            self._assistant_events(params)
        elif route.path == "/api/agent/poll":
            self._agent_poll(params)
        elif route.path == "/api/call/poll":
            self._call_poll(params)
        elif route.path == "/api/files":
            self._file_list(params)
        elif route.path.startswith("/api/files/download/"):
            self._file_download(route.path, params)
        elif route.path == "/api/photos":
            self._photo_list()
        elif route.path.startswith("/api/photos/file/"):
            self._photo_file(route.path)
        elif route.path == "/api/photos/qr":
            self._photo_qr()
        elif route.path == "/api/games":
            self._games()
        elif route.path == "/api/game/status":
            self._game_status()
        elif route.path == "/api/game/stream":
            self._game_stream()
        elif route.path == "/api/game/hardware":
            self._game_hardware()
        elif route.path == "/api/game/settings":
            self._game_settings()
        elif route.path == "/api/game/preview":
            self._game_preview(params)
        elif route.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "not found"}, 404)

    def _asset(self, request_path: str) -> None:
        """Serve a fixed local game asset; never a file outside ``assets``."""
        path = asset_file(request_path)
        kind = ASSET_TYPES.get(path.suffix.lower(), "") if path else ""
        if path is None or not kind:
            self._json({"error": "not found"}, 404)
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self._send(200, body, kind)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # Before anything reads a body or acts on one. Every route below is a
        # side effect, and a page on this device's own screen can send a
        # request here even though it cannot read the reply — see
        # `_same_origin`. The upload is exempt from the JSON rule only: it is
        # multipart by design.
        if not self._same_origin(json_body=(path != "/api/files/upload")):
            if path != "/api/files/upload":
                # Not on an upload: the body may be a large file and this
                # refusal must not read it.
                self._drain()
            self._json({"error": "that request did not come from this screen"},
                       403)
            return
        # An upload is the one body this server must not buffer. It is streamed to disk in
        # `aipi5/files/web.py` rather than read whole in order to be parsed.
        if path == "/api/files/upload":
            self._file_upload()
            return
        # The Pi's half of a call. No token on any of these, and that is the
        # same reasoning as everything else on this server: it is bound to
        # loopback, so the only thing that can reach it is a process on this
        # device — which for these routes is the kiosk Chromium showing the
        # assistant's own screen. The phone's half, which *is* reachable from
        # the network, is `aipi5/call/server.py` and authenticates every route.
        if path.startswith("/api/call/"):
            self._call_post(path)
            return
        if path not in ("/api/action", "/api/shutdown", "/api/files/delete",
                        "/api/photos", "/api/game/open", "/api/game/close",
                        "/api/game/command", "/api/game/debug",
                        "/api/game/settings", "/api/agent/gesture",
                        "/api/agent/say", "/api/agent/dictate",
                        "/api/assistant/ask", "/api/assistant/cancel",
                        "/api/assistant/approval",
                        "/api/hand/debug", "/api/hand/pause",
                        "/api/calendar/birthdays", "/api/volume",
                        "/api/screensaver", "/api/display/wake"):
            # Drained before it is refused. The 404 is written and the socket
            # closed, and a body still sitting in the kernel's receive queue at
            # that moment is an RST on Windows and a reset connection on the
            # caller — so a page posting to a route that has been renamed sees
            # "the assistant is not answering" instead of "not found", which
            # sends whoever is debugging it to look at the wrong thing
            # entirely. `aipi5/call/server.py` learned this first; see
            # `_read_body` there.
            self._drain()
            self._json({"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        maximum = CALENDAR_MAX_BODY if path == "/api/calendar/birthdays" else MAX_BODY
        if length > maximum:
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

        if path == "/api/files/delete":
            self._file_delete(payload)
            return

        if path == "/api/photos":
            self._photo_post(payload)
            return

        if path == "/api/calendar/birthdays":
            self._calendar_birthdays_post(payload)
            return

        if path == "/api/volume":
            self._volume_post(payload)
            return

        if path == "/api/screensaver":
            self._screensaver_post(payload)
            return

        if path == "/api/display/wake":
            # Activity only. In particular this does not enter UiState's
            # action queue: the first touch on a dark panel must power the
            # output on and reset the idle stages without pretending the user
            # said the wake word or starting a microphone turn.
            self.ui.on_wake("touchscreen activity")
            self._json({"ok": True})
            return

        if path.startswith("/api/game/"):
            self._game_post(path, payload)
            return

        if path.startswith("/api/assistant/"):
            self._assistant_post(path, payload)
            return

        if path == "/api/agent/gesture":
            self._gesture_post(payload)
            return

        if path == "/api/agent/say":
            self._agent_say(payload)
            return

        if path == "/api/agent/dictate":
            self._dictate_post(payload)
            return

        if path == "/api/hand/debug":
            self._hand_debug(payload)
            return

        if path == "/api/hand/pause":
            feed = getattr(self.ui, "hands", None)
            if feed is not None:
                feed.set_paused(bool(payload.get("paused")))
            self._json({"ok": True, "paused": bool(payload.get("paused"))})
            return

        action = str(payload.get("action", ""))
        # **Refused here as well as greyed in the page.** The button is the
        # polite half; this is the half that holds when a request arrives from
        # somewhere else, and it gives the reason rather than letting the
        # camera fail somewhere further down where the message would be about
        # a device rather than about what is going on.
        if action in CAMERA_ACTIONS:
            feed = getattr(self.ui, "hands", None)
            if feed is not None and feed.active:
                self._json({"ok": False, "busy": "hand control",
                            "error": "a hand is driving the browser, which "
                                     "needs the camera. Close the browser to "
                                     "use this again."}, 409)
                return
        # The membership check is inside `UiState.request`, deliberately —
        # one place decides what an action is, and it is the same place that
        # holds the list.
        if self.ui.state.request(action):
            if action == "wake":
                # Here rather than only where the voice loop consumes the queued
                # action, and the reason is the camera. This is the Talk page's
                # explicit Listen button; ordinary touchscreen activity uses
                # `/api/display/wake` and never enters this queue.
                #
                # Idempotent with the voice loop's own `suppress`, which still
                # runs when the action is dequeued.
                self.ui.on_wake("the Listen button")
            self._json({"ok": True, "action": action})
        else:
            self._json({"ok": False, "error": "not accepted"}, 400)

    # ── files ────────────────────────────────────────────────────────
    #
    # The same folder the phone reaches through `aipi5/call/server.py`, and the
    # same code underneath. What is missing here is the authentication, and
    # that is the whole difference between the two servers: this one is bound
    # to loopback, so the only thing that can reach it is a process on this
    # device — the kiosk Chromium showing the assistant's own screen. There is
    # no token to check because there is no network to check it against.
    #
    # No download ticket either, for the same reason. Tickets exist because
    # Safari cannot put a bearer token on a link; the screen here has nothing
    # to prove, so `/api/files/download/<name>` is the plain thing it looks
    # like. The name still goes through `FileStore.resolve`, which is what
    # keeps it inside the folder.

    def _files(self):
        """The store, or None having answered."""
        store = getattr(self.ui, "files", None)
        if store is None or not store.ready:
            self._json({"error": (store.error if store else "")
                        or "file transfer is not available"}, 503)
            return None
        return store

    def _file_list(self, params: dict) -> None:
        store = self._files()
        if store is None:
            return
        sort = (params.get("sort") or ["date"])[0]
        ascending = (params.get("order") or ["desc"])[0] == "asc"
        self._json(files_web.payload(store, sort, ascending))

    def _file_download(self, path: str, params: dict | None = None) -> None:
        store = self._files()
        if store is None:
            return
        name = files_web.name_from_path(path, "/api/files/download/")
        # `?inline=1` is how the screen looks at a photo without leaving the
        # page. Only pictures, video and sound are ever honoured — see
        # `may_show_inline`, which is where that decision is made and not here.
        inline = ((params or {}).get("inline") or ["0"])[0] == "1"
        try:
            files_web.send_file(self, store, name, who="the screen",
                                inline=inline)
        except FileError as exc:
            self._json({"error": str(exc)}, exc.status)

    def _file_delete(self, payload: dict) -> None:
        store = self._files()
        if store is None:
            return
        try:
            gone = store.delete(str(payload.get("name", "")))
        except FileError as exc:
            self._json({"error": str(exc)}, exc.status)
            return
        self._json({"ok": True, "name": gone})

    def _file_upload(self) -> None:
        store = getattr(self.ui, "files", None)
        if store is None or not store.ready:
            files_web.refuse_upload(
                self, 503,
                (store.error if store else "") or "file transfer is not available")
            return
        try:
            self._json(files_web.receive_upload(self, store, who="the screen"))
        except FileError as exc:
            files_web.refuse_upload(self, exc.status, str(exc))
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    # ── AI Motion games ──────────────────────────────────────────────
    #
    # Section 35's endpoints, in this server's shape rather than the shape the
    # requirement sketched: `POST /api/game/command {"action": "pause"}` rather
    # than `POST /api/game/fruit-ninja/pause`, because a path segment per verb
    # per game multiplies, and because everything else that this screen asks
    # the assistant to *do* already posts a name from a list into one route.
    #
    # The interesting one is `/api/game/stream`. Everything else on this server
    # is polled twice a second and could stay that way; a game cannot. Sending
    # thirty snapshots a second down this server's HTTP/1.0 connections would
    # be thirty TCP handshakes a second, so the game state goes out over
    # server-sent events instead — one connection, held open, the same shape as
    # the camera preview above and for the same reason.

    def _game(self):
        """The manager, or None having answered."""
        games = getattr(self.ui, "games", None)
        if games is None:
            self._json({"error": "games are turned off in the configuration"},
                       503)
            return None
        return games

    def _calendar_birthdays(self) -> None:
        """The reboot-safe birthday list; calendar arithmetic stays in-browser."""
        store = getattr(self.ui, "birthdays", None)
        if store is None:
            self._json({"birthdays": []})
            return
        self._json({"birthdays": store.list()})

    def _calendar_birthdays_post(self, payload: dict) -> None:
        store = getattr(self.ui, "birthdays", None)
        if store is None:
            self._json({"ok": False, "error": "birthday storage is unavailable"}, 503)
            return
        try:
            action = str(payload.get("action", "save"))
            if action == "save":
                birthday = payload.get("birthday")
                if not isinstance(birthday, dict):
                    raise BirthdayError("birthday is required")
                saved = store.save(birthday)
                self._json({"ok": True, "birthday": saved,
                            "birthdays": store.list()})
            elif action == "delete":
                store.delete(str(payload.get("id", "")))
                self._json({"ok": True, "birthdays": store.list()})
            else:
                raise BirthdayError("unknown birthday action")
        except BirthdayError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)

    def _games(self) -> None:
        games = self._game()
        if games is None:
            return
        self._json({"games": games.catalogue()})

    def _game_status(self) -> None:
        games = self._game()
        if games is None:
            return
        self._json(games.status())

    def _game_hardware(self) -> None:
        """Section 44: proof that the AI HAT+ 2 is the thing doing the work."""
        games = self._game()
        if games is None:
            return
        self._json(games.hardware())

    def _game_settings(self) -> None:
        """The small, persistent set of choices shown under Settings."""
        games = self._game()
        if games is None:
            return
        self._json(games.settings())

    def _hand_debug(self, payload: dict) -> None:
        """One line a second about what the gesture reader is seeing.

        Logged rather than answered, because the reader is in a browser and
        the person watching it is on the far end of an ssh session. Only ever
        posted while hand control is running, which is only while the agent has
        a page on the screen.
        """
        def _n(key, digits=0):
            try:
                value = float(payload.get(key, 0))
            except (TypeError, ValueError):
                return 0
            return round(value, digits) if digits else int(value)

        log.info(
            "HAND n=%d infer=%dms gap=%dms hand=%d%% palm=%d fist=%d "
            "other=%d ready=%d unused=%ds dead=%dms travel=%.2f/%.2f speed=%.2f armed=%d "
            "trail=%d fired=%s rtt=%dms recog=%s blocked=%d stuck=%d still=%d jitter=%.4f same=%d edge=%d",
            _n("n"), _n("infer"), _n("gap"), _n("handpct"), _n("palm"),
            _n("fist"), _n("other"), _n("ready"), _n("unused"), _n("dead"), _n("dx", 2), _n("dy", 2),
            _n("speed", 2), _n("armed"), _n("trail"),
            str(payload.get("fired") or "-")[:60], _n("rtt"),
            str(payload.get("recog") or "?")[:100],
            _n("blocked"), _n("stuck"), _n("skipped"),
            _n("jitter", 4), _n("same"), _n("edge"))
        self._json({"ok": True})

    def _gesture_post(self, payload: dict) -> None:
        """The person's hand, on its way to the agent's browser.

        This is the only route on this server that reaches the agent, and it
        forwards exactly one thing: the *name* of a gesture, from a fixed list
        the runtime holds. Not a coordinate to click without a gesture, not an
        operation name, not a URL. The page decides what a hand did; it does
        not decide what that means.

        No token, like everything else here — the server is on loopback and
        the only caller is the kiosk browser on this device. What that browser
        can ask for is bounded by the list on the other end, which is why the
        list lives there and not in the JavaScript.
        """
        if self.ui.agent is None:
            self._json({"ok": False, "detail": "the agent is not installed"},
                       503)
            return
        at = payload.get("at")
        status, answer = self.ui.agent.say(
            {"type": "agent.gesture",
             "gesture": str(payload.get("gesture", ""))[:32],
             "at": at if isinstance(at, dict) else None})
        self._json(answer if answer else {"ok": False},
                   200 if status == 200 else status)

    # ── the agent console, on the screen as well as on the phone ────
    #
    # The console was the phone's alone by decision, not by accident: the
    # panel has no keyboard, and a chat surface nobody can type into is worse
    # than no chat surface. Dictation is what changed that — see
    # `aipi5/ui/dictation.py`.
    #
    # These three forward and nothing more, the way `_gesture_post` does. What
    # a caller may ask the agent for is bounded by the message types the
    # runtime knows, which is why that list lives there and not here.

    # ── the unified assistant contract ───────────────────────────────
    #
    # Four routes, one coordinator, and the same tools and the same policy
    # behind all of them — which is the whole point. What used to happen is
    # that a sentence typed here became `agent.ask` and started a maintenance
    # run with a 24-step budget, while the same sentence said out loud went to
    # the short tool loop. Two answers to one question, and no way for anybody
    # to know which they were going to get.
    #
    # **`/api/agent/poll` and `/api/agent/say` stay** as compatibility
    # wrappers, because the phone (`aipi5/call/web/phone.html`) is still on
    # them. They are the same mailbox seen at a different level, so a page must
    # read one or the other: a page that polls both draws every delegated row
    # twice. The same is true of `/api/feed`, which carries spoken turns that
    # `/api/assistant/events` also carries.

    def _assistant_events(self, params: dict) -> None:
        """Hold a GET until the transcript has something new, or ~20 s passes.

        Long-polled for the reason the agent's own poll is: a delegated run
        posts a dozen rows in a few seconds, and a timer either shows them late
        or asks constantly while nothing is happening.
        """
        coordinator = getattr(self.ui, "coordinator", None)
        if coordinator is None:
            self._json({"error": "this build has no assistant coordinator"}, 503)
            return
        try:
            since = int(params.get("since", ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        # `wait=0` asks for whatever is there and the snapshot, without
        # holding. A page opening wants both at once — on a device that has
        # been idle since breakfast the transcript is empty, and the default
        # long poll would leave the panel blank for twenty seconds before it
        # could draw so much as the run state.
        try:
            wait = float(params.get("wait", [ASSISTANT_POLL_S])[0])
        except (TypeError, ValueError):
            wait = ASSISTANT_POLL_S
        wait = max(0.0, min(wait, ASSISTANT_POLL_S))
        events, cursor = coordinator.collect(since, wait)
        self._json({"events": events, "cursor": cursor,
                    "assistant": coordinator.snapshot()})

    def _assistant_post(self, path: str, payload: dict) -> None:
        """Ask, stop, or answer an approval.

        Approvals included, which is the decision the agent console already
        made: anyone who can touch this panel can approve a settings or a
        source change, because that is consistent with what a touch already
        means here — the screen reaches the settings page and a shutdown
        countdown — and the alternative was a run started at the screen that
        stops halfway until somebody finds a phone.
        """
        coordinator = getattr(self.ui, "coordinator", None)
        if coordinator is None:
            self._json({"error": "this build has no assistant coordinator"}, 503)
            return

        if path == "/api/assistant/ask":
            if not self.ui.may_ask():
                self._json({"error": "too many requests in a row; wait a "
                                     "moment and ask again"}, 429)
                return
            # `source` says where it was typed, not who typed it, and it is
            # normalised by the coordinator rather than trusted — a caller
            # inventing one must not lose somebody's sentence.
            self._json(coordinator.submit_text(
                str(payload.get("text", ""))[:MAX_ASK],
                str(payload.get("source", "text")),
                # None rather than "en" when the caller did not say. The panel
                # does not know what somebody typed, and guessing English
                # answers 音量调到二十 in English after carrying it out.
                (str(payload.get("language"))[:8]
                 if payload.get("language") else None)))
        elif path == "/api/assistant/cancel":
            self._json(coordinator.cancel(str(payload.get("run", ""))[:64]))
        elif path == "/api/assistant/approval":
            # `allow` is coerced to a boolean here and again in the
            # coordinator. Silence, a timeout, an unclear answer and a stale
            # token all mean no, and none of that is decided on this side —
            # the desk in `aipi5-agent.service` binds an answer to the token it
            # issued.
            self._json(coordinator.answer_approval(
                str(payload.get("token", ""))[:128], bool(payload.get("allow"))))
        else:
            self._json({"error": "not found"}, 404)

    def _agent_poll(self, params: dict) -> None:
        """Hold a GET until the agent has something to say, or ~20 s passes.

        Long-polled rather than ticked, for the reason the phone's console is:
        a run posts a dozen events in a few seconds and a timer either shows
        them late or asks constantly while nothing is happening. The cursor is
        the whole of the reconnection story — the mailbox lives in
        `aipi5-agent.service`, which is *not* the service the agent is most
        often asked to restart, so this page can lose its connection, ask
        again from the cursor it had, and be handed what it missed.
        """
        if self._agent_missing():
            return
        try:
            since = int(params.get("since", ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        status, payload = self.ui.agent.poll(since)
        self._json(payload if payload else {"error": "the agent is not answering"},
                   200 if status == 200 else status)

    def _agent_say(self, payload: dict) -> None:
        """Ask, stop, or answer an approval.

        **Approvals included, which is a decision rather than an oversight.**
        Anyone who can touch this panel can now approve a settings or a source
        change. That is consistent with what a touch already means here — the
        screen reaches the settings page and a shutdown countdown — and the
        alternative was a run started at the screen that stops halfway until
        somebody finds a phone.

        The type is passed through rather than reconstructed: the runtime
        refuses one it does not know, and duplicating that list here would be
        a second place for it to be wrong.
        """
        if self._agent_missing():
            return
        kind = str(payload.get("type", ""))
        if kind not in AGENT_MESSAGES:
            # Not the runtime's job to explain a message the *page* should
            # never have sent. `agent.gesture` has its own route with its own
            # bounds, and `agent.open` is the assistant's, not the page's.
            self._json({"ok": False, "error": f"unknown message {kind!r}"}, 400)
            return
        status, answer = self.ui.agent.say(payload)
        self._json(answer if answer else {"ok": False},
                   200 if status == 200 else status)

    def _dictate_post(self, payload: dict) -> None:
        """Capture one utterance and answer with the words.

        Blocks this HTTP thread for as long as somebody is speaking, which is
        why the server is threaded and why the microphone is never touched
        here: the voice loop does the work and fills the answer in. See
        `aipi5/ui/dictation.py`.
        """
        dictation = getattr(self.ui, "dictation", None)
        if dictation is None:
            self._json({"ok": False,
                        "error": "this build cannot listen for text"}, 503)
            return
        self._json(dictation.ask().as_dict())

    def _agent_missing(self) -> bool:
        if self.ui.agent is not None:
            return False
        self._json({"error": "the agent is not installed on this device"}, 503)
        return True

    def _game_post(self, path: str, payload: dict) -> None:
        games = self._game()
        if games is None:
            return

        from aipi5.games.manager import GameError

        try:
            if path == "/api/game/open":
                self._json(games.open(str(payload.get("game", ""))))
            elif path == "/api/game/close":
                self._json(games.close())
            elif path == "/api/game/command":
                self._json(games.command(str(payload.get("action", ""))))
            elif path == "/api/game/debug":
                games.set_debug(bool(payload.get("on")))
                self._json({"ok": True, "debug": games.debug})
            elif path == "/api/game/settings":
                # One field per request, the way the page sends them: a tap on
                # the difficulty must not restate the round length and race a
                # tap somebody made a moment earlier.
                if "difficulty" in payload:
                    self._json(games.set_difficulty(payload.get("difficulty")))
                elif "sound" in payload:
                    self._json(games.set_sound(payload.get("sound")))
                else:
                    self._json(
                        games.set_round_seconds(payload.get("round_seconds")))
            else:
                self._json({"error": "not found"}, 404)
        except GameError as exc:
            # 409 rather than 500: every one of these is a thing the player can
            # do something about — the camera is busy, the HAT is missing,
            # nobody is standing in front of it — and the page turns the
            # message into the screens section 41 asks for.
            self._json({"error": str(exc), "ok": False}, 409)

    @staticmethod
    def _bounded(params: dict, name: str, fallback: int,
                 low: int, high: int) -> int:
        """One integer from a query string, or the default. Never outside."""
        try:
            value = int(params.get(name, [fallback])[0])
        except (TypeError, ValueError):
            return fallback
        return max(low, min(high, value))

    def _game_preview(self, params: dict | None = None) -> None:
        """The start screen's camera preview, from the game's own capture.

        A separate route from `/api/camera/stream` because that one reads
        `Camera`, which is *lent* for as long as a game is open and correctly
        answers 503 while it is. Pointing the start screen at it produced a
        broken image on the one screen whose whole job is to show somebody
        that the camera can see them — section 20.

        Slower than the camera page's preview on purpose. Nothing here is
        being watched for motion; it is a "yes, that is you" check while
        somebody positions themselves, and every frame is an encode competing
        with inference for a core.
        """
        games = self._game()
        if games is None:
            return
        if games.preview_jpeg() is None:
            self._json({"error": "no game is running"}, 503)
            return

        boundary = "aipi5motion"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        params = params or {}
        rate = self._bounded(params, "fps", GAME_PREVIEW_FPS,
                             1, GAME_PREVIEW_FPS_MAX)
        width = self._bounded(params, "w", GAME_PREVIEW_WIDTH,
                              240, GAME_PREVIEW_WIDTH_MAX)
        interval = 1.0 / rate
        deadline = time.monotonic() + GAME_PREVIEW_MAX_S
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                frame = games.preview_jpeg(width)
                if frame is None:
                    # The game closed, or the camera went away. End the
                    # response rather than spinning; the page reconnects if it
                    # is still showing the start screen.
                    break
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError):
            log.debug("game preview client went away")
        except OSError as exc:
            log.debug("game preview ended: %s", exc)

    def _game_stream(self) -> None:
        """Game and pose state as server-sent events, at the pose rate.

        Holds one thread for as long as the game page is open, which is what
        `ThreadingHTTPServer` is for and is the same trade the camera preview
        makes. Ends itself when the game does, so a page left on a finished
        game is not still holding a connection an hour later.
        """
        games = self._game()
        if games is None:
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        interval = 1.0 / GAME_STREAM_FPS
        deadline = time.monotonic() + GAME_STREAM_MAX_S
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                # `seen=True` is the liveness signal the manager's watchdog
                # reads: a page holding this stream open is a page that still
                # has somebody in front of it, so the camera is not taken back.
                payload = games.status(seen=True)
                body = json.dumps(payload, ensure_ascii=False)
                self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
                if not payload.get("active"):
                    # The game was closed by something else. Ending the stream
                    # rather than sending "nothing is running" thirty times a
                    # second until the page notices.
                    break
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError):
            # Navigating away from the game page. The normal ending.
            log.debug("game stream client went away")
        except OSError as exc:
            log.debug("game stream ended: %s", exc)

    # ── the daytime slideshow ────────────────────────────────────────
    #
    # Three GETs and one POST, all loopback like everything else here. The one
    # worth pausing on is `/api/photos/file/<name>`: it serves bytes off the
    # disk by name, which is the shape of every directory traversal ever
    # written. What makes it safe is that the name has to match
    # `aipi5/photos/cache.py`'s `NAME` — thirty-two hex characters and one of
    # three extensions — and that check happens before the path is joined, so
    # there is nothing for `../` to be part of. The same regex is what named
    # the file in the first place.

    def _photos(self):
        """The service, or None having answered."""
        photos = getattr(self.ui, "photos", None)
        if photos is None or not photos.cfg.enabled:
            self._json({"error": "the photo slideshow is turned off"}, 503)
            return None
        return photos

    def _photo_list(self) -> None:
        """The slideshow's playlist, and the settings that shape it.

        Asked for when the slideshow starts and again only when
        `photos_rev` on the state poll has moved — a few hundred filenames is
        not something to send twice a second for a photograph that arrives
        once an hour.
        """
        photos = self._photos()
        if photos is None:
            return
        cfg = photos.cfg
        self._json({
            "photos": photos.cache.playlist(),
            "interval_s": cfg.interval_seconds,
            "transition_ms": cfg.transition_ms,
            "shuffle": cfg.shuffle,
            "show_info": cfg.show_info,
            "rev": photos.cache.revision(),
        })

    def _photo_file(self, path: str) -> None:
        """One cached photograph.

        Cached hard by the browser on purpose. The name is a digest of the
        Google media id, so the same name is always the same bytes — a
        content-addressed URL, which is exactly the case `immutable` exists
        for. Without it Chromium re-reads several hundred kilobytes off the SD
        card every time the shuffle comes back round.
        """
        photos = self._photos()
        if photos is None:
            return
        name = path[len("/api/photos/file/"):]
        resolved = photos.cache.resolve(name)
        if resolved is None:
            self._json({"error": "no such photo"}, 404)
            return
        try:
            body = resolved.read_bytes()
        except OSError as exc:
            log.warning("could not read a cached photo: %s", exc)
            self._json({"error": "unreadable"}, 500)
            return
        kind = ("image/png" if name.endswith(".png")
                else "image/webp" if name.endswith(".webp") else "image/jpeg")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("the slideshow went away mid-photo")

    def _photo_qr(self) -> None:
        """The picker URL as a QR code, for a phone to photograph.

        SVG, and `no-store` — this URL authorises picking into one session and
        is not something to leave in a browser cache once the session is gone.
        """
        photos = self._photos()
        if photos is None:
            return
        uri = str(photos.pick_status().get("uri") or "")
        if not uri:
            self._json({"error": "no picking session is open"}, 409)
            return
        drawn = qr.svg(uri)
        if drawn is None:
            self._json({"error": "segno is not installed on this device, so "
                                 "the QR code cannot be drawn"}, 501)
            return
        self._send(200, drawn.encode("utf-8"), "image/svg+xml; charset=utf-8")

    def _photo_post(self, payload: dict) -> None:
        """The settings page's verbs.

        Not entries in `ACTIONS`: everything in that tuple is a request for the
        *assistant* to do something and is queued for the voice loop. These
        configure a background service and are answered immediately, the same
        reasoning `/api/shutdown` carries.
        """
        photos = self._photos()
        if photos is None:
            return
        action = str(payload.get("action", ""))

        if action == "pick":
            self._json({"ok": True, "pick": photos.begin_pick(),
                        "qr": qr.available()})
        elif action == "cancel":
            self._json({"ok": True, "pick": photos.cancel_pick()})
        elif action == "dismiss":
            # Closing a finished report, which must never delete a session.
            self._json({"ok": True, "pick": photos.dismiss_pick()})
        elif action == "status":
            self._json({"ok": True, "photos": photos.describe(),
                        "qr": qr.available()})
        elif action == "select":
            chosen = payload.get("collections")
            if not isinstance(chosen, list):
                self._json({"ok": False, "error": "expected a list"}, 400)
                return
            self._json({"ok": True,
                        "selected": photos.select([str(c) for c in chosen])})
        elif action == "forget":
            removed = photos.forget_collection(str(payload.get("collection", "")))
            self._json({"ok": True, "removed": removed})
        elif action == "reconnect":
            # The linking script runs in another process, so the assistant
            # only learns it worked by looking again.
            photos.reload_auth()
            self._json({"ok": True, "auth": photos.auth.describe()})
        elif action == "disconnect":
            photos.disconnect()
            self._json({"ok": True})
        elif action == "sync":
            photos.sync_soon()
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "unknown action"}, 400)

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
        # Whether the transfer folder has changed since the page last looked.
        # Carried on the poll the page already makes rather than given a poll
        # of its own: the file list is only worth re-reading when it is both
        # visible and different, and this is one `stat` against a listing.
        store = getattr(self.ui, "files", None)
        payload["files_rev"] = store.revision() if store is not None else 0
        # The same trick for the slideshow: the page re-reads its playlist only
        # when this changes, rather than fetching a few hundred filenames twice
        # a second for a photograph that arrives once an hour.
        photos = getattr(self.ui, "photos", None)
        payload["photos_rev"] = (photos.cache.revision()
                                 if photos is not None and photos.cache.ready
                                 else "")
        # The Pi's clock, not the browser's. The screensaver draws the time and
        # the two can disagree — a Chromium with no network time on a device
        # that has it, or the reverse.
        payload["now"] = time.time()
        # Whether the agent has a browser up over this screen. The page needs
        # it to know when to run hand tracking: a recogniser fed by the camera
        # at twelve frames a second with nothing to drive is a core spent on
        # nothing. Cached on the agent's side, so this is not a socket round
        # trip twice a second.
        agent = getattr(self.ui, "agent", None)
        snapshot = agent.snapshot() if agent is not None else None
        payload["agent_browser"] = bool(snapshot and
                                        (snapshot.get("browser") or {}).get("open"))
        # Two booleans for the dot on the Agent button: a run still going, and a
        # question waiting for an answer. Off the same snapshot the line above
        # reads, so the console being closed costs the agent nothing — and a
        # question raised while somebody is looking at the weather is visible
        # from the home screen rather than only on the phone.
        payload["agent_busy"] = bool(snapshot and snapshot.get("busy"))
        payload["agent_pending"] = bool(snapshot and snapshot.get("pending"))
        # Which acquisition of the camera the feed is on. The page reconnects
        # its video stream when this changes: a stream opened before the camera
        # was handed back is dead, and the element it is attached to goes on
        # reporting the last frame it ever received.
        feed = getattr(self.ui, "hands", None)
        payload["hand_feed"] = feed.generation if feed is not None else 0
        # So a page that reloads comes back paused rather than quietly taking
        # the camera again.
        payload["hand_paused"] = bool(feed is not None and feed.paused)
        # Whether a hand is driving the browser right now. The camera is lent
        # while it is, so the three things that also want it -- a picture, a
        # call, a game -- cannot work, and the page greys them rather than
        # offering buttons that fail.
        payload["hand_control"] = bool(feed is not None and feed.active)
        # How many pages the agent has opened. A new one restarts the idle
        # clock and lifts a pause: somebody who has just asked for a page is
        # about to use it, and should not have to find a control to say so.
        payload["hand_page"] = int((snapshot or {}).get("browser", {})
                                   .get("opened") or 0)
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

    def _volume(self) -> None:
        control = getattr(self.ui, "volume", None)
        if control is None:
            self._json({"available": False,
                        "error": "volume control is unavailable"}, 503)
            return
        self._json(control.describe())

    def _volume_post(self, payload: dict) -> None:
        """Set the one sink downstream of every application audio stream."""
        control = getattr(self.ui, "volume", None)
        if control is None:
            self._json({"ok": False, "available": False,
                        "error": "volume control is unavailable"}, 503)
            return
        value = payload.get("level")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= 100 or int(value) != value):
            self._json({"ok": False,
                        "error": "level must be a whole number from 0 to 100"},
                       400)
            return
        ok = control.set(int(value))
        answer = control.describe()
        answer["ok"] = ok
        self._json(answer, 200 if ok else 503)

    def _screensaver_post(self, payload: dict) -> None:
        """Move the day/night boundary, or change what each half shows.

        Reading it back is `/api/system`'s `screensaver`, which already carries
        the running schedule — so there is no GET here.

        The validation lives in `ScreensaverSettings`, which knows why a value
        is wrong; this handler chooses the status code and nothing else. 400
        for a value somebody typed, 503 for a device with no screensaver to
        configure.
        """
        settings = getattr(self.ui, "screen_settings", None)
        if settings is None:
            self._json({"ok": False,
                        "error": "this build has no screensaver to set"}, 503)
            return

        fields = ("day_start", "night_start", "day_mode", "night_mode")
        # Only what was sent. The page changes one thing per tap and must not
        # have to restate the other three, which is how two taps in flight come
        # to overwrite one another.
        wanted = {name: payload[name] for name in fields if name in payload}
        if not wanted:
            self._json({"ok": False, "error": "nothing to change"}, 400)
            return

        try:
            answer = settings.set(**wanted)
        except ScheduleError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        answer["ok"] = True
        self._json(answer)

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
        # The page's shape, not the model's: this one carries the hourly strip
        # and the advice, neither of which belongs in the state poll.
        payload = weather.as_page_dict()
        payload["advice"] = should_go_outside(payload)
        self._json({"weather": payload, "now": time.time()})

    def _news(self, params: dict) -> None:
        """Today's local stories, as the feeds give them.

        **No page reads this any more.** The news page was removed; the News
        button now speaks its summary and nothing draws a list. Kept because it
        is the only way to see what the feeds actually returned — the spoken
        report is a model's two sentences about them, which is no use at all on
        the day the question is "is the feed broken?". `curl -s
        localhost:8092/api/news` on the device answers that; `?force=1` skips
        the cache.
        """
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
            # this server, so the only thing that can reach this route is a
            # process on this device — which is the point: a call out is
            # somebody at the Pi choosing to ring somebody, not a remote
            # request that could open a phone's microphone.
            #
            # The sequence itself is `CallController.call_out`, shared with the
            # spoken request. It used to be written out here, which was fine
            # while the button was the only way to ask; a second caller would
            # have been a second copy of start-the-session, tell-the-page,
            # send-the-push, and the two would have drifted at the first
            # change.
            outcome = self.ui.call.controller.call_out(
                str(payload.get("device", "")))
            if not outcome:
                self._json({"ok": False, "error": outcome.detail}, 409)
                return
            answer = outcome.as_dict()
            answer["error"] = ""
            answer["ice_servers"] = outcome.ice_servers
            self._json(answer)
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

    def _camera_capture(self) -> None:
        """The still the most recent description was made from.

        Takes no picture and no argument. The camera decides what the latest
        capture is and this serves that one file — which is the whole reason
        there is no name in the URL: a route that reads `/dev/shm` by a name
        the browser supplies is the shape of a traversal bug, and this one has
        nothing for `../` to be part of.

        The page still appends `?t=<taken_at>` because two different pictures
        are the same URL here, and without a cache-buster Chromium draws the
        first one under every answer afterwards. That token is the same
        `camera_image` the state poll published, so a page that asks for a
        capture is always asking about the answer it is currently showing.

        `no-store`, from `_send`: this is a photograph of the room, taken
        because somebody asked one question, and it does not belong in a
        browser cache after that.
        """
        camera = self.ui.camera
        still = getattr(camera, "last_capture", None) if camera else None
        if still is None:
            self._json({"error": "nothing has been captured"}, 404)
            return
        try:
            body = still.path.read_bytes()
        except OSError:
            # Pruned, or /dev/shm cleared under us. A 404 lets the page drop
            # the picture and keep the sentence, which is the honest half.
            log.debug("the last capture is no longer on disk: %s", still.path)
            self._json({"error": "the picture is no longer available"}, 404)
            return
        self._send(200, body, "image/jpeg")

    def _hand_stream(self, params: dict | None = None) -> None:
        """The camera at speed, for the gesture reader. See `agent/hands.py`.

        A separate route from `/api/camera/stream` for the reason that one is
        slow: that reads `Camera`, whose every read drains the V4L2 queue and
        waits for a live frame, and which is *lent* while this feed holds the
        device -- so it would answer 503 here anyway. This serves whatever the
        capture thread decoded most recently, which costs a resize and an
        encode and takes nothing from anyone.
        """
        feed = getattr(self.ui, "hands", None)
        if feed is None or not feed.active:
            self._json({"error": feed.error if feed else "no hand feed",
                        "active": False}, 503)
            return

        # **A stream whose viewer has gone must not pin a thread forever.**
        #
        # It did. `wfile.write` blocks once the socket buffer fills, and a
        # client that has stopped reading never drains it -- so the handler
        # sits in a write that cannot complete, holding a thread and a socket.
        # Six of them accumulated over an hour and the device stopped
        # responding to touch. `BrokenPipeError` only arrives for a connection
        # that was *closed*; one that is merely abandoned raises nothing at
        # all, which is why the existing handler caught nothing.
        self.connection.settimeout(HAND_WRITE_TIMEOUT_S)

        boundary = "aipi5hand"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        params = params or {}
        rate = self._bounded(params, "fps", HAND_FPS, 1, HAND_FPS_MAX)
        width = self._bounded(params, "w", HAND_WIDTH, 160, 640)
        interval = 1.0 / rate
        # **Its own cap, and a long one.** The camera page's five minutes suit
        # somebody glancing at a preview; this is the feed a person drives the
        # screen with, and every ending is a reconnect that can go wrong. One
        # did: the page reconnected on a timer to stay inside the shorter cap,
        # and the third reconnect left the element with no picture at all.
        # Fewer endings is the fix; the page no longer reconnects on a clock.
        deadline = time.monotonic() + HAND_STREAM_MAX_S
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                frame = feed.preview_jpeg(width)
                if frame is None:
                    # The browser closed and the camera went back. End the
                    # response; the page reopens it if it is wanted again.
                    break
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError):
            log.debug("hand feed client went away")
        except socket.timeout:
            # The abandoned-viewer case above. Not an error: the page has moved
            # on to a newer stream and this one has nobody to send to.
            log.debug("hand feed client stopped reading; dropping it")
        except OSError as exc:
            log.debug("hand feed stream ended: %s", exc)

    def _camera_stream(self, params: dict | None = None) -> None:
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

        # Six a second is right for "yes, that is you" on the settings page.
        # It is not enough to see a hand sweep: a sweep takes about half a
        # second, which is three frames, and three points cannot tell a
        # deliberate movement from a shrug. The gesture reader asks for more.
        interval = 1.0 / self._bounded(params or {}, "fps", PREVIEW_FPS,
                                       1, PREVIEW_FPS_MAX)
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
                 on_call_change=lambda: None, countdown=None, files=None,
                 photos=None, screen=None, screen_settings=None,
                 games=None, on_wake=lambda why: None,
                 agent=None, hands=None, birthdays=None, volume=None,
                 dictation=None, coordinator=None):
        self.cfg = cfg
        self.state = state
        # Called the moment activity arrives. `/api/display/wake` uses it
        # without queueing a voice action; the Talk page's explicit `wake`
        # action also uses it before that action reaches the microphone loop.
        self.on_wake = on_wake
        # The AI Motion game manager, or None when games are off. This module
        # knows only that it can be asked to open, close and describe a game;
        # everything about cameras, accelerators and screensavers is the
        # manager's, which is what keeps the four hardware handoffs in one
        # place rather than spread across HTTP handlers.
        self.games = games
        self.birthdays = birthdays
        # Applies a schedule change to the running manager and writes it to
        # the deployed configuration. Optional, so a test can build a server
        # with no screensaver behind it — `_screensaver_post` answers 503.
        self.screen_settings = screen_settings
        # The agent, or None where it is not installed. The same `AgentProxy`
        # the call server holds — one socket, not two. This server forwards a
        # hand gesture to the agent's browser (`_gesture_post`) and, since the
        # console arrived on this screen too, the console's own three messages
        # (`_agent_say`).
        self.agent = agent
        # The one door a sentence goes through, whichever of the four ways it
        # arrived. This server holds it only to forward — it decides nothing
        # about tools or policy, which is the point of there being one of
        # these rather than a decision in each handler. Optional, so a test can
        # build a server with no model and no agent behind it.
        self.coordinator = coordinator
        #: When the last `ASK_LIMIT` model requests were started. See
        #: `may_ask`.
        self._asks: deque[float] = deque(maxlen=ASK_LIMIT)
        self._ask_lock = threading.Lock()
        # How the compose box gets text on a panel with no keyboard: the voice
        # loop captures one utterance and hands back the words. Optional, so a
        # test can build a server with no microphone anywhere near it.
        self.dictation = dictation
        # The camera at speed while the agent's browser is up. Held by this
        # server only so `/api/hand/stream` can reach it; what decides whether
        # it runs is `Housekeeping`, once a second.
        self.hands = hands
        # The daytime slideshow's photographs and the object that decides
        # which screensaver is due. Both optional, so a test can build a
        # server without either.
        self.photos = photos
        self.screen = screen
        # The master PipeWire sink controller. Optional so lightweight UI
        # tests and deployments without audio still serve every other page.
        self.volume = volume
        # The shutdown countdown, which this module only ever answers about:
        # it is started by the voice loop and drawn by the page.
        self.countdown = countdown
        # The transfer folder. The same object the call server has, so a file
        # the phone sent is on the screen's list without anything syncing.
        self.files = files
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

    def may_ask(self) -> bool:
        """Whether another model request may start now.

        A backstop, not a boundary: `_same_origin` is what keeps a web page
        out. This is for the case that something gets past it, or a script on
        this device runs away — each of these costs money at OpenAI and takes
        the coordinator's turn lock, so a loop is both a bill and a device that
        will not answer the person standing in front of it.

        Counted across every caller rather than per address, because there is
        only one address: everything reaching this server came from loopback.
        """
        now = time.monotonic()
        with self._ask_lock:
            if len(self._asks) == self._asks.maxlen and (
                    now - self._asks[0]) < ASK_WINDOW_S:
                log.warning("refusing a model request: %d in the last %.0fs",
                            len(self._asks), now - self._asks[0])
                return False
            self._asks.append(now)
        return True

    def page(self) -> bytes | None:
        """Read the single application page once and hold it.

        Game assets use the separate, containment-checked ``asset_file`` path;
        this method never resolves a request URL.
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
