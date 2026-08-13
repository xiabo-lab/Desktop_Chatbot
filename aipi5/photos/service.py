"""Authorisation, picking and syncing, on one background thread.

**One thread, one loop, for the whole feature.** Section 28 asks for timers not
to accumulate over a device that runs for months, and the way to be sure of
that is to have exactly one and to be able to point at it. It wakes on an
event, does whatever is due — poll a picking session, download what is missing,
trim the cache — and sleeps again. Nothing else here starts a thread and
nothing schedules a callback.

The loop is deliberately lazy. When there is no picking session open and every
photograph of every live collection is already on disk, it has nothing to do
and sleeps for `sync_minutes`; when a session has expired there is nothing it
*can* do and it stops asking. A device that has been running since March makes
no Google API calls at all, which is section 28's "excessive Google API calls"
answered by there being none.

**The slideshow never waits on this.** The page draws from the cache through
`aipi5/ui/server.py`; this thread only fills it. Every failure path therefore
ends in "keep what we have and try later", which is also what section 18 asks
for when the internet is gone.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from aipi5.photos.auth import GoogleAuth, GoogleAuthError
from aipi5.photos.cache import Collection, PhotoCache
from aipi5.photos.picker import PickerClient, PickerError

log = logging.getLogger(__name__)

#: How long to keep polling a picking session nobody finished. The API sends
#: its own `timeoutIn`, which is honoured; this is the backstop for a session
#: that does not carry one, and it is short because the QR code is on a screen
#: in somebody's living room and should not stay there all afternoon.
PICK_TIMEOUT_S = 900.0

#: Between downloads. Not politeness to Google — a few hundred requests is
#: nothing — but to the Pi: the slideshow, the person detector and the wake
#: recogniser share four cores, and a sync that saturates the link makes the
#: page's own polls late.
DOWNLOAD_GAP_S = 0.25

#: How long after a failed sync before trying again. Short enough that a Wi-Fi
#: outage of a few minutes costs nothing, long enough that a revoked grant does
#: not produce a request a second forever.
RETRY_S = 300.0


@dataclass
class SyncResult:
    """What one pass over a collection actually managed.

    Three numbers rather than one, because they mean different things to the
    person who just chose 130 photographs and got 126. `failed` is a download
    that did not work and will be retried; `no_room` is the cache ceiling,
    which retrying will not fix. **A partial result is kept, never discarded**
    — section 7 of the follow-up, and it is the difference between "four
    photos could not be downloaded" and losing the other 126.
    """

    stored: int = 0
    failed: int = 0
    no_room: int = 0
    errors: list = field(default_factory=list)

    def add(self, other: "SyncResult") -> "SyncResult":
        self.stored += other.stored
        self.failed += other.failed
        self.no_room += other.no_room
        self.errors.extend(other.errors)
        return self

    def as_dict(self) -> dict:
        return {"stored": self.stored, "failed": self.failed,
                "no_room": self.no_room,
                "error": self.errors[0] if self.errors else ""}


class GooglePhotosService:
    """The whole of the Google side, behind about eight methods.

    Built even when the feature is off or unauthorised, so the settings page
    can say *why* rather than showing nothing — the same reasoning the call
    server is built unconditionally.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.auth = GoogleAuth(cfg)
        self.cache = PhotoCache(cfg)
        self.client = PickerClient(self.auth)

        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.Lock()

        #: The picking session in progress, if any.
        self._pick: dict = {}
        self._sync_error = ""
        self._last_sync = 0.0
        self._next_sync = 0.0
        #: Said once per collection rather than once per sync, so a cache that
        #: is legitimately full does not write a line an hour forever.
        self._warned_full: set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> bool:
        """Open the cache and start the worker. False only if the cache cannot
        be opened, which is a disk problem and not a Google one."""
        if not self.cfg.enabled:
            log.info("google photos: turned off in the configuration")
            return False
        if not self.cache.open():
            return False
        # A ceiling that was lowered in the YAML applies at the next start
        # rather than at some arbitrary later moment.
        self.cache.enforce_limits()
        self._thread = threading.Thread(target=self._run, name="aipi5-photos",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        self.client.close()
        self.auth.close()

    @property
    def ready(self) -> bool:
        """Whether the slideshow has anything to show.

        Deliberately about the *cache* and not about Google. A device with a
        revoked token and three hundred photographs on disk is ready; one
        freshly authorised with an empty cache is not. This is what
        `ScreensaverManager` asks before choosing the daytime screensaver, so
        it has to mean "there are pictures", not "we could get pictures".
        """
        return self.cfg.enabled and self.cache.ready and self.cache.count() > 0

    # ── the settings page's verbs ────────────────────────────────────

    def begin_pick(self) -> dict:
        """Open a picking session and hand back the URL for the QR code.

        Returns the same shape as `pick_status`, so the page has one thing to
        render either way.

        **An existing session in flight is returned, not replaced.** This
        used to open a new one unconditionally, and the failure that produced
        is worth writing down because it is silent at both ends: the service
        tracks one `_pick`, so a second `begin_pick` displaces the first, and
        the phone that is *already in the Google picker* goes on to submit a
        selection into a session nobody is polling any more. The photographs
        simply never appear, with nothing wrong in any log. Measured for real
        on 2026-08-13 — two `Choose Photos` three seconds apart, one from the
        panel and one from a script, and the person's selection was lost.

        So pressing the button twice now re-shows the same code, which is also
        what somebody who pressed it twice actually wanted. `cancel_pick` is
        the explicit way to abandon one.
        """
        if not self.auth.authorised:
            return self._pick_error(self.auth.error or "not authorised")

        with self._lock:
            waiting = (self._pick.get("state") == "waiting"
                       and time.monotonic() < float(self._pick.get("deadline", 0)))
        if waiting:
            log.info("google photos: a picking session is already open; "
                     "showing the same code rather than starting another")
            return self.pick_status()

        try:
            session = self.client.create()
        except (PickerError, GoogleAuthError) as exc:
            return self._pick_error(str(exc))
        with self._lock:
            self._pick = {
                "session": session.id,
                "uri": session.picker_uri,
                "started": time.monotonic(),
                "poll_s": max(2.0, session.poll_interval_s),
                "deadline": time.monotonic() + min(session.timeout_s,
                                                   PICK_TIMEOUT_S),
                "state": "waiting",
                "error": "",
                "picked": 0,
            }
        self._wake.set()
        return self.pick_status()

    def cancel_pick(self) -> dict:
        """Give up on a pick in progress, and tell Google to forget it.

        The QR code on the screen authorises picking into that session, so a
        cancelled one is deleted rather than just forgotten — otherwise a
        photograph of the panel taken a minute ago still works.
        """
        with self._lock:
            pick, self._pick = self._pick, {}
        if pick.get("session") and pick.get("state") == "waiting":
            self.client.delete(str(pick["session"]))
        return self.pick_status()

    def dismiss_pick(self) -> dict:
        """Clear a *finished* pick's report, leaving any live session alone.

        Separate from `cancel_pick` because pressing Close on "126 photos
        ready" must not be able to delete a session — and because the two are
        reached by the same button, whose label is the only thing that
        distinguishes them. Getting that wrong would be silent.
        """
        with self._lock:
            if self._pick.get("state") in ("done", "empty", "error", "timeout"):
                self._pick = {}
        return self.pick_status()

    def pick_waiting(self) -> bool:
        """Whether a QR code is on the screen waiting to be scanned.

        Asked on every state publish, so it is a lock and a dict lookup and
        nothing else. The screensaver holds off while this is true — section
        19's "important system dialog": a code somebody is walking across the
        room to photograph must not be replaced by a clock after sixty
        seconds.
        """
        with self._lock:
            return (self._pick.get("state") == "waiting"
                    and time.monotonic() < float(self._pick.get("deadline", 0)))

    def pick_status(self) -> dict:
        """What the picking flow is doing, for the settings page's poll.

        **The picker URL is in here**, which is the one piece of this payload
        that is sensitive: it authorises picking into this session. It is only
        ever served over the loopback UI server — the same reasoning the rest
        of that server carries — and it is never logged.
        """
        with self._lock:
            pick = dict(self._pick)
        if not pick:
            return {"state": "idle", "uri": "", "picked": 0, "cached": 0,
                    "stored": 0, "failed": 0, "no_room": 0, "error": ""}
        return {
            "state": str(pick.get("state", "waiting")),
            "uri": str(pick.get("uri", "")),
            "picked": int(pick.get("picked") or 0),
            # How many are on the device now, and how the last run went. The
            # screen shows these rather than the number chosen: they differ
            # exactly when something went wrong, which is when it matters.
            "cached": int(pick.get("cached") or 0),
            "stored": int(pick.get("stored") or 0),
            "failed": int(pick.get("failed") or 0),
            "no_room": int(pick.get("no_room") or 0),
            "error": str(pick.get("error", "")),
            "waiting_s": round(time.monotonic() - float(pick.get("started", 0))),
        }

    def _pick_error(self, message: str) -> dict:
        with self._lock:
            self._pick = {"state": "error", "error": message, "uri": "",
                          "picked": 0, "started": time.monotonic()}
        log.warning("google photos: %s", message)
        return self.pick_status()

    def select(self, ids: list[str]) -> list[str]:
        chosen = self.cache.select(ids)
        self.sync_soon()
        return chosen

    def forget_collection(self, ident: str) -> int:
        return self.cache.forget_collection(ident)

    def disconnect(self) -> None:
        """Sign out: revoke, forget the token, drop the local copies.

        The copies go too, and that is the right default rather than a
        surprise: they are somebody's family photographs on a device they have
        just said they want disconnected from their account.
        """
        self.cancel_pick()
        self.auth.forget()
        self.cache.clear()

    def sync_soon(self) -> None:
        self._next_sync = 0.0
        self._wake.set()

    def reload_auth(self) -> None:
        self.auth.reload()
        self.sync_soon()

    def describe(self) -> dict:
        """`/api/system` and the settings page."""
        return {
            "enabled": self.cfg.enabled,
            "ready": self.ready,
            "interval_s": self.cfg.interval_seconds,
            "transition_ms": self.cfg.transition_ms,
            "shuffle": self.cfg.shuffle,
            "show_info": self.cfg.show_info,
            "auth": self.auth.describe(),
            "cache": self.cache.describe(),
            "pick": self.pick_status(),
            "sync": {
                "error": self._sync_error,
                "last": self._last_sync,
                "running": self._thread is not None and self._thread.is_alive(),
            },
        }

    # ── the worker ───────────────────────────────────────────────────

    def _run(self) -> None:
        log.info("google photos: sync thread started")
        while not self._stopping.is_set():
            delay = 60.0
            try:
                delay = self._tick()
            except Exception:
                # A background thread that dies takes the whole feature with
                # it silently. Nothing in `_tick` should raise — every call in
                # it catches — so this is the backstop that turns a bug into a
                # traceback in the journal and a slideshow that carries on
                # from the cache.
                log.exception("google photos: the sync thread hit an error")
                delay = RETRY_S
            self._wake.wait(timeout=max(1.0, delay))
            self._wake.clear()
        log.info("google photos: sync thread stopped")

    def _tick(self) -> float:
        """One pass. Returns how long to sleep before the next one."""
        if self._poll_pick():
            # A pick is in progress: come back on its polling interval and do
            # nothing else. Downloading while somebody is still choosing would
            # only compete with them for the link.
            with self._lock:
                return float(self._pick.get("poll_s", 5.0))

        if not self.auth.authorised:
            return RETRY_S
        if time.monotonic() < self._next_sync:
            return max(1.0, self._next_sync - time.monotonic())
        # `_sync` returns what it managed, and sets `_next_sync` to when it
        # should run again — retry soon after a failure, an hour otherwise.
        # Reading the deadline back rather than returning it keeps one place
        # deciding the cadence.
        self._sync()
        return max(1.0, self._next_sync - time.monotonic())

    # ── picking ──────────────────────────────────────────────────────

    def _poll_pick(self) -> bool:
        """True while a picking session is still being waited on."""
        with self._lock:
            pick = dict(self._pick)
        if not pick or pick.get("state") not in ("waiting",):
            return False

        if time.monotonic() > float(pick.get("deadline", 0)):
            log.info("google photos: nobody finished picking; the session was "
                     "abandoned")
            self.client.delete(str(pick["session"]))
            with self._lock:
                self._pick = {"state": "timeout", "uri": "", "picked": 0,
                              "error": "nobody finished choosing photos",
                              "started": pick.get("started", 0)}
            return False

        try:
            session = self.client.get(str(pick["session"]))
        except PickerError as exc:
            if exc.expired:
                with self._lock:
                    self._pick = {"state": "timeout", "uri": "", "picked": 0,
                                  "error": "the picking session expired",
                                  "started": pick.get("started", 0)}
                return False
            log.debug("google photos: polling the session failed (%s)", exc)
            return True
        if not session.media_items_set:
            return True

        self._collect(str(pick["session"]))
        return False

    def _collect(self, session_id: str) -> None:
        """Somebody finished picking. Turn it into a collection and sync it."""
        try:
            picked = self.client.items(session_id)
        except PickerError as exc:
            self._pick_error(f"could not read the selection: {exc}")
            return
        if not picked.items:
            with self._lock:
                self._pick = {"state": "empty", "uri": "", "picked": 0,
                              "error": "no photos were chosen",
                              "started": time.monotonic()}
            self.client.delete(session_id)
            return

        album = Collection(
            id=f"c{int(time.time())}",
            name=_collection_name(),
            session_id=session_id,
            picked=len(picked.items),
        )
        self.cache.add_collection(album)
        log.info("google photos: %d photos were chosen; they are now the "
                 "slideshow's source", len(picked.items))
        with self._lock:
            self._pick = {"state": "syncing", "uri": "",
                          "picked": len(picked.items), "error": "",
                          "started": time.monotonic()}
        self._next_sync = 0.0
        # Straight into the download rather than waiting for the next sync
        # window: the session is on a clock and everything it holds becomes
        # unreachable when it expires.
        result = self._sync()

        # `done` rather than back to `idle`, because the person is standing in
        # front of the screen waiting to be told it worked. The count is the
        # answer to the only question they have, and it is the *cached* count
        # rather than the picked one — those differ when a download failed or
        # the cache filled, and reporting the number they chose would be
        # reporting something the device does not actually have.
        with self._lock:
            if self._pick.get("state") == "syncing":
                self._pick = {
                    "state": "done", "uri": "", "error": "",
                    "picked": len(picked.items),
                    "cached": self.cache.count(),
                    "started": time.monotonic(),
                    **result.as_dict(),
                }
        log.info("google photos: %d of %d chosen photos are on the device"
                 "%s%s", result.stored, len(picked.items),
                 f", {result.failed} failed" if result.failed else "",
                 f", {result.no_room} did not fit" if result.no_room else "")

    # ── syncing ──────────────────────────────────────────────────────

    def _sync(self) -> SyncResult:
        """Download whatever the live collections still owe.

        Returns what it managed rather than only a delay — the picking flow
        shows the person how many of their photographs are actually on the
        device, which it cannot do if this swallows the numbers.
        """
        result = SyncResult()
        live = [c for c in self.cache.collections() if c.live and c.session_id]
        if not live:
            self._sync_error = ""
            self._last_sync = time.time()
            self._next_sync = time.monotonic() + self.cfg.sync_minutes * 60
            return result

        for album in live:
            if self._stopping.is_set():
                break
            result.add(self._sync_one(album))

        self._last_sync = time.time()
        self._next_sync = time.monotonic() + (
            RETRY_S if self._sync_error else self.cfg.sync_minutes * 60)
        if result.stored or result.failed:
            log.info("google photos: %d new photos cached, %d failed "
                     "(%d on the device)",
                     result.stored, result.failed, self.cache.count())
        return result

    def _sync_one(self, album: Collection) -> SyncResult:
        result = SyncResult()
        try:
            picked = self.client.items(album.session_id)
        except PickerError as exc:
            if exc.expired:
                # The expected end of a collection's life, not a fault. What
                # is on disk stays there and is what the slideshow shows from
                # now on — which is the whole reason the cache exists.
                self.cache.update_collection(album.id, live=False)
                log.info("google photos: the session behind %r has expired; "
                         "its cached photos keep working offline (%d on the "
                         "device)", album.name, self.cache.count())
                return result
            self._sync_error = str(exc)
            result.errors.append(str(exc))
            log.warning("google photos: %s", exc)
            return result
        except GoogleAuthError as exc:
            self._sync_error = str(exc)
            result.errors.append(str(exc))
            return result

        self._sync_error = ""
        missing = [item for item in picked.items if not self.cache.has(item.id)]
        for index, item in enumerate(missing):
            if self._stopping.is_set():
                break
            if not self.cache.room_for():
                # Not a failure and not retryable — the ceiling is doing its
                # job. Counted separately so the screen can say "the cache is
                # full" rather than "4 downloads failed", which would send
                # somebody looking at their Wi-Fi.
                result.no_room = len(missing) - index
                if album.id not in self._warned_full:
                    self._warned_full.add(album.id)
                    log.warning(
                        "google photos: the cache is full at %d photos / %d MB, "
                        "so %d of the chosen photos were not downloaded. Raise "
                        "photos.max_photos or photos.max_cache_mb to keep more.",
                        self.cfg.max_photos, self.cfg.max_cache_mb,
                        result.no_room)
                break

            # **One photograph failing must not lose the others.** Each
            # download is caught on its own and counted; the loop carries on.
            # The two exceptions are an expired link and a dead grant, where
            # every remaining download would fail the same way and the right
            # thing is to stop and come back.
            try:
                data = self.client.download(item, self.cfg.download_size)
            except PickerError as exc:
                if exc.expired:
                    # A `baseUrl` older than an hour. Re-listing is the fix and
                    # the next pass does it; stopping here rather than looping
                    # keeps one slow sync from becoming a retry storm.
                    log.info("google photos: download links expired mid-sync; "
                             "%d cached so far, continuing on the next pass",
                             result.stored)
                    result.failed += len(missing) - index
                    return result
                if exc.status in (401, 403):
                    self._sync_error = str(exc)
                    result.errors.append(str(exc))
                    result.failed += len(missing) - index
                    return result
                result.failed += 1
                log.warning("google photos: a photo did not download (%s); "
                            "keeping the %d already cached", exc, result.stored)
                continue
            except GoogleAuthError as exc:
                self._sync_error = str(exc)
                result.errors.append(str(exc))
                result.failed += len(missing) - index
                return result

            if self.cache.store(item, data, album.id) is not None:
                result.stored += 1
            else:
                result.no_room += 1
            time.sleep(DOWNLOAD_GAP_S)

        self.cache.update_collection(album.id, synced=time.time())
        if result.stored:
            self._warned_full.discard(album.id)
        return result


def _collection_name(when: datetime | None = None) -> str:
    """What a picked set is called: `Photos · 13 Aug 2026`.

    Named by the day it was chosen because there is nothing else to name it by.
    The Picker API returns photographs, not the album they came from — see this
    package's docstring — and this device has no keyboard to type a name on.
    The settings page shows this string as "Album".

    `%-d` for `13` rather than `13`, and `%d` where that is not available:
    glibc understands the dash, Windows raises a `ValueError` for it, and the
    test suite runs in both places.
    """
    when = datetime.now() if when is None else when
    for pattern in ("%-d %b %Y", "%d %b %Y"):
        try:
            return f"Photos · {when.strftime(pattern)}"
        except ValueError:
            continue
    return "Photos"
