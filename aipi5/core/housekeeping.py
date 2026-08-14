"""The periodic work that must keep happening when nothing is being said.

**This exists because all of it used to hang off the microphone.** The voice
loop is `while not stopping: frame = next(frames)`, and its idle branch was the
only thing that swept expired calls, retried a camera that had gone away,
refreshed the weather and published the UI state. That is a reasonable place
for housekeeping right up until the microphone stops delivering frames — at
which point `next(frames)` yields nothing, the loop body never runs, and four
unrelated subsystems stop with it.

Measured on the device, 2026-08-13. The TI microphone was unplugged; AIA could
not match a replacement and logged `could not reopen the microphone`. Then the
camera was swapped, and this happened:

    20:00:57  lost the camera  → retries at :57 :59 :01 :03 :05 → back :10
    20:11:09  lost the camera  → retries at :09 :11 :14 :16     → back :18
    20:19:54  lost the camera  → (nothing at all)               → needed a restart

The first two losses recovered on their own. The third did not, and the only
difference was that by then the microphone was dead. The camera's own retry is
unbounded and patient by design — it is simply never called.

The other three failures in that state are quieter and worse:

* the weather freezes at whatever it last read, forever, so the night
  screensaver shows an hour-old temperature with no indication why;
* a call stuck in `ringing` never expires, so the hardware is never given back;
* **`publish()` stops**, which freezes the whole UI snapshot — presence, the
  screensaver decision, the cooldowns. The screen neither goes to the
  screensaver nor comes back from it.

So this runs on its own thread with its own clock, and the microphone cannot
take it down. Nothing here is new work: it is the same four calls the voice
loop used to make, on a schedule that does not depend on audio.

**Every call must be idempotent and cheap**, because that is what the voice
loop required of them too. They are: `sweep` returns immediately with nothing
expired, `retry_reclaim` returns immediately when the camera is fine and rate
limits itself when it is not, `refresh_weather` is bounded by its own cache,
and `publish` is a dictionary swap under a lock.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

#: How often to look. A second is well inside the 60 s screensaver timeout and
#: the 500 ms the page polls at, and every call on this path is a no-op in the
#: common case — the expensive one, the weather, is gated by its own cache.
TICK_S = 1.0

#: How long an unhandled failure is allowed to repeat before the log stops
#: carrying a traceback for each one. A broken tick every second for a month is
#: a journal nobody can read.
QUIET_AFTER = 5

#: How soon to try the weather again when the last attempt produced nothing.
#:
#: **Not `weather.cache_seconds`.** That number answers "how stale may a good
#: reading be", which is a different question from "how long after a failure
#: should we try again", and using it for both is a bug you only see on a
#: device that boots before its network. Measured: with the network blocked at
#: startup the state's weather is `None`, and at the ten-minute cache interval
#: it stays `None` for ten minutes after the link comes back — so the night
#: screensaver shows no temperature at all, with nothing on screen or in the
#: log to say why. A minute is short enough that nobody notices and far too
#: slow to trouble a provider that allows unlimited requests.
WEATHER_RETRY_S = 60.0

#: How often to re-probe a subsystem that reported degraded at startup.
#:
#: Only runs while something *is* degraded, so a healthy device pays nothing.
#: Five minutes because the thing being corrected is a banner across the top of
#: the screen, not a decision.
RECHECK_S = 300.0


class Housekeeping:
    """A daemon thread running the assistant's periodic maintenance.

    Takes the assistant rather than the four objects, because the work is
    defined in terms of the assistant's own methods and this way there is one
    place naming what "idle housekeeping" means.
    """

    def __init__(self, assistant, weather_seconds: float, tick_s: float = TICK_S):
        self.assistant = assistant
        self.weather_seconds = weather_seconds
        self.tick_s = tick_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_weather = time.monotonic()
        self._last_recheck = time.monotonic()
        #: Whether the last weather refresh produced a reading, or None for
        #: "this thread has not checked yet". Anything but True means retry on
        #: the short interval rather than the cache one.
        #:
        #: **Starts un-true, and that is load-bearing.** The startup fetch is
        #: `Assistant.start()`'s, not this thread's, so beginning at True means
        #: believing a reading exists without ever having checked — and a
        #: device that booted with no network then waits the *full cache
        #: interval* before its first retry. Measured after the first attempt
        #: at this fix: network restored, and `/api/state` still had no weather
        #: 75 seconds later because the timer was still the 600 s one.
        #:
        #: Starting pessimistic costs one extra call a minute after boot, and
        #: that call is served from `WeatherService`'s own cache when the
        #: startup fetch worked — so it makes no request and immediately puts
        #: the interval back to the slow one.
        #:
        #: None rather than False so the log can tell "we have not looked" from
        #: "we looked and there was nothing", and announce only real changes.
        self._weather_ok: bool | None = None
        self._ticks = 0
        self._failures = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="aipi5-housekeeping",
                                        daemon=True)
        self._thread.start()
        log.info("housekeeping every %.0fs, independent of the microphone",
                 self.tick_s)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def describe(self) -> dict:
        return {"running": self.running, "ticks": self._ticks,
                "failures": self._failures}

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.tick_s)

    def tick(self, now: float | None = None) -> None:
        """One round of maintenance. Public so a test can drive it directly.

        Each item is guarded on its own. One subsystem throwing must not stop
        the other three — that is the whole failure this module was written
        for, and repeating it inside the fix would be the obvious way to get it
        wrong.
        """
        now = time.monotonic() if now is None else now
        self._ticks += 1

        self._guard("sweeping expired calls", self.assistant.call_hub.sweep)
        self._guard("retrying the camera", self.assistant.camera.retry_reclaim)

        # Two intervals, chosen by whether there is anything to go stale.
        due = self.weather_seconds if self._weather_ok else WEATHER_RETRY_S
        if now - self._last_weather > due:
            self._last_weather = now
            got = self._guard("refreshing the weather",
                              self.assistant.refresh_weather)
            was, self._weather_ok = self._weather_ok, bool(got)
            # Only real changes are announced. `was is None` is the first
            # check of the process: worth a line when it finds nothing, and
            # not worth one when it finds the reading `start()` already got.
            if self._weather_ok and was is False:
                log.info("the weather is available again")
            elif not self._weather_ok and was is not False:
                log.info("no weather right now; retrying every %.0fs until "
                         "there is some", WEATHER_RETRY_S)

        # A subsystem that was degraded at startup and has since recovered.
        # Only while something is degraded, so this costs nothing normally.
        if now - self._last_recheck > RECHECK_S:
            self._last_recheck = now
            self._guard("re-checking degraded subsystems",
                        self.assistant.recheck_degraded)

        # Last, and always: it reconciles the call state and publishes the UI
        # snapshot, so it should carry whatever the three above just changed.
        self._guard("publishing state", self.assistant.on_call_change)

    def _guard(self, what: str, call):
        """Run one job, swallowing anything it throws. Returns its result."""
        try:
            return call()
        except Exception:
            self._failures += 1
            if self._failures <= QUIET_AFTER:
                log.exception("housekeeping: %s failed", what)
            elif self._failures == QUIET_AFTER + 1:
                log.error("housekeeping: %s is still failing; further "
                          "tracebacks suppressed", what)
            return None
