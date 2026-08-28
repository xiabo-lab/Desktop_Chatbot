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

#: How long the browser may sit untouched before it is closed. It is several
#: hundred megabytes on a machine with eight, and a window left over the kiosk
#: is a device that looks broken to somebody walking past.
IDLE_TIMEOUT_S = 600.0

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

    def _attach(self):
        """Find the page and open a session on it."""
        targets = self.call("Target.getTargets")
        pages = [t for t in targets.get("targetInfos", [])
                 if t.get("type") == "page"]
        if not pages:
            raise Refused("the browser has no page open")
        self.target = pages[0]["targetId"]
        answer = self.call("Target.attachToTarget",
                           {"targetId": self.target, "flatten": True})
        self.session = answer.get("sessionId", "")
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
                raise Refused(str(answer["error"].get("message", "refused")))
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
    """Close a browser nobody is using. Called from the helper's own loop."""
    with _lock:
        if _browser is not None and _browser.alive \
                and _browser.idle_for() > IDLE_TIMEOUT_S:
            _browser.stop()
            return True
    return False


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
    "browser_screenshot": screenshot,
}
