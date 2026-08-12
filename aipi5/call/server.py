"""The one thing in this project that listens on the network.

Everything else — `aipi5/ui/server.py` — is bound to loopback and has no
authentication, which is safe precisely because nothing off the device can
reach it. This server is the exception, so every decision in it is made the
other way round.

**Every route authenticates before it does anything**, including the ones that
only read. The single exception is `GET /` and its two assets, which are the
page that asks for the token — it contains no state, reveals nothing about the
device, and has to be reachable before the phone has proved anything.

**Nothing here opens the camera or the microphone.** It cannot: the media is
Chromium's on both ends and this process never touches a capture device for a
call. The strongest form of "reject unauthorised calls before activating the
Brio" is that the code path does not exist, and this is it — an unauthenticated
request is refused three layers before anything is asked to hand over hardware.

**Refusals are uniform.** A bad token, a token for a revoked device and a
missing token all produce the same 401 and the same body. Distinguishing them
would tell somebody probing which of their guesses was closer.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aipi5.call import push, tailscale, tls, turn
from aipi5.call.signaling import PHONE, PI, POLL_TIMEOUT_S, SignalingHub

log = logging.getLogger(__name__)

#: Addresses that mean "only this machine". `tls: false` is allowed on these
#: and nowhere else.
LOOPBACK = ("127.0.0.1", "localhost", "::1")

PAGE = Path(__file__).resolve().parent / "web" / "phone.html"

#: A signalling message is an SDP blob, which for a video call with a few
#: codecs runs to a few kilobytes. 64 KiB is generous and still bounded.
MAX_BODY = 64 * 1024

#: `Authorization: Bearer <token>`, and nothing else accepted. A token in a
#: query string would be a token in the server log, in the browser history and
#: in any proxy in between.
_BEARER = re.compile(r"^Bearer\s+([A-Za-z0-9_\-]{16,128})$")


class _CallServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` with a backlog fit for a proxy in front of it.

    The stdlib default `request_queue_size` is 5. That is a reasonable number
    for a browser talking to a server directly and a thin one for
    `tailscale serve`, which pools connections and opens them in bursts —
    anything arriving while five are already waiting to be accepted is dropped
    by the kernel, and what the client sees is a request that vanished.

    Raised rather than diagnosed: two requests were lost over the course of
    testing behind the proxy — one `bye`, which left a call up, and one page
    fetch that came back 501 — and neither could be reproduced afterwards in a
    hundred attempts. The cause was not established. This removes the cheapest
    candidate rather than claiming to have found it.
    """

    daemon_threads = True
    request_queue_size = 64


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIPI5"

    #: How long a kept-alive connection may sit idle before it is closed.
    #: Comfortably longer than a long poll, which blocks inside the handler
    #: rather than between requests, so this never cuts one short. Without it
    #: an idle connection holds a thread for as long as the peer keeps it open,
    #: which for a pooling proxy is indefinitely.
    timeout = POLL_TIMEOUT_S + 15.0

    call: "CallServer" = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    # ── responses ────────────────────────────────────────────────────

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # This page asks for camera and microphone permission, so it is worth
        # being explicit that nothing on it may be framed, and that it loads
        # nothing from anywhere else. Everything the phone page needs is inline.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # `script-src` needs `'self'` as well as `'unsafe-inline'`, and
        # `worker-src` has to be stated outright.
        #
        # Without them the service worker cannot be registered and the phone
        # can never be rung — `worker-src` falls back to `script-src`, which
        # was `'unsafe-inline'` alone, and that permits inline scripts while
        # forbidding the loading of `/sw.js` as one. The symptom was a button
        # that reported "this browser cannot receive calls" and no request
        # reaching the Pi at all, on a phone and a browser that both support
        # it perfectly well.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "worker-src 'self'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; media-src blob:; connect-src 'self'; "
            "manifest-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
        self.end_headers()
        # `Content-Length` is still the real length on a HEAD — that is the
        # whole point of the request — but the body itself must not follow, or
        # the connection desynchronises exactly as an unread POST body does.
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("phone went away mid-response")

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _refuse(self) -> None:
        """The single answer to every authentication failure."""
        self._json({"error": "unauthorized"}, 401)

    # ── authentication ───────────────────────────────────────────────

    def _peer(self) -> str:
        """Who is really calling, for the rate limiter.

        Behind `tailscale serve` every connection arrives from `127.0.0.1`,
        because the proxy is on this machine. Rate-limiting on that would make
        the lockout **global**: five bad guesses from any device on the tailnet
        would lock out the phone that is allowed to call, which converts a
        defence into a denial of service against its own user.

        So the forwarded address is used — but **only when the connection came
        from loopback**, which is the only case where a proxy of ours could
        have set it. Trusting `X-Forwarded-For` from a remote peer would let
        anyone claim a fresh address per request and never be locked out at
        all, which is the more common way this header is got wrong.
        """
        address = self.client_address[0] if self.client_address else ""
        if address not in LOOPBACK:
            return address
        forwarded = (self.headers.get("X-Forwarded-For", "") or "").split(",")
        claimed = forwarded[0].strip()
        # Length-capped: this is attacker-controlled text used as a dict key.
        return claimed[:64] if claimed else address

    def _device(self) -> dict | None:
        """The trusted device behind this request, or None — having refused it.

        The lockout is checked first and costs nothing, so a flood from one
        address is answered without a hash per request.
        """
        address = self._peer()
        left = self.call.devices.blocked(address)
        if left > 0:
            log.debug("%s is locked out for another %.0f s", address, left)
            self._json({"error": "too many attempts"}, 429)
            return None

        match = _BEARER.match(self.headers.get("Authorization", "") or "")
        device = self.call.devices.authenticate(match.group(1) if match else "",
                                                address)
        if device is None:
            self._refuse()
            return None
        return device

    def _read_body(self) -> dict | None:
        """Consume and parse the request body. None means already answered.

        A body larger than the limit is still *read* before the 413 goes back,
        for the reason in `do_POST` — refusing without draining desynchronises
        the connection just as thoroughly as ignoring it. It is read in bounded
        chunks so an oversized one cannot be a way to allocate memory here.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0:
            length = 0

        if length > MAX_BODY:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._json({"error": "too large"}, 413)
            return None

        try:
            raw = self.rfile.read(length) if length else b""
        except OSError:
            self.close_connection = True
            return None
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            self._json({"error": "expected JSON"}, 400)
            return None
        return payload if isinstance(payload, dict) else {}

    def _body(self) -> dict:
        """The body `do_POST` already read."""
        return getattr(self, "_payload", {})

    # ── routes ───────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if route.path in ("/", "/index.html", "/call"):
            self._page()
        elif route.path in ("/icon-180.png", "/icon-512.png"):
            self._asset(route.path.lstrip("/"), "image/png")
        elif route.path == "/sw.js":
            # Served from the root deliberately: a service worker may only
            # control pages at or below its own path, and this one has to
            # control "/".
            self._asset("sw.js", "application/javascript; charset=utf-8")
        elif route.path == "/call/v1/vapid":
            # The public half only, and unauthenticated because the phone needs
            # it to build a subscription. It is a public key; publishing it is
            # what it is for.
            self._json({"key": self.call.push.keys.public()})
        elif route.path == "/manifest.webmanifest":
            self._manifest()
        elif route.path == "/call/v1/poll":
            self._poll(parse_qs(route.query))
        elif route.path == "/call/v1/state":
            self._state()
        elif route.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "not found"}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        """Same headers as `GET`, no body — for the static routes only.

        Without this the stdlib answers every HEAD with `501 Unsupported
        method`, and clients do probe with it: a phone deciding whether a saved
        page has changed, a proxy checking liveness, a link preview. None of
        them are load-bearing here, which is why this went unnoticed, but a 501
        to a liveness probe is a device that looks down.

        Deliberately **not** routed to the API. `/call/v1/poll` blocks for
        twenty-five seconds by design, and a HEAD that parked a thread there
        would be a free way to exhaust them without ever authenticating.
        """
        path = urlparse(self.path).path
        self._head_only = True
        try:
            if path in ("/", "/index.html", "/call"):
                self._page()
            elif path in ("/icon-180.png", "/icon-512.png"):
                self._asset(path.lstrip("/"), "image/png")
            elif path == "/manifest.webmanifest":
                self._manifest()
            else:
                self._json({"error": "not found"}, 404)
        finally:
            self._head_only = False

    def do_POST(self) -> None:  # noqa: N802
        """Read the body, then dispatch. The order is the whole point.

        **Every POST must consume its body, whatever the route does with it.**
        On a kept-alive HTTP/1.1 connection an unread body is not discarded —
        it stays in the socket, and the next request on that connection is
        parsed starting from it. The symptom is a request that fails or
        vanishes some time later, on a different route, with nothing wrong at
        either end.

        This is not hypothetical. `/call/v1/ring` used to ignore its body, and
        the phone posts `{}` to it; behind `tailscale serve`, which pools
        connections, the very next request came back
        `501 Unsupported method ('{}POST')` — the leftover `{}` glued to the
        front of the following request line. It cost a lost `bye`, which left
        a call up with the camera lent, and it took two failures and a hundred
        clean retries to catch.

        Reading here rather than in each route is what makes it impossible for
        a future route to reintroduce it.
        """
        path = urlparse(self.path).path
        payload = self._read_body()
        if payload is None:
            return                      # already answered with 400 or 413
        self._payload = payload

        routes = {"/call/v1/ring": self._ring,
                  "/call/v1/send": self._send_message,
                  "/call/v1/bye": self._bye,
                  "/call/v1/subscribe": self._subscribe,
                  "/call/v1/pickup": self._pickup}
        handler = routes.get(path)
        if handler is None:
            self._json({"error": "not found"}, 404)
            return
        handler()

    def _page(self) -> None:
        body = self.call.page()
        if body is None:
            self._json({"error": "the phone page is missing"}, 500)
            return
        self._send(200, body, "text/html; charset=utf-8")

    def _asset(self, name: str, content_type: str) -> None:
        """One of the two home-screen icons.

        Unauthenticated, like the page itself: an icon reveals nothing about
        the device and has to be fetchable before anybody has proved anything,
        because iOS reads it while the shortcut is being created. The name is
        matched against a fixed list in `do_GET` rather than joined onto a
        directory, so there is nothing here for a crafted path to traverse.
        """
        body = self.call.asset(name)
        if body is None:
            self._json({"error": "not found"}, 404)
            return
        self._send(200, body, content_type)

    def _manifest(self) -> None:
        """What Android and desktop Chrome install from. iOS ignores it."""
        # **No `start_url`.** Per the manifest spec it then defaults to the
        # document the manifest was loaded from — which carries the `#t=`
        # fragment, so an installed app launches with its token. A literal `/`
        # here would drop it and every launch would say "not paired", which is
        # exactly the failure this feature already had on iOS for a different
        # reason. iOS ignores the manifest entirely; this is for Android.
        self._send(200, json.dumps({
            "name": "Call AIPI5",
            "short_name": "AIPI5",
            "display": "standalone",
            "background_color": "#0e1116",
            "theme_color": "#0e1116",
            "icons": [
                {"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any maskable"},
            ],
        }).encode("utf-8"), "application/manifest+json")

    def _state(self) -> None:
        device = self._device()
        if device is None:
            return
        state = dict(self.call.hub.snapshot())
        # Whether *this* phone is registered to be rung. The phone cannot tell
        # from its own side: iOS keeps a push subscription that the Pi may
        # never have received, and a page that trusts the local one then
        # believes it is set up while the Pi lists no phone at all. The Pi is
        # the authority, so it says so on every poll.
        state["can_ring"] = bool(self.call.subscriptions.get(device["name"]))
        self._json(state)

    def _ring(self) -> None:
        """Dial the Pi. The one route that starts anything."""
        device = self._device()
        if device is None:
            return
        accepted, session, why = self.call.hub.ring(device["name"])
        if not accepted:
            self._json({"ok": False, "error": why}, 409)
            return
        self.call.on_change()
        # The ICE servers travel with the answer rather than being baked into
        # the page, because phase 3 adds a TURN relay with credentials that
        # rotate — and a page holding stale ones is a call that fails on the
        # cellular path only, which is the hardest kind to reproduce.
        self._json({"ok": True, "session": session, "role": PHONE,
                    "ice_servers": self.call.ice_servers(device["name"])})

    def _subscribe(self) -> None:
        """The phone offering a way to be rung when its app is closed.

        Stored against the paired device name, so re-pairing a phone replaces
        its subscription instead of leaving a dead endpoint behind that every
        future ring pays a failed request for.
        """
        device = self._device()
        if device is None:
            return
        body = self._body()
        subscription = body.get("subscription")
        if not isinstance(subscription, dict):
            # A failure to subscribe is reported here too, because everything
            # that can go wrong does so on a phone whose console nobody can
            # open. Third time in this feature that a silent failure hid the
            # answer, so the reason goes to the journal even though there is
            # nothing to store.
            problem = body.get("error")
            if problem:
                log.warning("%s could not subscribe to rings: %s",
                            device["name"], str(problem)[:300])
                self._json({"ok": False, "logged": True})
                return
            self._json({"error": "expected a subscription"}, 400)
            return
        ok = self.call.subscriptions.register(device["name"], subscription)
        if ok:
            # The screen draws "Call my phone" from `can_ring`, and that is
            # published on state changes rather than computed per request — so
            # without this the Pi kept saying it had no phone to ring until
            # something unrelated happened to walk past the camera.
            self.call.on_change()
        self._json({"ok": ok}, 200 if ok else 400)

    def _pickup(self) -> None:
        """Somebody answered on the phone.

        The counterpart of the Pi's auto-answer, and deliberately *not*
        automatic: this only ever runs because a person tapped. The Pi may
        answer a trusted phone by itself because the trust was established at
        pairing; a phone must not answer the Pi by itself, because the phone
        belongs to somebody who may be anywhere.
        """
        device = self._device()
        if device is None:
            return
        session = str(self._body().get("session", ""))
        ok = self.call.hub.picked_up(session)
        self.call.on_change()
        self._json({"ok": ok, "role": PHONE,
                    "ice_servers": self.call.ice_servers(device["name"])},
                   200 if ok else 409)

    def _send_message(self) -> None:
        """One signalling message from the phone to the Pi."""
        if self._device() is None:
            return
        payload = self._body()
        message = payload.get("message")
        if not isinstance(message, dict):
            self._json({"error": "expected a message"}, 400)
            return
        session = str(payload.get("session", ""))

        # The phone reports its own view of the transport, because it is the
        # side that notices a cellular handover first. Both peers may report;
        # the hub decides what is a legal transition.
        kind = str(message.get("type", ""))
        if kind == "connected":
            self.call.hub.connected(session)
            self.call.on_change()
        elif kind == "reconnecting":
            self.call.hub.reconnecting(session)
            self.call.on_change()
        elif kind in ("route", "note", "audio"):
            # Diagnostics for whoever reads this device's journal, not
            # something the Pi's page needs. Which candidate type carried the
            # media is the fact phase 3 turns on: "connected" and "connected
            # through the relay" look the same on screen. Truncated because it
            # is text from a client.
            log.info("call %s: %s", kind, str(message.get("detail", ""))[:200])
            self._json({"ok": True})
            return

        self.call.hub.post(PI, message, session)
        self._json({"ok": True})

    def _poll(self, params: dict) -> None:
        if self._device() is None:
            return
        try:
            since = int(params.get("since", ["0"])[0])
        except ValueError:
            since = 0
        messages, cursor = self.call.hub.collect(PHONE, since, POLL_TIMEOUT_S)
        self._json({"messages": messages, "cursor": cursor,
                    "call": self.call.hub.snapshot()})

    def _bye(self) -> None:
        if self._device() is None:
            return
        payload = self._body()
        # The phone says why — "you hung up", "the connection could not be
        # recovered", "the connection failed". Truncated because it is text
        # from a client, and prefixed so the journal still reads as an event
        # rather than a bare phrase.
        why = str(payload.get("reason", ""))[:120].strip()
        self.call.hub.hang_up(str(payload.get("session", "")),
                              f"the phone: {why}" if why else "the phone hung up")
        self.call.on_change()
        self._json({"ok": True})


class CallServer:
    """The HTTPS listener, its certificate, and the hub behind both doors.

    `on_change` is called whenever the call state moves, so the assistant can
    publish it to the screen immediately rather than the page waiting for its
    next poll. Defaulted to a no-op so this object is usable in a test without
    an assistant around it.
    """

    def __init__(self, cfg, *, hub: SignalingHub, devices,
                 on_change=lambda: None):
        self.cfg = cfg
        self.hub = hub
        self.devices = devices
        # Ringing a phone whose app is closed. Optional: without the library or
        # a subscription the Pi simply cannot start an outgoing call, and
        # everything else works exactly as before.
        self.push_keys = push.PushKeys(cfg.devices.parent)
        self.subscriptions = push.Subscriptions(cfg.devices.parent)
        self.push = push.Pusher(self.push_keys, self.subscriptions)
        self.on_change = on_change
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._page: bytes | None = None
        self._assets: dict[str, bytes] = {}
        self.error: str = ""
        self.url: str = ""

    def ice_servers(self, name: str = "aipi5") -> list[dict]:
        """What both peers should use to find each other.

        Empty on a LAN, which is phase 2: host candidates alone connect two
        devices on the same subnet, and a STUN round trip to the Internet only
        adds latency to a call that was going to work anyway. Configured for
        phase 3, where the phone is on a cellular network and the two have no
        address in common. See `aipi5/call/turn.py` — the credentials it
        returns expire, which is why this is built per call.
        """
        return turn.ice_servers(self.cfg, name)

    def asset(self, name: str) -> bytes | None:
        """A file from the web directory, read once and held.

        Only ever called with a name `do_GET` matched against a fixed list, and
        the join is checked against the directory afterwards regardless —
        cheap, and it means the guarantee does not depend on the caller.
        """
        cached = self._assets.get(name)
        if cached is not None:
            return cached
        path = (PAGE.parent / name).resolve()
        if path.parent != PAGE.parent.resolve():
            log.warning("refusing to serve %r from outside the web directory", name)
            return None
        try:
            self._assets[name] = path.read_bytes()
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            return None
        return self._assets[name]

    def page(self) -> bytes | None:
        if self._page is None:
            try:
                self._page = PAGE.read_bytes()
            except OSError as exc:
                log.error("could not read %s: %s", PAGE, exc)
                return None
        return self._page

    def start(self) -> bool:
        """Listen, or explain why not. Never raises — calling is optional."""
        if not self.cfg.enabled:
            self.error = "disabled in the configuration"
            return False

        # Refusing to listen with nothing paired is the belt to the braces of
        # authenticating every route. A server with an empty device list is
        # already safe — every token fails — but not listening at all is a
        # smaller thing to get wrong, and the log line tells somebody exactly
        # what to run.
        if len(self.devices) == 0:
            self.error = ("no phone is paired — run scripts/pair-phone.sh to "
                          "authorise one")
            log.warning("the call server is not listening: %s", self.error)
            return False

        context = None
        if self.cfg.tls:
            if not tls.ensure(self.cfg.certificate, self.cfg.private_key):
                self.error = "no TLS certificate; the phone cannot connect"
                return False

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # TLS 1.2 is the floor. Everything that can run a WebRTC call is a
            # browser from the last five years and speaks 1.3.
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                context.load_cert_chain(self.cfg.certificate, self.cfg.private_key)
            except (OSError, ssl.SSLError) as exc:
                self.error = f"the certificate could not be loaded: {exc}"
                log.error("%s", self.error)
                return False
        elif self.cfg.host not in LOOPBACK:
            # Refused, not warned about. Plaintext on a shared network means
            # the bearer token — which is the entire authorisation to turn on
            # a camera in somebody's home — is readable by anything on the
            # path. `tls: false` exists for a proxy on this machine and for
            # nothing else.
            self.error = (f"tls is off but the server would listen on "
                          f"{self.cfg.host}; refusing. Set host to 127.0.0.1 "
                          f"and let a local proxy terminate TLS, or set "
                          f"tls: true")
            log.error("%s", self.error)
            return False

        handler = type("_BoundHandler", (_Handler,), {"call": self})
        try:
            server = _CallServer((self.cfg.host, self.cfg.port), handler)
        except OSError as exc:
            self.error = f"could not listen on {self.cfg.host}:{self.cfg.port} — {exc}"
            log.warning("%s", self.error)
            return False

        if context is not None:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="aipi5-call", daemon=True)
        self._thread.start()

        self.url = self.public_url()
        log.info("call server listening on %s:%d — phones reach it at %s "
                 "(%d trusted)", self.cfg.host, self.cfg.port, self.url,
                 len(self.devices))
        if not self.cfg.tls and not tailscale.serving(self.cfg.port):
            # Listening on loopback with nothing in front of it is a server no
            # phone can reach. Said plainly, with the command, because the
            # symptom is otherwise a call button that does nothing.
            log.warning("tls is off and nothing appears to be proxying to "
                        "port %d — run: %s", self.cfg.port,
                        tailscale.serve_command(self.cfg.port))
        return True

    def public_url(self) -> str:
        """Where a phone should be pointed.

        The tailnet name wins when there is one: it is the address that works
        from a cellular network, and it is the one with a real certificate.
        A LAN address is the phase 2 answer and only right at home.
        """
        name = tailscale.dns_name()
        if name:
            # `tailscale serve` publishes on 443, so the port is implicit.
            return f"https://{name}/" if not self.cfg.tls else \
                   f"https://{name}:{self.cfg.port}/"
        addresses = tls.local_addresses()
        where = addresses[0] if addresses else self.cfg.host
        scheme = "https" if self.cfg.tls else "http"
        return f"{scheme}://{where}:{self.cfg.port}/"

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        # Anything still parked in a long poll is told the call is over first,
        # so those threads return rather than being waited on for their full
        # 25 seconds while the assistant is trying to shut down.
        self.hub.hang_up(why="the assistant is shutting down")
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            log.debug("shutting down the call server failed", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=POLL_TIMEOUT_S + 2.0)

    def describe(self) -> dict:
        """For the settings page. No secrets, and no token."""
        return {
            "listening": self._server is not None,
            "url": self.url,
            "error": self.error,
            "devices": self.devices.devices(),
            "tls": self.cfg.tls,
            "fingerprint": (tls.fingerprint(self.cfg.certificate)
                            if self.cfg.tls and self.cfg.certificate.exists()
                            else ""),
            "ice": turn.describe(self.cfg),
            "tailscale": tailscale.describe(),
            "push": self.push.describe(),
            # The hub returns `can_ring: False` always — it knows nothing about
            # notifications, and the assistant fills the field in for the
            # screen. Publishing that raw made this page report that no phone
            # could be rung while `push.phones` in the same payload named one,
            # which is the settings page lying about the thing it exists to
            # show.
            "call": {**self.hub.snapshot(),
                     "can_ring": bool(self.push.keys.available
                                      and self.subscriptions.names())},
            "updated": time.time(),
        }
