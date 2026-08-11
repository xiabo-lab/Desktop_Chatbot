"""Open Kodama-Lite by voice.

The one Kodama capability AIA does not have. Everything else it can already do
— AIA's plugin is registered alongside this one and its twenty-odd commands are
untouched — but every one of them assumes the player is running, and a device
that can skip a track but not start the music is missing the first step.

**Started through its systemd user unit, never by running the binary.**
Kodama-Lite's own README is emphatic about this and the reason is not
stylistic: launching `/usr/bin/kodama-lite` directly starts a second copy
behind systemd's back, which takes its own stream-server port and, with
resume-on-startup, plays the last queue over the top of the first. The app
refuses the duplicate now — `tauri-plugin-single-instance` raises the running
window and exits — but keeping one launch path is what makes Quit-then-restart
behave predictably, and the assistant must not be the thing that introduces a
second one.

**Started and then waited for.** `systemctl start` returns when the unit has
been forked, not when a Tauri app with a webview has painted and published
MPRIS — several seconds apart on this Pi. Saying "opening the music player" and
having the next spoken command fail because the player is not on the bus yet is
the failure this waiting prevents.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.plugins.base import CommandSpec, Plugin, Result

log = logging.getLogger(__name__)

# How often the unit is asked whether the player has reached the bus, while
# waiting for it to start. Cheap — a `playerctl status` is 6-7 ms — and slow
# enough not to be a spin.
POLL_INTERVAL_S = 0.5

# Used only to raise an already-running window — never to start the player.
# See `raise_window` for why running the binary is safe there and nowhere else.
KODAMA_BINARY = "/usr/bin/kodama-lite"


class KodamaLauncher(Plugin):
    """Starts the player, and says whether it worked.

    `available()` is always True, unlike AIA's Kodama plugin. That is the whole
    point: the command exists precisely for when the app is *not* running, and
    a plugin that reported itself unavailable then would have its own launch
    command refused by the very check that exists to protect the others.
    """

    name = "kodama_launcher"
    description = "The music player launcher"

    def __init__(self, cfg, player):
        """`player` is AIA's `KodamaLite` plugin — the thing that can see the app.

        Passed in rather than constructed, so there is one object asking the
        session bus whether the player is there. Two would be two answers that
        can disagree during the several seconds a launch takes, which is
        exactly the window this class spends its time in.
        """
        self.cfg = cfg
        self.player = player

    def available(self) -> bool:
        return self.cfg.enabled

    def running(self) -> bool:
        """Is the player on the session bus right now?"""
        return self.player.available()

    def _systemctl(self, *args: str, timeout: float = 10.0) -> tuple[bool, str]:
        """Run a `systemctl --user` verb against the configured unit.

        The unit name comes from configuration and is passed as its own
        argument to a fixed argv — never interpolated into a shell string.
        There is no shell anywhere on this path, which is the property section
        29 is about: a value that came from a file cannot become a command.
        """
        env = dict(os.environ)
        # A service, or an ssh login, inherits no session bus address, and
        # `systemctl --user` without one fails with "Failed to connect to bus"
        # — which reads like the unit is broken rather than like the
        # environment is. Same fix AIA's Kodama plugin applies to playerctl.
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                       f"unix:path=/run/user/{os.getuid()}/bus")
        try:
            proc = subprocess.run(
                ["systemctl", "--user", *args, self.cfg.service],
                capture_output=True, text=True, timeout=timeout, env=env,
            )
        except FileNotFoundError:
            log.error("systemctl is not installed")
            return False, "systemctl is not available"
        except subprocess.TimeoutExpired:
            log.warning("systemctl %s %s timed out", " ".join(args), self.cfg.service)
            return False, "systemctl did not answer"
        output = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, output

    def raise_window(self) -> bool:
        """Bring the running player's window to the front. True if asked.

        **This is the one place the binary is run rather than the unit, and it
        is not a contradiction of the rule above.** The rule exists because
        running the binary *starts* a second copy. It does not: Kodama-Lite is
        built with `tauri-plugin-single-instance`, so a second launch hands the
        argv to the process already running — which raises its window — and
        then exits without ever opening a webview, a stream server or a second
        MPRIS name.

        Verified on this device rather than taken from the README, because the
        failure if the plugin were ever removed is the exact disaster the rule
        is about. With the player running, launching the binary left the
        process count at one, the launched copy exited on its own, and
        `playerctl -l` still listed a single `kodamalite`.

        There is no alternative on this hardware. `wmctrl` and `xdotool` are
        both installed and both are X11 clients; this is a Wayland session and
        they cannot enumerate a window, let alone raise one.

        Fire-and-forget. The process exits by itself and this must not block a
        button press waiting for it, so it is not waited on — which is also
        why the return value is "the request was made", not "the window is
        now in front". Nothing can honestly report the latter here.
        """
        try:
            subprocess.Popen(
                [KODAMA_BINARY],
                env=self._session_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own session, so the assistant's own shutdown does not
                # take the raise attempt down with it, and so no zombie is left
                # for a process nobody is going to wait on.
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not ask %s to raise its window: %s",
                        KODAMA_BINARY, exc)
            return False
        log.info("asked the running Kodama-Lite to raise its window")
        return True

    def _session_env(self) -> dict:
        """Environment with the session bus and Wayland display filled in.

        A systemd service inherits neither, and a GUI app started without them
        either fails to connect to the compositor or starts a second instance
        because it cannot see the first.

        `os.getuid` is guarded because it does not exist on Windows, which is
        where the tests run. None of these variables mean anything there and
        the subprocess will not start either way — but an `AttributeError`
        raised while building an environment is a confusing way to find that
        out, and it would come from a line that has nothing to do with the
        failure. Same guard `aipi5/core/preflight.py` carries, for the same
        reason.
        """
        env = dict(os.environ)
        uid = getattr(os, "getuid", None)
        if uid is not None:
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid()}")
            env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                           f"unix:path=/run/user/{uid()}/bus")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        return env

    def open(self) -> Result:
        """Start the player, or raise the window it already has.

        Idempotent, and idempotent in the way a person means it: somebody
        pressing Music while the player is already running wants to *see* the
        player, not to be told it is running. So the already-open case raises
        the window and says so, rather than only saying so.

        `systemctl start` on a running unit is a no-op anyway, so the branch is
        about what is done for the window, not about avoiding a restart.
        """
        if not self.cfg.enabled:
            return Result.failed("The music player is turned off in the settings.",
                                 "音乐播放器在设置里被关闭了。")

        if self.running():
            self.raise_window()
            return Result.done("Kodama-Lite is already open.",
                               "Kodama-Lite 已经打开了。")

        started, detail = self._systemctl("start")
        if not started:
            log.error("could not start %s: %s", self.cfg.service, detail)
            return Result.failed(
                "I couldn't start the music player.",
                "无法启动音乐播放器。",
            )

        log.info("started %s; waiting for it to reach the session bus",
                 self.cfg.service)
        deadline = time.monotonic() + self.cfg.start_timeout_s
        while time.monotonic() < deadline:
            if self.running():
                waited = self.cfg.start_timeout_s - (deadline - time.monotonic())
                log.info("player is up after %.1fs", waited)
                return Result.done("Kodama-Lite is open.", "音乐播放器已打开。")
            time.sleep(POLL_INTERVAL_S)

        # The unit started and the app has not appeared. Both halves are true
        # and the person needs both: something is loading, and it is not ready
        # for the command they were about to give it.
        log.warning("%s started but the player has not appeared after %.0fs",
                    self.cfg.service, self.cfg.start_timeout_s)
        return Result.done(
            "The music player is starting, but it isn't ready yet.",
            "音乐播放器正在启动，还没准备好。",
        )

    def commands(self) -> list[CommandSpec]:
        return [
            CommandSpec(
                name="open_kodama",
                description="Open the Kodama-Lite music player",
                handler=self.open,
                # Speaks, for the same reason AIA's `now_playing` does: for the
                # several seconds this takes there is nothing on screen and no
                # sound, so silence is indistinguishable from having been
                # ignored. It is also the only Kodama command whose result the
                # user cannot see happening.
                speaks=True,
                phrases={
                    "en": ("open kodama", "open kodama lite", "start kodama",
                           "launch kodama", "open the music player",
                           "start the music player", "open music"),
                    # 打开音乐 is here, and 启动音乐 is not. The two look
                    # interchangeable and are not, because of what they sound
                    # like next to `resume`, whose phrases include 播放音乐 and
                    # 放音乐. Measured in the pinyin the router actually
                    # compares, against the 0.78 a whole-utterance match needs:
                    #
                    #   打开音乐 vs 播放音乐   0.61
                    #   打开音乐 vs 放音乐     0.67
                    #   启动音乐 vs 播放音乐   0.75   ← too close to keep
                    #
                    # So 打开音乐 has 0.11 of headroom and is safe in both
                    # directions — an exact 打开音乐 scores 1.00 here and 0.61
                    # against resume, and 播放音乐 scores 1.00 against resume
                    # and 0.61 here. 启动音乐 has 0.03, which is not a margin,
                    # so the 启动 forms below all carry the extra syllables of
                    # 播放器 that put them clear. Checked in
                    # tests/test_routing.py rather than asserted here — an
                    # earlier version of this comment claimed 0.80 for the
                    # first pair and was simply wrong.
                    "zh": ("打开音乐播放器", "启动音乐播放器", "打开Kodama",
                           "启动Kodama", "打开播放器", "启动播放器", "打开音乐"),
                },
            ),
        ]
