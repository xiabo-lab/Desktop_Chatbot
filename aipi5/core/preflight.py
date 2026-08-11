"""What is verified at boot, and what is allowed to be missing.

Section 31 lists eleven things to check at startup and adds one sentence that
does most of the work: *if a non-critical service fails, continue running in
degraded mode where practical.* So this module's job is not really checking —
each subsystem already reports on itself — it is **deciding what is fatal**,
and there are only two things on that list.

**Critical.** The microphone and speech recognition. Without either there is no
way to say anything to this device and no way for it to know what was said; a
process that starts anyway is a process that looks healthy in `systemctl
status` and does nothing at all, which is worse than a failed unit somebody can
see.

**Everything else is degraded.** No speaker: it hears and acts, silently, and
AIA's own `probe_output` already makes that case loudly. No network: Kodama
commands and the clock work. No OpenAI key or an unknown model: every
deterministic command works and conversation says why it cannot. No camera: it
cannot see, and the screensaver never engages. No Kodama-Lite: everything but
the music. No AI HAT+: person detection is off or on a core.

That list is the whole of section 37's reliability criteria, and the way to
make it true rather than aspirational is for it to be one function returning a
list of strings, which the UI then shows across the top of the screen.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Check:
    """One subsystem, and what was found."""

    name: str
    ok: bool
    detail: str = ""
    critical: bool = False

    def line(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.critical else "--  ")
        return f"  {mark} {self.name:<22} {self.detail}"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "",
            critical: bool = False) -> Check:
        check = Check(name, ok, detail, critical)
        self.checks.append(check)
        return check

    @property
    def fatal(self) -> list[Check]:
        return [c for c in self.checks if c.critical and not c.ok]

    @property
    def degraded(self) -> list[str]:
        """Short phrases for the top of the screen.

        The name and nothing else where the detail is long: this goes across
        a 1280 px header beside the weather, and "OpenAI: the API does not
        recognise the model 'gpt-5.6-luna' — check openai.model in
        config/aipi5.yaml" belongs in the journal, not there.
        """
        return [f"{c.name} unavailable" for c in self.checks
                if not c.ok and not c.critical]

    def log(self) -> None:
        log.info("startup checks:")
        for check in self.checks:
            (log.info if check.ok or not check.critical else log.error)(check.line())

    def as_dict(self) -> dict:
        return {
            "ok": not self.fatal,
            "degraded": self.degraded,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail,
                 "critical": c.critical}
                for c in self.checks
            ],
        }


# What "the network is up" is asked of. An address rather than a name, so the
# answer does not depend on resolution — and then a name as well, because the
# two failures are genuinely different and this device has seen the second:
# a link that is up while DNS is not is what a 5G hotspot does, and it looks
# identical to being online from across the room.
PROBE_ADDRESS = ("1.1.1.1", 53)
PROBE_NAME = "api.openai.com"

# Generous, and deliberately more generous than the 1.0 s AIA's `network`
# command uses. That one answers a spoken question and is inside a 2.5 s turn
# budget; this one runs once at boot with nothing waiting on it, while the Pi
# is loading three models and starting a browser.
#
# 2.0 s was measured to be too tight on the real device. Idle, this connect
# takes 544-783 ms; under boot load it overran and the assistant reported
# "network unavailable" across the top of the screen in the same breath as
# "OpenAI answered in 2293 ms" — a false banner, which is worse than no banner
# because it sends somebody to debug a working network.
PROBE_TIMEOUT_S = 6.0


def _reachable(address, timeout: float) -> bool:
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


def _resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def _unit_active(unit: str) -> bool:
    try:
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        proc = subprocess.run(["systemctl", "--user", "is-active", unit],
                              capture_output=True, text=True, timeout=5, env=env)
        return proc.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError, AttributeError):
        # AttributeError covers `os.getuid` on Windows, which is where the
        # tests run. Nothing here is meaningful off the Pi.
        return False


Outcome = tuple  # (ok: bool, detail: str)


def run(settings, *, mic=None, stt=None, speaker: Outcome = (False, ""),
        camera: Outcome = (False, ""), detector: Outcome = (False, ""),
        llm: Outcome = (False, ""), wake=None, ui_started=True) -> Report:
    """Check everything, decide what is fatal, and say so in the journal.

    Subsystem arguments are `(ok, detail)` pairs rather than objects, and that
    is worth a sentence because the alternative was tried and was wrong. Passing
    "the object, or None, or an error string" reads fine at the call site and
    means the truthiness of the argument sometimes indicates health and
    sometimes indicates failure — the camera was passed its *error*, so a
    working camera arrived as `None` and was reported broken. A pair says which
    is which and cannot be read backwards.
    """
    report = Report()

    # ── critical ─────────────────────────────────────────────────────
    report.add("microphone", mic is not None, _describe_mic(mic), critical=True)
    report.add("speech recognition", stt is not None,
               getattr(stt, "name", "not started"), critical=True)

    # ── degraded, in the order they matter to somebody in the room ────
    speaker_ok, speaker_detail = speaker
    report.add("speaker", speaker_ok,
               speaker_detail or ("Piper voices loaded" if speaker_ok else
                                  "no audio output — replies will be "
                                  "synthesised and never heard"))

    has_route = _reachable(PROBE_ADDRESS, PROBE_TIMEOUT_S)
    has_names = _resolves(PROBE_NAME)
    report.add("network", has_route and has_names,
               "online" if has_route and has_names
               else ("connected, but name lookups are failing" if has_route
                     else "offline — weather, news and conversation need it"))

    llm_ok, llm_detail = llm
    report.add("OpenAI", llm_ok,
               llm_detail or ("ready" if llm_ok else "conversation is unavailable"))

    if not settings.camera.enabled:
        report.add("camera", True, "disabled in the configuration")
    else:
        camera_ok, camera_detail = camera
        report.add("camera", camera_ok,
                   camera_detail or ("Brio 101 ready" if camera_ok
                                     else "no camera"))

    if not settings.person.enabled:
        report.add("person detection", True, "disabled in the configuration")
    else:
        detector_ok, detector_detail = detector
        report.add("person detection", detector_ok,
                   detector_detail or ("running — the screensaver will engage"
                                       if detector_ok else
                                       "not running — the screensaver will "
                                       "not engage"))

    report.add("display", ui_started,
               f"{settings.display.width}x{settings.display.height} at "
               f"{settings.display.url}" if ui_started
               else "the UI server did not start; the screen will not update")

    # Always ok: a player that is not running is the normal state of a device
    # that has just booted, and the assistant can start it on request. Reported
    # so the boot log says which it was.
    if not settings.kodama.enabled:
        report.add("Kodama-Lite", True, "disabled in the configuration")
    else:
        running = _unit_active(settings.kodama.service)
        report.add("Kodama-Lite", True,
                   "running" if running else
                   f"not running — {settings.kodama.service} will be started on request")

    # Not critical, and that is a deliberate call. `AIA_NO_WAKE=1` is a
    # supported way to run — every utterance becomes a command — so a wake
    # detector that did not load leaves an assistant that still works for
    # anybody willing to talk to it continuously. Worth a warning, not a
    # refusal to start.
    report.add("wake word", wake is not None,
               f"AIA's {type(wake).__name__}" if wake is not None else
               "no wake detector; every utterance will be treated as a command")

    report.log()
    for check in report.fatal:
        log.error("%s is required and is not working: %s", check.name, check.detail)
    if report.degraded:
        log.warning("running degraded: %s", ", ".join(report.degraded))
    return report


def _describe_mic(mic) -> str:
    if mic is None:
        return "no capture device"
    try:
        described = mic.describe()
        return f"{described.get('name', 'unknown')} (card {described.get('card')})"
    except Exception:
        return "open"


def chromium() -> str | None:
    """Which browser can present the UI, if any.

    Named here rather than in the launcher script so the boot log can say the
    screen will not come up, on the device, at the moment it does not — rather
    than leaving somebody to discover it by looking at a blank display.
    """
    for candidate in ("chromium-browser", "chromium", "chromium-browser-stable"):
        found = shutil.which(candidate)
        if found:
            return found
    return None
