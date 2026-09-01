"""Open a website by voice — in the agent's browser, which a hand can drive.

Asked for by name, in either language, and matched on the fast path — so
"open YouTube" costs the ~10 ms the router costs and never reaches the model.
That is not only about latency. `aipi5/llm/tools.py` is explicit that the model
may drive the music player and may not *start* it, because an app that begins
making noise in somebody's living room has to be a person's decision rather
than an inference from a half-heard sentence. Opening a browser is the same
kind of act, so it is the same kind of command: declared here, spoken by a
person, and absent from the toolbox.

**One site per command, from a table.** There is no `{site}` slot and there is
not going to be one. A slot would make the address bar reachable by speech,
which turns a mis-heard word into a page nobody asked for — and the router's
own comments are full of what it does to unfamiliar proper nouns. What this
device will open is decided in this file.

## Which browser, and why it matters more than it looks

The first version started a Chromium of its own, and it worked: the window
appeared, YouTube loaded, and the person could touch it. What it could not do
was be driven by a hand — reported from the room as "the hand tracker is not
activated" — and the reason is that hand control is not a property of a window
but of a *pipe*. `aipi5/agent/pilot.py` reads wrists off the accelerator and
sends gestures to the root helper, which drives Chromium over a CDP pipe it
holds; and `Housekeeping` only lends it the camera at all while the agent's
`browser_state` says a page is open. A browser nobody holds a pipe to is
invisible to every part of that.

So a page asked for out loud goes through `AgentProxy.open_page` to the agent
runtime, which calls the helper's `browser_open` — the same op the phone's
agent calls, into the same window. One browser: the one somebody asks for by
voice, the one the phone drives, and the one a hand drives. Hand control needs
no telling, because it already follows the flag that op sets.

It is handed over on a thread and the reply is spoken at once. Measured on the
device, `browser_open` is **5.3 s cold and 3.0 s warm** — most of it the settle
and the page read that exist for a model rather than for a person — and the
voice loop's whole budget is 2.5 s. The window appears about a second in, well
before the sentence about it finishes.

**The plain Chromium is still here, as the fallback**, for a device where the
agent is not installed or not answering (`agent.enabled` is off in the
committed configuration). It is a worse browser on purpose-built terms — no
hand, no ad blocking — so taking that path is logged as a warning that says so
rather than left to be discovered by waving at the screen.

**A decorated window, not full-screen and not `--app`.** This project has twice
built a window with no way out of it, and the agent's browser has the scar: its
docstring records the owner opening YouTube, meeting a consent wall, and having
nothing to press. A decorated window gives a close button, a tab close button,
Back, and an address bar — four ways out, on a panel whose only input is a
finger. 770 rather than 800 tall so the title bar fits on the screen and the
close button is never off the bottom. The agent's browser is built the same way
and for the same reason.

**Its own Chromium profile, never the kiosk's.** The kiosk profile carries the
camera and microphone grants for the assistant's own page (see
`scripts/aipi5-ui.sh`), and a browser sharing a profile does not start a second
window — Chromium hands the command line to the instance already running, which
would open YouTube as a *tab inside the kiosk*, over the assistant's face, with
no tab strip to get back from.

That same forwarding is how the fallback window is raised: launching again with
the URL reaches the running copy, which opens it in a tab and brings itself to
the front. It is the Kodama-Lite `raise_window` trick and it works for the same
reason.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.plugins.base import CommandSpec, Plugin, Result

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Site:
    """One website this device will open, and what to call it out loud."""

    name: str
    url: str
    label: str
    phrases: dict[str, tuple[str, ...]] = field(default_factory=dict)


#: Every site reachable by voice. Adding one is this entry plus its phrases,
#: and its phrases have to be measured against the router the way the Mandarin
#: ones below were — see tests/test_routing.py.
SITES = (
    Site(
        name="youtube",
        url="https://www.youtube.com/",
        label="YouTube",
        phrases={
            "en": ("open youtube", "open you tube", "open the youtube",
                   "start youtube", "launch youtube", "go to youtube",
                   "open youtube please"),
            # 打开YouTube is what a person says and 打开优兔 is not, so the
            # transcript to plan for is code-switched — and SenseVoice does
            # not reliably reach the final consonant of an English word it is
            # hearing inside a Mandarin sentence. The utterance that started
            # this work came back as 打开 youtu 。, which the router folds to
            # `dakaiyoutu` against this phrase's `dakaiyoutube`: 0.91, against
            # a floor of 0.78. 打开优兔 and 打开有兔 — the two ways the same
            # sounds get written when the recogniser guesses characters —
            # score the same 0.91, because pinyin is what is compared.
            #
            # 打开油管 is the colloquial name and is its own phrase rather than
            # a near-miss of the first: `dakaiyouguan` scores 0.75 against
            # `dakaiyoutube`, under the floor, so without it "open the oil
            # pipe" would simply not route.
            #
            # Both are "打开" plus a name, which is also what the music player's
            # phrases are, so that is where the margin is thinnest and it is
            # the pair worth writing down: 打开YouTube against 打开音乐 is
            # 0.70 and 打开油管 against 打开播放器 is 0.64, against the 0.78 a
            # whole-utterance match needs. Nothing else in the command set is
            # nearer. Checked in tests/test_browser.py, not asserted here — the
            # first version of the launcher's comment quoted a score that was
            # wrong by 0.19 and only a test found it.
            "zh": ("打开YouTube", "打开油管", "打开YouTube网站",
                   "我要看YouTube"),
        },
    ),
)


#: How long the agent gets to answer "open this page". Comfortably over the
#: 5.3 s a cold `browser_open` was measured at, because nothing waits on it —
#: the reply has already been spoken and this is only how long the thread
#: hangs about before deciding the agent is not going to answer.
AGENT_OPEN_TIMEOUT_S = 60.0


class BrowserLauncher(Plugin):
    """Opens one of `SITES` in a browser, and says whether it worked.

    `available()` is always True while the feature is on. Same reasoning as
    `KodamaLauncher`: the whole point of the command is the case where the
    browser is *not* running, so a plugin that reported itself unavailable
    then would have `main.py` refuse the one command it exists for.
    """

    name = "browser"
    description = "The web browser"

    def __init__(self, cfg, agent=None):
        """`agent` is the `AgentProxy`, or a callable returning one.

        A callable because `main.py` builds the command set before it builds
        the proxy, and reordering two blocks of a startup sequence that works
        is a larger change than late binding — the same reason `GameVoice` is
        handed `lambda: getattr(self, "games", None)` there.

        `None` is a device with no agent, which is the committed default. It
        gets the fallback browser and a warning saying what it does not get.
        """
        self.cfg = cfg
        self._agent = agent
        #: The Chromium *this* object started, if it is still up — never the
        #: agent's, which belongs to the helper. Only ever the first launch: a
        #: second one forwards its command line to this process and exits
        #: immediately, so tracking it would report the browser as closed a
        #: moment after opening a page in it.
        self._proc: subprocess.Popen | None = None
        #: One hand-off at a time. Two rapid asks are one person repeating
        #: themselves, and the second would otherwise queue a second three
        #: second round trip behind the first.
        self._handing_over = threading.Lock()
        #: The thread the last hand-off went out on. Kept so `settled()` can
        #: wait for it — the tests do, and so would anything that ever needs
        #: to know the page has landed rather than been asked for.
        self._handover: threading.Thread | None = None
        #: Why the last hand-off failed, for the settings page. A failure that
        #: happens after the reply has been spoken has nowhere else to go.
        self._last_error = ""

    def available(self) -> bool:
        return self.cfg.enabled

    def _proxy(self):
        """The agent proxy, if there is one and its socket is there.

        `available()` on the proxy is a `stat`, so this is cheap enough for
        the settings page to call on every request.
        """
        proxy = self._agent() if callable(self._agent) else self._agent
        if proxy is None:
            return None
        try:
            return proxy if proxy.available() else None
        except Exception:                                       # noqa: BLE001
            log.debug("could not ask the agent proxy for its socket",
                      exc_info=True)
            return None

    def settled(self, timeout: float = 5.0) -> bool:
        """Wait for a hand-off in flight. True if nothing is still going.

        Nothing on the voice path calls this — the whole point of the thread is
        that the reply does not wait for it. It exists so a test can assert on
        what the hand-off did rather than on how long it slept.
        """
        thread = self._handover
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def running(self) -> bool:
        """Is the browser this assistant started still open?

        Says nothing about a Chromium left over from a previous run of the
        service — that one still holds the profile, so a launch reaches it and
        opens the page, and the only cost of not knowing is that the reply says
        "opening" where it could have said "already open".
        """
        return self._proc is not None and self._proc.poll() is None

    def describe(self) -> dict:
        """For the settings page.

        `through_agent` is the one worth reading: false means a page asked for
        by voice opens in a browser no hand can drive, and that is a property
        of the device rather than of anything somebody did wrong.
        """
        return {
            "enabled": self.cfg.enabled,
            "through_agent": self._proxy() is not None,
            "running": self.running(),
            "profile": str(self.cfg.profile),
            "sites": [site.label for site in SITES],
            "last_error": self._last_error,
        }

    # ── launching ────────────────────────────────────────────────────

    def _session_env(self) -> dict:
        """Environment with Wayland and the session bus filled in.

        A systemd service inherits neither, and a browser started without them
        cannot connect to the compositor. `os.getuid` is guarded because it
        does not exist on Windows, which is where the tests run — the same
        guard `aipi5/kodama/launcher.py` carries, for the same reason.
        """
        env = dict(os.environ)
        uid = getattr(os, "getuid", None)
        if uid is not None:
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid()}")
            env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                           f"unix:path=/run/user/{uid()}/bus")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        return env

    def _argv(self, url: str) -> list[str] | None:
        """The command line, or None if there is no Chromium to run.

        The URL comes from `SITES`, which is source. Nothing a person said
        reaches this list.
        """
        browser = shutil.which("chromium") or shutil.which("chromium-browser")
        if not browser:
            return None
        return [
            browser,
            f"--user-data-dir={self.cfg.profile}",
            # The Pi's Chromium finds Wayland through /etc/chromium.d, which a
            # service's environment does not necessarily reach. Named, like the
            # agent's browser names it, so this does not depend on a wrapper
            # script having been sourced.
            "--ozone-platform=wayland",
            "--window-size=1280,770", "--window-position=0,0",
            "--no-first-run", "--no-default-browser-check",
            # Do not touch the system keyring. Without this Chromium asks
            # GNOME Keyring for somewhere to keep secrets and, on a machine
            # with no keyring, raises a modal password dialog nobody is going
            # to type into — centred, on top, and surviving a restart. Same
            # flag and the same reason as the kiosk.
            "--password-store=basic",
            "--noerrdialogs", "--disable-infobars",
            "--disable-session-crashed-bubble",
            # One --disable-features flag, never two: Chromium keeps only the
            # last occurrence, so a second silently discards this one.
            "--disable-features=TranslateUI,PipeWireCamera,WebRtcPipeWireCamera",
            # No camera and no microphone for pages opened here. The assistant
            # owns the Brio and the TI capsule, and a web page asking for
            # either while a turn is in flight is a fight nobody wins.
            "--use-fake-device-for-media-stream",
            url,
        ]

    def open(self, site: Site, who: str = "something unnamed") -> Result:
        """Open `site`, in the agent's browser where there is one.

        Returns before the page has loaded, and that is the point: the hand-off
        is 3–5 s measured on the device against a turn budget of 2.5 s, so it
        goes on a thread and the sentence is spoken while the window is still
        painting. The reply says what is starting, not what has finished, which
        is what "Opening YouTube." means anyway.

        `who` names the caller, for the same reason `KodamaLauncher.open` takes
        it: journald here is volatile, so a launch nobody can attribute is one
        that gets argued about later instead of explained.
        """
        if not self.cfg.enabled:
            return Result.failed(
                "Opening websites is turned off in the settings.",
                "打开网站的功能在设置里被关闭了。")

        proxy = self._proxy()
        if proxy is not None:
            log.info("%s asked for %s; handing it to the agent's browser",
                     who, site.label)
            self._handover = threading.Thread(
                target=self._through_agent, args=(proxy, site, who),
                name="browser-open", daemon=True)
            self._handover.start()
            return Result.done(f"Opening {site.label}.",
                               f"正在打开 {site.label}。")

        # **Named, and named as a loss.** This window is a browser a hand
        # cannot drive and an advertisement is not blocked in, and the only
        # symptom is a feature that does nothing when somebody waves at it.
        log.warning("the agent is not reachable, so %s opens in a browser of "
                    "its own — hand control and ad blocking both belong to the "
                    "agent's browser and will not work in this one", site.label)
        return self._launch(site, who)

    def _through_agent(self, proxy, site: Site, who: str) -> None:
        """Ask the agent runtime for the page. Runs on its own thread.

        Falls back to a browser of our own rather than leaving somebody who
        asked for YouTube with nothing. That is the right trade even though the
        window is worse, and it is why the failure is logged at ERROR with the
        reason in it: by the time this runs the reply has already been spoken,
        so this line is the only account of what happened.
        """
        if not self._handing_over.acquire(blocking=False):
            log.info("already opening a page; ignoring %s", site.label)
            return
        try:
            status, payload = proxy.open_page(site.url,
                                              timeout=AGENT_OPEN_TIMEOUT_S)
            if status == 200 and payload.get("ok"):
                self._last_error = ""
                log.info("%s is open in the agent's browser (%r); a hand can "
                         "drive it", site.label, payload.get("title") or "")
                return
            detail = str(payload.get("error") or f"HTTP {status}")
        except Exception as exc:                                # noqa: BLE001
            # `AgentProxy` does not raise, so this is something unforeseen
            # rather than the agent being down — and it is on a thread whose
            # exception nobody would ever see.
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            self._handing_over.release()

        self._last_error = detail
        log.error("the agent could not open %s (%s); falling back to a browser "
                  "of our own, which a hand cannot drive", site.label, detail)
        self._launch(site, who)

    def _launch(self, site: Site, who: str) -> Result:
        """Start a Chromium of our own. The fallback, and only the fallback."""
        argv = self._argv(site.url)
        if argv is None:
            log.error("no chromium on this device; cannot open %s", site.url)
            return Result.failed(
                "I couldn't find a browser to open.",
                "我找不到可以用的浏览器。")

        already = self.running()
        try:
            self.cfg.profile.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                argv, env=self._session_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own session, so a restart of the assistant does not take
                # the browser down with it, and so nothing is left for a
                # process nobody is going to wait on.
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("could not start a browser for %s: %s", site.url, exc)
            return Result.failed(
                "I couldn't open the browser.",
                "我打不开浏览器。")

        if already:
            # This process is the one that forwards the URL and exits; the
            # window belongs to the one already recorded. Not tracked.
            #
            # What the running Chromium does with a URL handed to it is open a
            # tab and raise itself — verified on the device — so the reply says
            # that rather than "bringing it to the front", which would leave
            # somebody looking for a window that had in fact grown a tab.
            log.info("%s asked for %s; the browser is open, so it opens a tab",
                     who, site.label)
            return Result.done(
                f"The browser is already open. {site.label} is in a new tab.",
                f"浏览器已经开着了，{site.label} 在新的标签页里。")

        self._proc = proc
        log.info("%s asked to open %s at %s", who, site.label, site.url)
        return Result.done(f"Opening {site.label}.",
                           f"正在打开 {site.label}。")

    # ── what can be said ─────────────────────────────────────────────

    def commands(self) -> list[CommandSpec]:
        return [
            CommandSpec(
                name=f"open_{site.name}",
                description=f"Open {site.label} in a web browser",
                speech={"en": f"Open {site.label}",
                        "zh": f"打开 {site.label}"},
                # Bound here, so the log says a person asked out loud rather
                # than leaving the launch unattributed. `site=site` because the
                # router calls the handler with no arguments and a late-bound
                # loop variable would give every command the last site.
                handler=(lambda site=site: self.open(site, "a spoken command")),
                # Speaks, like `open_kodama`: a browser takes seconds to paint
                # and until it does there is nothing on the screen and no
                # sound, so silence is indistinguishable from being ignored.
                speaks=True,
                phrases=site.phrases,
            )
            for site in SITES
        ]
