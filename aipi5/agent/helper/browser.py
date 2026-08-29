"""Driving a second Chromium, over a pipe that only root holds.

The design review's warning about this stage was that a debugging *port* on a
browser running as `fuwenxu` is arbitrary code as `fuwenxu` — who carries
`NOPASSWD: ALL` — and that the agent user can reach loopback like anybody else.
That was correct, and it is why this does not use a port.

`--remote-debugging-pipe` gives Chromium its control channel on file
descriptors 3 and 4 instead of a socket. **Nothing is listening.** Measured on
the device: `ss -ltn` shows no debugging port at all while the browser is up. So
the only process that can speak CDP is the one holding the other ends of those
pipes, and that is this one — root, with a method allowlist in front.

    aipi5-agent  --named ops-->  helper (root)  --fds 3/4-->  chromium (fuwenxu)

The browser runs as `fuwenxu` because it has to: Wayland is that user's
session, and a browser anywhere else does not appear on the screen. Root
launches it through `runuser` and keeps the pipe, so the privilege that matters
— who may send `Runtime.evaluate` — stays here.

**The allowlist is on CDP methods, not on URLs alone.** `Page.navigate` and
`Input.dispatchMouseEvent` are how a person uses a browser. `Runtime.evaluate`,
`Runtime.callFunctionOn`, `Browser.setDownloadBehavior` and `Fetch.*` are how a
program uses one, and each of them turns "look at a web page" back into
"execute what you like as the user who is root here". They are refused.

Stdlib only, like everything else root runs on this device.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time

import policy

#: Chromium reads commands on fd 3 and writes replies on fd 4, each message a
#: NUL-terminated JSON object.
READ_FD = 3
WRITE_FD = 4

#: How long the browser may sit untouched before it is closed.
#:
#: Half an hour, not the ten minutes it started at. Ten was chosen when the
#: only way to be rid of a forgotten window was the agent closing it; the
#: window has its own close button now, so the cost of waiting longer is a
#: window somebody can dismiss, and the cost of waiting less was measured --
#: "closed the agent browser after 600s idle" while the owner was watching a
#: video on it.
IDLE_TIMEOUT_S = 1800.0

#: And how long once it has been *handed over* to a person -- a verification
#: box, a sign-in, a consent wall. Closing the page somebody was coming to deal
#: with is exactly the failure this whole feature exists to avoid, and the job
#: can mean finding a password or a phone. An hour, and it stays longer than
#: the plain timeout above however that one is tuned: a page waiting on a named
#: person has more claim to the screen than one nobody has been asked about.
HANDOVER_TIMEOUT_S = 3600.0

#: How long one CDP command may take. A navigation is the slow one.
CALL_TIMEOUT_S = 30.0
NAVIGATE_TIMEOUT_S = 45.0

#: Enough of a page to decide what to do next, and not so much that a search
#: results page fills the model's context.
MAX_ELEMENTS = 60
MAX_TEXT = 6000


class Refused(Exception):
    """Policy said no.

    Re-bound to `ops.Refused` when the helper loads, so a refusal from here
    reaches the connection handler as a refusal rather than as an unhandled
    exception reported as "failed".
    """


_lock = threading.Lock()
_browser = None


class Browser:
    """One Chromium, and the pipe to it. Created on demand, closed when idle."""

    def __init__(self):
        self.proc = None
        self.to_browser = -1
        self.from_browser = -1
        self.buffer = b""
        self.next_id = 0
        self.session = ""
        self.target = ""
        self.last_used = time.monotonic()
        self.opened_at = 0.0
        #: True while the page is waiting for a person rather than
        #: for the agent.
        self.handed_over = False
        #: The address as of the last sweep. A change means somebody
        #: has been navigating, which is the only sign of human use
        #: available without Runtime.evaluate.
        self.last_url = ''
        #: Whether the Overlay domain is on: False before, True after, and
        #: None once it has refused, so a browser that will not draw a pointer
        #: is asked once rather than eight times a second. See `_cursor`.
        self.overlay = False
        #: Why there is no pointer, when there is none. See `_cursor`.
        self.overlay_error = ""
        #: Whether the pointer is on the page now, so hiding it when the hand
        #: leaves costs nothing when it is already gone.
        self.cursor_shown = False
        #: Guards `_attach` calling itself through `call`. See the stale-session
        #: branch there.
        self._reattaching = False
        #: (measured_at, (width, height)) for the content area. See _viewport.
        self.viewport = None
        #: Where the page was when it was last scrolled, or None. Lets a
        #: scroll say whether the *previous* one took effect without waiting
        #: for this one to finish gliding. See `hand_scroll`.
        self.scrolled_to = None

    # ── lifecycle ───────────────────────────────────────────────────

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.alive:
            return
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if not chromium:
            raise Refused("there is no chromium on this device")

        # A profile of its own, so nothing it does can disturb the kiosk's --
        # which holds the camera and microphone grants for the assistant's own
        # page and must not be shared with pages the agent visits.
        profile = policy.BROWSER_PROFILE
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()

        def child():
            os.dup2(to_r, READ_FD)
            os.dup2(from_w, WRITE_FD)
            os.set_inheritable(READ_FD, True)
            os.set_inheritable(WRITE_FD, True)

        argv = ["runuser", "-u", policy.OWNER, "--", chromium,
                "--remote-debugging-pipe",
                "--ozone-platform=wayland",
                f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check",
                "--password-store=basic", "--noerrdialogs",
                "--disable-infobars", "--disable-session-crashed-bubble",
                # The same one-flag rule the kiosk follows: Chromium keeps only
                # the last --disable-features, so a second would discard this.
                "--disable-features=TranslateUI,PipeWireCamera,"
                "WebRtcPipeWireCamera,MediaRouter",
                # No camera, no microphone, no location. The agent is here to
                # look at pages, and a page that asks for the Brio while the
                # assistant owns it is a fight nobody wins.
                "--use-fake-device-for-media-stream",
                "--deny-permission-prompts",
                # Ad and tracker blocking, scoped to this browser. See
                # adblock.pac for why it is a PAC file and not Pi-hole.
                #
                # **Handed over as a data: URL, not a file:// one.** Chromium
                # accepts `--proxy-pac-url=file://…` without complaint and then
                # ignores it -- measured here: an ad host still loaded, and the
                # only way to tell was that it loaded. A data: URL is honoured,
                # and a blocked host fails with ERR_PROXY_CONNECTION_FAILED.
                "--proxy-pac-url=" + _pac_url(),
                # **An ordinary window, deliberately -- not --start-fullscreen
                # and not --app.**
                #
                # This used to start full-screen, which on this device means no
                # decorations at all: a page that traps somebody -- an
                # advertisement, a consent wall, anything modal -- trapped them
                # until the agent closed it or ten minutes went by. The owner
                # opened YouTube, met exactly that, and had no way out. It is
                # the same fault as a kiosk window with no way back, which this
                # project has now had twice.
                #
                # An --app window would fix it with one close button, and was
                # tried. A plain window is better: the person gets a close
                # button, a tab close button, a Back button and an address bar
                # -- four ways out instead of one. Tidiness is worth less than
                # that. The agent drives it the same either way.
                #
                # 770 rather than 800 so the title bar fits on a screen that is
                # exactly 800 tall, and the close button is never off-screen.
                "--window-size=1280,770", "--window-position=0,0",
                "about:blank"]

        env = {"XDG_RUNTIME_DIR": f"/run/user/{policy.OWNER_UID}",
               "WAYLAND_DISPLAY": "wayland-0",
               "HOME": str(policy.OWNER_HOME),
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                       "/sbin:/bin"}

        # **`pass_fds` must name 3 and 4 themselves, not only the pipe ends.**
        # Listing just the originals leaves subprocess closing the duplicates
        # after `preexec_fn` has run, and Chromium exits with "Remote debugging
        # pipe file descriptors are not open" -- which reads like the flag is
        # unsupported. It is not; they were closed underneath it.
        self.proc = subprocess.Popen(
            argv, preexec_fn=child,
            pass_fds=(to_r, from_w, READ_FD, WRITE_FD), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_r)
        os.close(from_w)
        self.to_browser, self.from_browser = to_w, from_r
        self.buffer = b""
        self.next_id = 0
        self.opened_at = time.monotonic()

        deadline = time.time() + 25
        while time.time() < deadline:
            if not self.alive:
                raise Refused("the browser would not start")
            try:
                self.call("Browser.getVersion", timeout=4.0)
                break
            except Refused:
                time.sleep(0.5)
        else:
            self.stop()
            raise Refused("the browser started but never answered")
        self._attach()

    def closed_by_hand(self):
        """True when the person at the device closed the window themselves.

        Not an error. The helper notices on the next operation and reports "no
        page is open", which is exactly what happened.
        """
        return self.proc is not None and self.proc.poll() is not None

    def stop(self):
        if self.alive:
            try:
                self.call("Browser.close", timeout=5.0)
            except Refused:
                pass
            for _ in range(20):
                if not self.alive:
                    break
                time.sleep(0.25)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for fd in (self.to_browser, self.from_browser):
            try:
                os.close(fd)
            except OSError:
                pass
        self.proc = None
        self.session = ""
        self.target = ""
        self.overlay = False
        self.cursor_shown = False

    #: The CDP error for a session that no longer exists. Matched on the text
    #: because that is all the protocol gives -- there is no code.
    STALE_SESSION = "session with given id not found"

    def _attach(self):
        """Find the page a person is looking at, and open a session on it.

        **Not simply the first page target.** The browser is a real window with
        a tab strip, and the person is invited to use it -- that is the whole
        point of `browser_hand_over`. When they open a tab, the target this was
        attached to is no longer the one on screen, and a page that is not on
        screen is the wrong thing to scroll.

        So: the last page target that is showing something, which is the tab
        most recently opened. `about:blank` is skipped because the browser is
        launched on it and it is never what anybody wants to drive.
        """
        targets = self.call("Target.getTargets", session=False)
        pages = [t for t in targets.get("targetInfos", [])
                 if t.get("type") == "page"]
        if not pages:
            raise Refused("the browser has no page open")
        real = [p for p in pages if p.get("url") not in ("", "about:blank")]
        chosen = (real or pages)[-1]
        self.target = chosen["targetId"]
        answer = self.call("Target.attachToTarget",
                           {"targetId": self.target, "flatten": True},
                           session=False)
        self.session = answer.get("sessionId", "")
        # A new document is a new overlay: whatever was enabled belonged to the
        # page that has gone. See `_cursor`.
        self.overlay = False
        self.cursor_shown = False
        self.viewport = None
        self.scrolled_to = None
        self.call("Page.enable")

    # ── the wire ────────────────────────────────────────────────────

    def call(self, method, params=None, timeout=CALL_TIMEOUT_S, session=True):
        """One CDP command. **The allowlist is checked by the caller, not here.**

        Kept separate on purpose: this function is also how the module drives
        its own lifecycle (`Target.attachToTarget`, `Page.enable`), and those
        are not things the agent may ask for. Mixing the two would mean either
        the agent could reach them or the module could not.
        """
        if not self.alive:
            raise Refused("the browser is not running")
        self.next_id += 1
        ident = self.next_id
        message = {"id": ident, "method": method, "params": params or {}}
        if session and self.session:
            message["sessionId"] = self.session
        try:
            os.write(self.to_browser, json.dumps(message).encode() + b"\0")
        except OSError as exc:
            raise Refused(f"the browser stopped listening ({exc})")

        deadline = time.time() + timeout
        while time.time() < deadline:
            answer = self._next_message(deadline)
            if answer is None:
                break
            if answer.get("id") != ident:
                continue                    # an event, or another command
            if "error" in answer:
                message = str(answer["error"].get("message", "refused"))
                # **The session dies when the person uses the browser.**
                #
                # Opening a tab replaces the target this was attached to, and
                # every call afterwards fails with "Session with given id not
                # found" -- while `browser_state` goes on reporting the browser
                # as open, because the *process* is fine. Hand control simply
                # stopped: no pointer, no gestures, nothing in any log saying
                # why. Reported twice as "the tracking is not working".
                #
                # So a stale session is not an error, it is a fact about a
                # browser somebody is using: re-attach to whatever page is
                # there now and run the command again. Once only -- a second
                # failure is a real one and must be reported.
                if (self.STALE_SESSION in message.lower()
                        and session and not self._reattaching):
                    self._reattaching = True
                    try:
                        self._attach()
                    except Refused:
                        raise Refused(message)
                    finally:
                        self._reattaching = False
                    return self.call(method, params, timeout, session)
                raise Refused(message)
            return answer.get("result", {})
        raise Refused(f"{method} did not answer within {timeout:.0f}s")

    def _next_message(self, deadline):
        while b"\0" not in self.buffer:
            left = deadline - time.time()
            if left <= 0:
                return None
            try:
                chunk = os.read(self.from_browser, 65536)
            except OSError:
                return None
            if not chunk:
                return None
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(b"\0")
        try:
            return json.loads(head)
        except ValueError:
            return {}

    def touch(self):
        self.last_used = time.monotonic()
        # A new operation means the agent has taken it back, so
        # whatever was handed over has been dealt with or abandoned.
        self.handed_over = False

    def idle_for(self):
        return time.monotonic() - self.last_used


# ── the ops layer ───────────────────────────────────────────────────


def _get(start=True):
    global _browser
    if _browser is None:
        _browser = Browser()
    if start and not _browser.alive:
        _browser.start()
    if _browser.alive:
        _browser.touch()
    return _browser


def sweep():
    """Close a browser nobody is using. Called from the helper's own loop.

    **"Nobody" has to include the person, and it did not.** The timer measured
    the *agent* not touching the page -- `touch()` is called from `_get`, which
    only runs for a helper operation -- so somebody standing at the device
    reading a page was invisible to it. From the log, on the evening it was
    reported:

        closed the agent browser after 600s idle

    Two changes. The plain timeout is half an hour rather than ten minutes,
    because the window has its own close button now and an aggressive
    auto-close was worth more when the agent was the only way to be rid of it.
    And a page whose address has changed since the last look has been navigated
    by somebody, which counts as use.

    That check happens **only at the deadline**, not on every tick. This runs
    off the accept timeout, once a second, and a `Page.getNavigationHistory` at
    1 Hz would be a CDP round trip a second for as long as a window is open --
    holding the same lock a hand uses eight times a second to move a pointer.
    Waiting until the timeout expires costs nothing: the comparison is against
    the address as of the previous look, so a navigation at any point in the
    half hour is still visible when the half hour is up.
    """
    with _lock:
        if _browser is None or not _browser.alive:
            return False

        limit = (HANDOVER_TIMEOUT_S if _browser.handed_over
                 else IDLE_TIMEOUT_S)
        if _browser.idle_for() <= limit:
            return False

        # Out of time by the agent's reckoning. Before closing, the one signal
        # available for a person -- `Runtime.evaluate` is refused for good
        # reasons, so the page cannot simply be asked when it was last
        # scrolled, and its address is the next best thing.
        try:
            history = _browser.call("Page.getNavigationHistory", timeout=5.0)
            entries = history.get("entries", [])
            current = entries[history.get("currentIndex", 0)] if entries else {}
            here = current.get("url", "")
        except Refused:
            here = _browser.last_url
        if here and here != _browser.last_url:
            _browser.last_url = here
            _browser.scrolled_to = None
            _browser.overlay = False
            _browser.last_used = time.monotonic()
            return False

        _browser.stop()
    return True


def shutdown():
    with _lock:
        if _browser is not None:
            _browser.stop()


def _pac_url():
    """The blocklist as a data: URL, or "" when it is not installed.

    Empty rather than fatal: a browser with no ad blocking is worth more than
    no browser, and the installer not having been re-run is a likely and
    recoverable state.
    """
    try:
        raw = policy.ADBLOCK_PAC.read_bytes()
    except OSError as exc:
        log.warning("no ad blocklist at %s (%s); the browser will not filter",
                    policy.ADBLOCK_PAC, exc)
        return ""
    encoded = base64.b64encode(raw).decode("ascii")
    return "data:application/x-ns-proxy-autoconfig;base64," + encoded


def _check_url(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 2000:
        raise Refused("a web address is required")
    if not re.match(r"^https?://[^\s]+$", url):
        # `file:`, `chrome:`, `devtools:` and `javascript:` are each a way to
        # turn "look at a page" into "read this device" or "run this code".
        raise Refused("only http:// and https:// addresses can be opened")
    host = url.split("//", 1)[1].split("/", 1)[0].split("@")[-1].lower()
    host = host.split(":")[0]
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        raise Refused("this device's own services are not reachable from the "
                      "browser tool")
    if re.match(r"^(10|127)\.|^192\.168\.|^169\.254\.|^172\.(1[6-9]|2\d|3[01])\.",
                host):
        raise Refused("private network addresses are not reachable from the "
                      "browser tool")
    return url


def open_url(args):
    url = _check_url(args.get("url"))
    with _lock:
        browser = _get()
        browser.call("Page.navigate", {"url": url},
                     timeout=NAVIGATE_TIMEOUT_S)
        _settle(browser)
        return _describe(browser)


def read_page(args):
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        return _describe(browser)


def click(args):
    ref = args.get("ref")
    if not isinstance(ref, int) or isinstance(ref, bool) or ref < 1:
        raise Refused("a number from the page listing is required")
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        elements = _elements(browser)
        if ref > len(elements):
            raise Refused(f"there is no item {ref} on this page")
        spot = elements[ref - 1]
        if not spot.get("x"):
            raise Refused("that item is not somewhere I can click")
        for kind in ("mousePressed", "mouseReleased"):
            browser.call("Input.dispatchMouseEvent",
                         {"type": kind, "x": spot["x"], "y": spot["y"],
                          "button": "left", "clickCount": 1})
        _settle(browser)
        return _describe(browser)


def type_text(args):
    text = args.get("text")
    if not isinstance(text, str) or not 1 <= len(text) <= 500:
        raise Refused("some text to type is required")
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        browser.call("Input.insertText", {"text": text})
        if args.get("submit"):
            for kind in ("keyDown", "keyUp"):
                browser.call("Input.dispatchKeyEvent",
                             {"type": kind, "key": "Enter",
                              "code": "Enter", "windowsVirtualKeyCode": 13,
                              "nativeVirtualKeyCode": 13})
            _settle(browser)
        return _describe(browser)


def go_back(args):
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        history = browser.call("Page.getNavigationHistory")
        index = history.get("currentIndex", 0)
        entries = history.get("entries", [])
        if index <= 0:
            raise Refused("there is nothing to go back to")
        browser.call("Page.navigateToHistoryEntry",
                     {"entryId": entries[index - 1]["id"]})
        _settle(browser)
        return _describe(browser)


def hand_over(args):
    """Leave the page up for a person, and stop touching it.

    For the things a browser meets that only a human can answer: a verification
    box, a sign-in, a cookie wall, a payment form. The agent must not attempt
    any of those itself -- and the useful thing it can do instead is get out of
    the way and say what is on screen.

    **Closing the browser is the one response that helps nobody**, because it
    destroys the only thing the person could have acted on. That is what
    happened the first time this came up: the agent met Google's unusual-traffic
    page and closed the window one second later, with the owner standing in
    front of the device, able to tap the box.
    """
    reason = args.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 300:
        raise Refused("say what the person needs to do")
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open to hand over")
        page = _describe(browser)
        # Set after the describe: reading the page touches it, and touching it
        # is what clears the flag again.
        browser.handed_over = True
        minutes = int(HANDOVER_TIMEOUT_S // 60)
        return {"handed_over": True, "reason": reason,
                "url": page.get("url", ""), "title": page.get("title", ""),
                "minutes": minutes,
                "detail": ("the window is still open and has its own close, "
                           "back and address bar; it will stay for %d minutes"
                           % minutes)}


# ── hand control ────────────────────────────────────────────────────
#
# The person can drive this browser with their hand, from across the room,
# because on this device the alternative is walking to it. The recognition
# happens in the kiosk page -- MediaPipe's hand model, already on disk, fed
# from the camera Python owns -- and what arrives here is a decided gesture,
# not a picture.
#
# So these are the *effects*, and each is one thing a hand can mean:
#
#     open palm sweeping up      scroll up
#     open palm sweeping down    scroll down
#     open palm sweeping left    back
#     open palm sweeping right   forward
#     closed fist                click, where the palm was
#
# Every one of them is something the person could do with the touchscreen. That
# is the bar: hand control adds reach, not authority. Nothing here can be asked
# for a coordinate outside the window, and there is no gesture for "type".

#: How far one sweep of the palm scrolls. About two thirds of a window, which
#: is what a page-down feels like and leaves enough overlap to keep your place.
SCROLL_STEP = 520

#: Fallback viewport, used only if the browser will not report its own. The
#: window is launched 1280x770 and Chromium's furniture takes the top ~92px.
FALLBACK_VIEWPORT = (1280, 678)

#: How long a measured viewport is trusted. Long enough that a pointer
#: following a hand does not re-measure on every step, short enough that a
#: page which changed the window's shape is noticed within a gesture or two.
VIEWPORT_CACHE_S = 5.0


def _viewport(browser):
    """The page's own drawing area, in the coordinates CDP input uses.

    Worth being exact about, because it is where the safety of `hand_click`
    comes from. `Input.dispatchMouseEvent` takes **viewport** coordinates --
    the same space the accessibility tree reports, which is why `click` above
    can feed it element positions unchanged. The browser's own toolbar, tab
    strip and address bar are not in that space at all, so there is no number a
    gesture can send that lands on them. It is not a bounds check that keeps a
    waving hand out of the address bar; the coordinate system does.

    Cached for a few seconds. A pointer following a hand asks eight times a
    second and the window is not resized between two of those, so measuring it
    every time would double the CDP traffic of the thing that has to stay
    ahead of a moving arm.
    """
    now = time.monotonic()
    if browser.viewport and now - browser.viewport[0] < VIEWPORT_CACHE_S:
        return browser.viewport[1]
    size = FALLBACK_VIEWPORT
    try:
        metrics = browser.call("Page.getLayoutMetrics", timeout=5.0)
        box = metrics.get("cssLayoutViewport", {})
        width = int(box.get("clientWidth", 0))
        height = int(box.get("clientHeight", 0))
        if width > 0 and height > 0:
            size = (width, height)
    except Refused:
        pass
    browser.viewport = (now, size)
    return size


def _scroll_offset(browser):
    """How far down the page is, in CSS pixels. Zero if it will not say."""
    try:
        metrics = browser.call("Page.getLayoutMetrics", timeout=5.0)
        return float(metrics.get("visualViewport", {}).get("pageY", 0.0))
    except (Refused, TypeError, ValueError):
        return 0.0


def _point(args, browser):
    """Where in the page to act, from a fraction of the way across it.

    The caller sends 0..1, not pixels. That is not politeness about units: the
    kiosk page has no way to know how tall this window's content area is, and
    a page that guessed would put every click a toolbar's height out. Sending
    a fraction means the only thing that has to know the size is the thing
    that can measure it.

    Out of range is refused rather than clamped. A hand at the edge of the
    camera is a hand halfway out of frame, and turning that into a click on
    the edge of the page is a click nobody aimed.
    """
    try:
        across = float(args.get("x"))
        down = float(args.get("y"))
    except (TypeError, ValueError):
        raise Refused("a point is required")
    if not (0.0 <= across <= 1.0 and 0.0 <= down <= 1.0):
        raise Refused("that is outside the page")
    width, height = _viewport(browser)
    return across * width, down * height


def hand_scroll(args):
    """Scroll the page, as an open palm sweeping up or down."""
    direction = args.get("direction")
    if direction not in ("up", "down"):
        raise Refused("scroll up or down")
    steps = args.get("steps", 1)
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 5:
        raise Refused("between one and five steps")
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        width, height = _viewport(browser)
        delta = SCROLL_STEP * steps * (-1 if direction == "up" else 1)
        # **Measured against the last scroll, not against a sleep.**
        #
        # `Input.dispatchMouseEvent` reports success for a wheel event the page
        # then ignores -- a scroller that swallows wheels, a document already
        # at the end -- so saying "scrolled" without checking would make a hand
        # being ignored look like a hand that is working.
        #
        # But the obvious check does not work. Reading the offset straight
        # after the event catches a smooth scroll mid-glide and returns zero
        # every time; an earlier version slept 350 ms to let it settle, which
        # bought a truer number with a third of a second of a hand that could
        # do nothing else, on the operation a person repeats most.
        #
        # Comparing this scroll's starting offset with the *previous* one costs
        # nothing and answers the same question one gesture later, which for
        # somebody sweeping repeatedly is soon enough to matter and never worth
        # waiting for.
        here = _scroll_offset(browser)
        moved = None if browser.scrolled_to is None else here - browser.scrolled_to
        browser.scrolled_to = here
        browser.call("Input.dispatchMouseEvent",
                     {"type": "mouseWheel", "x": width / 2, "y": height / 2,
                      "deltaX": 0, "deltaY": delta})
        answer = {"scrolled": direction, "steps": steps, "at": round(here)}
        if moved is not None:
            answer["moved_last_time"] = round(moved)
        return answer


#: The pointer, in CSS pixels, in two shapes.
#:
#: **These are rectangles, and they are rectangles because that is all there
#: is.** The pointer is drawn with `Overlay`, which is the only way to mark
#: this page without running script in it -- and script in the page is
#: `Runtime.evaluate`, the one capability the whole design exists to keep out
#: of reach. `Overlay` draws rectangles and quads; it has no image and no path.
#: So a drawn hand would cost the boundary, and the shape carries the state
#: instead: upright and narrow for an open palm, square and squat for a fist,
#: which is the difference between the two the eye actually reads at a glance.
CURSOR_OPEN = (30, 42)          # taller than wide, like a hand held up
CURSOR_CLOSED = (34, 30)        # squat and square, like a fist

#: Filled amber with a dark outline, chosen to sit on both a white page and a
#: dark one, which a single flat colour does not. The fist is the same yellow
#: rather than a new colour: it is the same hand, and only its shape has
#: changed.
CURSOR_FILL = {"r": 255, "g": 193, "b": 7, "a": 0.55}
CURSOR_EDGE = {"r": 20, "g": 20, "b": 20, "a": 0.9}
CURSOR_CLOSED_FILL = {"r": 255, "g": 179, "b": 0, "a": 0.8}


def _hide_cursor(browser):
    """Take the pointer off the page. Never fatal."""
    if not browser.overlay or not browser.cursor_shown:
        return
    try:
        browser.call("Overlay.hideHighlight", timeout=5.0)
    except Refused:
        pass
    browser.cursor_shown = False


def _cursor(browser, x, y, closed=False):
    """Draw the pointer at (x, y). Never fatal -- it is feedback, not control.

    **Drawn with `Overlay.highlightRect`, and that choice is the point.**

    The obvious way to put a dot on a page is to inject an element into it, and
    that needs `Runtime.evaluate` -- the one method this whole design exists to
    keep out of reach, because it is arbitrary code in a browser running as the
    user who owns the device. It is not on the operation table and must not
    arrive by the back door of a nice feature.

    `Overlay` is the domain DevTools uses to draw the blue box over an element
    you are inspecting. The browser renders it above the page, from its own
    process, and the page cannot see it, read it, or be changed by it. So the
    person gets a real pointer and the page gets no new capability at all.

    The alternative that was tried first was to draw it on the kiosk's own
    screen -- which is underneath this window, where nobody can see it.
    """
    if browser.overlay is None:
        # It has already refused once. `None` is falsy, so this check has to
        # come first -- written the other way round it fell into the enable
        # branch again on every move, which is the opposite of what the
        # comment there claimed.
        return
    if not browser.overlay:
        try:
            # `DOM.enable` first. The overlay agent draws in the coordinate
            # space the DOM agent establishes, and without it `highlightRect`
            # is accepted and quietly draws nothing -- which is exactly what
            # happened: zero changed pixels in a compositor screenshot, with
            # no error anywhere to explain it.
            browser.call("DOM.enable", timeout=5.0)
            browser.call("Overlay.enable", timeout=5.0)
            browser.overlay = True
        except Refused as exc:
            # An overlay that will not enable costs the person their pointer
            # and nothing else. Do not keep asking on every move -- but keep
            # the reason, because a pointer that silently never appears is the
            # kind of thing that gets diagnosed as "the camera cannot see my
            # hand". This module has no logger by design; `browser_state`
            # carries it out instead.
            browser.overlay = None
            browser.overlay_error = str(exc)[:120]
            return
    width, height = CURSOR_CLOSED if closed else CURSOR_OPEN
    try:
        browser.call("Overlay.highlightRect",
                     {"x": int(x - width / 2), "y": int(y - height / 2),
                      "width": width, "height": height,
                      "color": CURSOR_CLOSED_FILL if closed else CURSOR_FILL,
                      "outlineColor": CURSOR_EDGE},
                     timeout=5.0)
        browser.cursor_shown = True
    except Refused:
        # A navigation clears the overlay and can refuse the call that races
        # it. The next move redraws; asking again here would only double the
        # cost of the one operation that has to keep up with an arm.
        browser.overlay = False


def hand_move(args):
    """Move the pointer, as an open palm drifting across the camera.

    **This is the whole reason hand control is usable.** The recogniser lives
    in the kiosk page, which is *underneath* this browser -- so a cursor drawn
    there is drawn where nobody can see it, and aiming a click at a target you
    have no pointer for is guesswork.

    A synthetic `mouseMoved` fixes it without any drawing at all: the page under
    the pointer lights its own hover states, links underline, a video shows its
    controls. The feedback appears in the window the person is actually looking
    at, produced by that page's own CSS, and costs one CDP call.

    It does not move the *system* pointer, which is the other half of why this
    is safe -- nothing here can reach the browser's furniture or another
    window, only the page.
    """
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        x, y = _point(args, browser)
        browser.call("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x, "y": y})
        _cursor(browser, x, y)
        # Reported so a missing pointer is visible from outside rather than
        # inferred from a screenshot.
        return {"at": {"x": round(x), "y": round(y)},
                "cursor": bool(browser.overlay)}


def practice_page(args):
    """Open the hand-control practice sheet. Takes nothing, aims nowhere.

    A page with a scroll track down the side, arrows across the top, five
    targets to close your hand on, and a line of text that says what it just
    understood. It exists because on a real website a gesture that *nearly*
    worked and one that did nothing look identical.

    See `practice.py` for why it is a `data:` URL rather than a page served
    from the assistant, which would be the obvious thing and is refused for
    good reason.
    """
    import practice

    with _lock:
        browser = _get()
        browser.call("Page.navigate", {"url": practice.data_url()})
        _settle(browser, 2.0)
        # The address is the whole page, base64. Not worth carrying back, and
        # `last_url` is compared on every sweep.
        browser.last_url = "the hand control practice sheet"
        return {"opened": "the practice sheet",
                "detail": "five targets, four sheets, and a scroll track"}


def hand_hide(args):
    """Take the pointer off the page -- the hand has gone.

    A pointer left behind is worse than none: it says a hand is being tracked
    when none is, and somebody will move their arm expecting it to follow.
    """
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        _hide_cursor(browser)
        return {"cursor": False}


def hand_click(args):
    """Click where the palm was, as a closed fist."""
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        x, y = _point(args, browser)
        # The hand closes, so the click is visible even when the thing
        # clicked does nothing. Drawn before the press, so it is on screen
        # while the page is busy handling it.
        _cursor(browser, x, y, closed=True)
        for kind in ("mousePressed", "mouseReleased"):
            browser.call("Input.dispatchMouseEvent",
                         {"type": kind, "x": x, "y": y,
                          "button": "left", "clickCount": 1})
        # The agent's own `click` settles, because it reads the page
        # afterwards and wants the page it navigated to. This one returns
        # nothing to read: the person is looking at the window and will see
        # the result themselves. Waiting a second and a half to tell them what
        # they can already see would only be a second and a half in which
        # their hand did nothing.
        return {"clicked": {"x": round(x), "y": round(y)}}


def hand_history(args):
    """Back or forward, as an open palm sweeping sideways."""
    way = args.get("way")
    if way not in ("back", "forward"):
        raise Refused("back or forward")
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        history = browser.call("Page.getNavigationHistory")
        index = history.get("currentIndex", 0)
        entries = history.get("entries", [])
        wanted = index - 1 if way == "back" else index + 1
        if not 0 <= wanted < len(entries):
            # Not an error. Sweeping forward at the end of the history is a
            # thing people do, and it should say so rather than fail.
            return {"moved": False,
                    "detail": "there is nothing to go %s to" % way}
        browser.call("Page.navigateToHistoryEntry",
                     {"entryId": entries[wanted]["id"]})
        _settle(browser)
        return {"moved": True, "way": way,
                "title": (entries[wanted].get("title") or "")[:80]}


def state(args):
    """Is a browser up, and on what. Cheap enough to ask about every few seconds.

    Deliberately does **not** go through `_get`: asking whether the window is
    there must not start one, and must not count as using it. Both would be
    easy to write by accident and each would break something -- the first turns
    a status poll into a browser launch, the second stops the idle sweep ever
    firing because something is always looking.
    """
    with _lock:
        alive = _browser is not None and _browser.alive
        return {"open": alive,
                "url": (_browser.last_url if alive else ""),
                "handed_over": bool(alive and _browser.handed_over),
                "cursor": bool(alive and _browser.overlay),
                "cursor_error": (_browser.overlay_error if alive else "")}


def close(args):
    with _lock:
        if _browser is None or not _browser.alive:
            return {"closed": False, "detail": "the browser was not open"}
        _browser.stop()
        return {"closed": True}


def screenshot(args):
    """A picture of the page, into the shared transfer folder.

    Delivered that way rather than back through the tool result because the
    phone's Files screen already knows how to show what is in there, and a
    megabyte of base64 through a language model is a megabyte nobody reads.
    """
    with _lock:
        browser = _get(start=False)
        if not browser.alive:
            raise Refused("no page is open")
        shot = browser.call("Page.captureScreenshot",
                            {"format": "jpeg", "quality": 70})
        raw = base64.b64decode(shot.get("data", ""))
        if not raw:
            raise Refused("the browser returned an empty picture")
        target = policy.TRANSFER_DIR / time.strftime("agent-%Y%m%d-%H%M%S.jpg")
        policy.TRANSFER_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        try:
            os.chown(target, policy.OWNER_UID, policy.OWNER_UID)
            os.chmod(target, 0o644)
        except OSError:
            pass
        return {"saved": target.name, "bytes": len(raw),
                "detail": "it is on the phone's Files screen"}


# ── reading a page without being able to run code in it ─────────────


def _settle(browser, seconds=2.5):
    """Give a navigation a moment. Crude, and enough.

    The precise way is to wait for `Page.frameStoppedLoading`, which needs an
    event loop reading the pipe continuously. This module is called one
    operation at a time from a socket handler, so a short sleep buys the same
    thing for a fraction of the machinery.
    """
    time.sleep(seconds)


def _describe(browser):
    """What is on the page, as a person would describe it down a phone.

    Built from the accessibility tree, **not** from `Runtime.evaluate`. That
    method is refused for the agent, and using it here would put back exactly
    the capability the pipe was chosen to take away.
    """
    info = {}
    try:
        history = browser.call("Page.getNavigationHistory")
        entries = history.get("entries", [])
        current = entries[history.get("currentIndex", 0)] if entries else {}
        info["url"] = current.get("url", "")[:300]
        info["title"] = current.get("title", "")[:200]
    except Refused:
        info["url"] = info["title"] = ""

    elements = _elements(browser)
    info["items"] = [{"ref": n + 1, "kind": e["role"], "text": e["name"][:120]}
                     for n, e in enumerate(elements)]
    info["text"] = _page_text(browser)[:MAX_TEXT]
    return info


#: Roles worth offering as something to click or type into. Everything else on
#: a page is scenery, and a list of scenery is a list nobody can act on.
_USEFUL = {"link", "button", "textbox", "searchbox", "combobox", "checkbox",
           "radio", "tab", "menuitem", "option", "switch"}


def _elements(browser):
    """Interactive things, numbered, with a point to click in the middle."""
    try:
        tree = browser.call("Accessibility.getFullAXTree", {"depth": -1})
    except Refused:
        return []
    out = []
    for node in tree.get("nodes", []):
        role = (node.get("role") or {}).get("value", "")
        if role not in _USEFUL:
            continue
        if node.get("ignored"):
            continue
        name = ((node.get("name") or {}).get("value") or "").strip()
        if not name:
            continue
        spot = {"role": role, "name": name, "x": 0, "y": 0}
        backend = node.get("backendDOMNodeId")
        if backend:
            try:
                box = browser.call("DOM.getBoxModel", {"backendNodeId": backend})
                quad = box.get("model", {}).get("border", [])
                if len(quad) >= 8:
                    spot["x"] = (quad[0] + quad[4]) / 2
                    spot["y"] = (quad[1] + quad[5]) / 2
            except Refused:
                pass
        out.append(spot)
        if len(out) >= MAX_ELEMENTS:
            break
    return out


def _page_text(browser):
    """The readable text, from the same tree."""
    try:
        tree = browser.call("Accessibility.getFullAXTree", {"depth": -1})
    except Refused:
        return ""
    seen, parts = set(), []
    for node in tree.get("nodes", []):
        role = (node.get("role") or {}).get("value", "")
        if role not in ("StaticText", "heading", "paragraph"):
            continue
        name = ((node.get("name") or {}).get("value") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(name)
        if sum(len(p) for p in parts) > MAX_TEXT:
            break
    return "\n".join(parts)


#: The operations, and therefore the only CDP this device will perform on
#: request. `Runtime.*`, `Browser.setDownloadBehavior`, `Fetch.*` and
#: `Network.setCookies` are absent, not blocked -- there is no name to ask for.
OPS = {
    "browser_open": open_url,
    "browser_read": read_page,
    "browser_click": click,
    "browser_type": type_text,
    "browser_back": go_back,
    "browser_close": close,
    "browser_hand_over": hand_over,
    "browser_hand_scroll": hand_scroll,
    "browser_hand_move": hand_move,
    "browser_hand_hide": hand_hide,
    "browser_practice": practice_page,
    "browser_hand_click": hand_click,
    "browser_hand_history": hand_history,
    "browser_screenshot": screenshot,
    "browser_state": state,
}