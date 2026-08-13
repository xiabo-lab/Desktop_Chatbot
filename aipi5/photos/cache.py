"""The photographs on this device, and the two ceilings they live under.

Section 8 asks for a cache so the slideshow is smooth on a slow link. Section 9
asks for it to be bounded so it cannot fill the SD card. Both are here, plus a
third thing neither section asks for but the Picker API makes true: **this is
the only durable copy**. A picked photograph is reachable from Google only
while its session lives, so a file evicted from here is gone until somebody
picks again.

    ~/.cache/aipi5/photos/
        manifest.json         what is here, which collection it came from
        a1b2c3d4e5f6….jpg     one file per photograph, named by digest

**Two ceilings, and the tighter one wins.** A count is what a person can reason
about — "about three hundred pictures" — and a byte total is what actually
protects the card, because one photograph is not the size of another.

**Eviction never touches Google Photos.** Section 9 is explicit and it is worth
restating in the code that does the deleting: everything here is a local copy,
`delete` unlinks a file in this directory and nothing else, and no method in
this module makes a network call at all.

The manifest is rewritten whole, to a neighbouring file, and renamed. It is a
few hundred kilobytes at most and the alternative — an index that can disagree
with the directory after a power cut — is the kind of state that needs a repair
path nobody will ever test. `verify()` reconciles the two at startup anyway,
because a photo frame reboots by having its plug pulled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"

#: What a cached file may be called, and the only thing the HTTP route will
#: serve. A digest and an extension — no dots, no slashes, nothing to traverse
#: with. The route checks against this rather than against the manifest so a
#: crafted name is refused before anything touches the filesystem.
NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp)$")

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heif": ".jpg",   # the API transcodes when a size is requested
    "image/heic": ".jpg",
}


def file_name(media_id: str, mime_type: str) -> str:
    """A stable local name for one Google media item.

    A digest of the item id rather than the id itself: those are long, contain
    characters that are awkward in a URL, and are not ours to write into a
    directory listing anybody with the device can read.
    """
    digest = hashlib.sha256(media_id.encode("utf-8")).hexdigest()[:32]
    return digest + EXTENSIONS.get(mime_type, ".jpg")


@dataclass
class CachedPhoto:
    """One photograph on disk."""

    media_id: str
    name: str
    collection: str
    bytes: int = 0
    created: str = ""
    width: int = 0
    height: int = 0
    #: When this device fetched it. The eviction order, and deliberately not
    #: the photograph's own date — evicting by how old the *picture* is would
    #: quietly delete exactly the old family photographs somebody chose this
    #: feature to see.
    added: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "media_id": self.media_id, "name": self.name,
            "collection": self.collection, "bytes": self.bytes,
            "created": self.created, "width": self.width,
            "height": self.height, "added": self.added,
        }

    @classmethod
    def parse(cls, raw: dict) -> "CachedPhoto | None":
        name = str(raw.get("name", ""))
        if not NAME.match(name):
            return None
        return cls(
            media_id=str(raw.get("media_id", "")), name=name,
            collection=str(raw.get("collection", "")),
            bytes=int(raw.get("bytes") or 0),
            created=str(raw.get("created", "")),
            width=int(raw.get("width") or 0),
            height=int(raw.get("height") or 0),
            added=float(raw.get("added") or 0.0),
        )


@dataclass
class Collection:
    """One picking session's worth of photographs, with a name on it.

    The nearest thing the Picker API allows to section 7's album. It is a
    snapshot — the person picked these photographs at this moment — and it is
    remembered across reboots, which is the promise section 7 actually cares
    about.
    """

    id: str
    name: str
    session_id: str = ""
    created: float = field(default_factory=time.time)
    #: How many photographs the pick contained, which is not how many are
    #: cached: the ceilings may have stopped the sync short, and the settings
    #: page should say so rather than quietly showing fewer.
    picked: int = 0
    #: False once Google stops answering for the session. Everything already
    #: downloaded keeps working; nothing new can arrive.
    live: bool = True
    synced: float = 0.0

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "session_id": self.session_id,
                "created": self.created, "picked": self.picked,
                "live": self.live, "synced": self.synced}

    @classmethod
    def parse(cls, raw: dict) -> "Collection | None":
        ident = str(raw.get("id", ""))
        if not ident:
            return None
        return cls(id=ident, name=str(raw.get("name") or "Photos"),
                   session_id=str(raw.get("session_id", "")),
                   created=float(raw.get("created") or 0.0),
                   picked=int(raw.get("picked") or 0),
                   live=bool(raw.get("live", False)),
                   synced=float(raw.get("synced") or 0.0))


class PhotoCache:
    """The directory, the manifest, and the ceilings.

    One lock over everything. The sync thread writes, the HTTP handler reads
    the listing, and the operations are all short — no I/O of any size happens
    inside the lock except the manifest write, which is one small file.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg.cache_dir).expanduser()
        self._lock = threading.RLock()
        self._photos: dict[str, CachedPhoto] = {}
        self._collections: dict[str, Collection] = {}
        self._selected: list[str] = []
        self._loaded = False
        self.error = ""

    # ── opening ──────────────────────────────────────────────────────

    def open(self) -> bool:
        """Create the directory, read the manifest, reconcile with the disk."""
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.error = f"cannot use {self.root}: {exc}"
                log.error("google photos: %s", self.error)
                return False
            self._read()
            self.verify()
            self._loaded = True
            log.info("google photos: %d photos cached (%s) in %s",
                     len(self._photos), _mb(self.size_bytes()), self.root)
            return True

    @property
    def ready(self) -> bool:
        return self._loaded and not self.error

    def _read(self) -> None:
        path = self.root / MANIFEST
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # A truncated manifest after a power cut. The photographs are
            # still there; `verify` adopts them rather than deleting a
            # directory of somebody's family pictures over a JSON error.
            log.warning("google photos: %s is unreadable (%s); rebuilding it "
                        "from the files on disk", path, exc)
            return
        if not isinstance(raw, dict):
            return
        for entry in raw.get("photos") or []:
            photo = CachedPhoto.parse(entry) if isinstance(entry, dict) else None
            if photo is not None:
                self._photos[photo.media_id or photo.name] = photo
        for entry in raw.get("collections") or []:
            album = Collection.parse(entry) if isinstance(entry, dict) else None
            if album is not None:
                self._collections[album.id] = album
        self._selected = [str(c) for c in (raw.get("selected") or [])
                          if str(c) in self._collections]

    def _write(self) -> None:
        """Rewrite the manifest. Caller holds the lock."""
        path = self.root / MANIFEST
        body = json.dumps({
            "version": 1,
            "collections": [c.as_dict() for c in self._collections.values()],
            "selected": list(self._selected),
            "photos": [p.as_dict() for p in self._photos.values()],
        }, ensure_ascii=False)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(body, encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            log.warning("google photos: could not write %s: %s", path, exc)

    def verify(self) -> None:
        """Make the manifest and the directory agree. Caller holds the lock.

        Both directions. A file the manifest names and the disk does not is
        dropped from the index; a file on the disk the manifest does not name
        is *adopted* rather than deleted, so a lost manifest costs the
        collection labels and not the photographs.
        """
        on_disk = set()
        try:
            for entry in self.root.iterdir():
                if entry.is_file() and NAME.match(entry.name):
                    on_disk.add(entry.name)
        except OSError as exc:
            self.error = f"cannot read {self.root}: {exc}"
            log.error("google photos: %s", self.error)
            return

        missing = [key for key, photo in self._photos.items()
                   if photo.name not in on_disk]
        for key in missing:
            self._photos.pop(key, None)
        if missing:
            log.info("google photos: %d cached photos are no longer on disk",
                     len(missing))

        known = {photo.name for photo in self._photos.values()}
        orphans = sorted(on_disk - known)
        for name in orphans:
            try:
                size = (self.root / name).stat().st_size
            except OSError:
                continue
            # Keyed by name, because there is no media id to recover. It will
            # never match a Google item again, so a later sync re-downloads
            # that photograph — which is correct, and costs one file.
            self._photos[name] = CachedPhoto(
                media_id="", name=name, collection="", bytes=size,
                added=(self.root / name).stat().st_mtime)
        if orphans:
            log.info("google photos: adopted %d photos the manifest did not "
                     "know about", len(orphans))
        if missing or orphans:
            self._write()

    # ── reading ──────────────────────────────────────────────────────

    def size_bytes(self) -> int:
        with self._lock:
            return sum(photo.bytes for photo in self._photos.values())

    def count(self) -> int:
        with self._lock:
            return len(self._photos)

    def has(self, media_id: str) -> bool:
        with self._lock:
            return media_id in self._photos

    def revision(self) -> str:
        """A cheap value that changes whenever the playlist would.

        Carried on the state poll the page already makes, the same trick the
        transfer folder uses: the slideshow re-reads its list only when there
        is a different one to read, rather than fetching a few hundred
        filenames twice a second for the sake of the photograph that arrives
        once an hour.
        """
        with self._lock:
            newest = max((p.added for p in self._photos.values()), default=0.0)
            return f"{len(self._photos)}:{int(newest)}:{','.join(self._selected)}"

    def resolve(self, name: str) -> Path | None:
        """The path a URL name refers to, or None.

        The regex is the whole of the safety here and it is checked before the
        filesystem is touched: a name that is 32 hex characters and a known
        extension cannot escape the directory, whatever it is joined to.
        """
        if not NAME.match(name or ""):
            return None
        path = self.root / name
        return path if path.is_file() else None

    def playlist(self) -> list[dict]:
        """What the slideshow draws from: the selected collections' photos.

        Ordered oldest-added first so the page's shuffle has a stable list to
        work from — the shuffle is the page's, per section 12, and a source
        list that reordered itself on every poll would defeat the "do not
        repeat until the set has been shown" half of it.

        A cache with nothing selected falls back to everything it has. That is
        the case where a collection was deleted but its photographs are still
        on disk, and a black screen would be the wrong answer to it.
        """
        with self._lock:
            chosen = set(self._selected)
            photos = [p for p in self._photos.values()
                      if not chosen or p.collection in chosen]
            if not photos:
                photos = list(self._photos.values())
            photos.sort(key=lambda p: p.added)
            return [{
                "name": photo.name,
                "created": photo.created,
                "album": (self._collections[photo.collection].name
                          if photo.collection in self._collections else ""),
                "width": photo.width,
                "height": photo.height,
            } for photo in photos]

    def describe(self) -> dict:
        with self._lock:
            return {
                "root": str(self.root),
                "ready": self.ready,
                "error": self.error,
                "photos": len(self._photos),
                "bytes": self.size_bytes(),
                "max_photos": self.cfg.max_photos,
                "max_bytes": self.cfg.max_cache_mb * 1024 * 1024,
                # Reported because it is the setting that explains a cache
                # sitting above its own ceiling, which otherwise looks broken.
                "min_photos": self.cfg.min_photos,
                "collections": [c.as_dict() for c in self._collections.values()],
                "selected": list(self._selected),
            }

    # ── collections ──────────────────────────────────────────────────

    def collections(self) -> list[Collection]:
        with self._lock:
            return sorted(self._collections.values(),
                          key=lambda c: c.created, reverse=True)

    def selected(self) -> list[str]:
        with self._lock:
            return list(self._selected)

    def add_collection(self, album: Collection, *, select: bool = True) -> None:
        with self._lock:
            self._collections[album.id] = album
            if select and album.id not in self._selected:
                # Replaces rather than appends, which is what "Change Album"
                # means to somebody pressing it. Selecting several is done by
                # `select()` from the settings page, deliberately, so the
                # common case does not silently accumulate collections until
                # the cache is full of sets nobody remembers choosing.
                self._selected = [album.id]
            self._write()

    def update_collection(self, ident: str, **fields) -> None:
        with self._lock:
            album = self._collections.get(ident)
            if album is None:
                return
            for key, value in fields.items():
                if hasattr(album, key):
                    setattr(album, key, value)
            self._write()

    def select(self, ids: list[str]) -> list[str]:
        with self._lock:
            self._selected = [i for i in ids if i in self._collections]
            self._write()
            return list(self._selected)

    def forget_collection(self, ident: str) -> int:
        """Drop a collection and every photograph that came with it.

        Local only. The photographs remain in the person's Google Photos
        account, untouched — this deletes copies.
        """
        with self._lock:
            self._collections.pop(ident, None)
            self._selected = [i for i in self._selected if i != ident]
            gone = [key for key, photo in self._photos.items()
                    if photo.collection == ident]
            for key in gone:
                self._remove(key)
            self._write()
            if gone:
                log.info("google photos: removed %d local copies from a "
                         "collection that was dropped", len(gone))
            return len(gone)

    def clear(self) -> int:
        """Everything. Used by Disconnect, and only local copies."""
        with self._lock:
            count = len(self._photos)
            for key in list(self._photos):
                self._remove(key)
            self._collections.clear()
            self._selected = []
            self._write()
            log.info("google photos: cleared %d local copies", count)
            return count

    # ── writing ──────────────────────────────────────────────────────

    def room_for(self, size_hint: int = 0) -> bool:
        """Whether another photograph fits under both ceilings."""
        with self._lock:
            if len(self._photos) >= self.cfg.max_photos:
                return False
            limit = self.cfg.max_cache_mb * 1024 * 1024
            return self.size_bytes() + max(0, size_hint) <= limit

    def store(self, item, data: bytes, collection: str) -> CachedPhoto | None:
        """Write one photograph in, evicting what is no longer wanted first.

        Returns None when there is no room, which is a normal outcome and not
        an error: a person who picked more photographs than the ceiling allows
        gets the ceiling's worth, and `service.py` says so once in the log.
        """
        name = file_name(item.id, item.mime_type)
        with self._lock:
            self._make_room(len(data))
            if not self.room_for(len(data)):
                return None
            path = self.root / name
            temporary = path.with_suffix(path.suffix + ".part")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                log.warning("google photos: could not save a photo: %s", exc)
                return None
            photo = CachedPhoto(
                media_id=item.id, name=name, collection=collection,
                bytes=len(data), created=item.created,
                width=item.width, height=item.height)
            self._photos[item.id] = photo
            self._write()
            return photo

    def _make_room(self, wanted: int) -> None:
        """Evict what belongs to no selected collection. Lock held.

        **Only unselected photographs.** Making room by deleting from the set
        the person is currently watching would mean two selected collections
        that together exceed the ceiling take turns evicting each other, and
        the device spends its life re-downloading the same photographs on a
        connection section 8 says may be a hotspot. When there is nothing
        unselected left to drop, the sync simply stops — which is bounded,
        predictable, and logged.
        """
        chosen = set(self._selected)
        if not chosen:
            # Nothing selected means everything plays — the same fallback
            # `playlist` makes, and it has to be the same one. Read the other
            # way round, an empty selection made *every* photograph droppable,
            # so the ceiling could always be satisfied by deleting the whole
            # cache and the limits stopped being limits. Found by the test
            # below, not on the device, which is the point of having it.
            return
        droppable = [p for p in self._photos.values()
                     if p.collection not in chosen]
        droppable.sort(key=lambda p: p.added)
        for photo in droppable:
            if self.room_for(wanted):
                return
            self._remove(photo.media_id or photo.name)

    def enforce_limits(self) -> int:
        """Trim to the ceilings, and stop at the floor.

        Used only when the limits themselves have been lowered — the ceiling
        changed in the YAML and the cache is now above it — so it runs at
        startup rather than on every sync.

        **The floor is the important part and it outranks both ceilings.**
        Unselected photographs go first and go entirely. Only then does this
        touch the set the slideshow is actually playing, and it stops at
        `min_photos` whatever the ceiling says, because a number edited in a
        config file must not be able to empty the collection somebody chose
        with their phone. Those photographs are frequently unrecoverable: the
        picking session behind them has expired, so a deleted local copy is
        gone until somebody picks again.

        When the floor and the ceiling genuinely conflict, the cache stays
        over its limit and says so. That is the honest outcome — the
        alternative is a device that silently throws away the feature.
        """
        with self._lock:
            limit = self.cfg.max_cache_mb * 1024 * 1024
            chosen = set(self._selected)

            def over() -> bool:
                return (len(self._photos) > self.cfg.max_photos
                        or self.size_bytes() > limit)

            def sweep(candidates, floor: int) -> int:
                gone = 0
                for photo in sorted(candidates, key=lambda p: p.added):
                    if not over() or len(self._photos) <= floor:
                        break
                    self._remove(photo.media_id or photo.name)
                    gone += 1
                return gone

            # Unselected first, with no floor: these are the leftovers of a
            # collection nobody is playing.
            removed = sweep([p for p in self._photos.values()
                             if chosen and p.collection not in chosen], 0)
            # Then, reluctantly, the selected set — down to the floor and no
            # further.
            #
            # **The floor is used as written, never clamped to the ceiling.**
            # Clamping would defeat it in precisely the case it exists for: a
            # `max_photos` edited down to something small is exactly when a
            # chosen set is at risk, and a floor that quietly shrinks to match
            # the new ceiling protects nothing. Floor above ceiling therefore
            # means the cache stays over its limit, and says so below.
            floor = max(0, self.cfg.min_photos)
            removed += sweep(list(self._photos.values()), floor)

            if removed:
                self._write()
                log.info("google photos: evicted %d photos to stay under "
                         "%d files / %d MB", removed, self.cfg.max_photos,
                         self.cfg.max_cache_mb)
            if over():
                log.warning(
                    "google photos: the cache is above its limit (%d photos, "
                    "%s) and cleanup has stopped at the %d-photo floor rather "
                    "than delete more of the chosen set. Raise "
                    "photos.max_photos / photos.max_cache_mb, or choose fewer "
                    "photos.", len(self._photos), _mb(self.size_bytes()), floor)
            return removed

    def _remove(self, key: str) -> None:
        """Unlink one local copy. Lock held. Never touches Google."""
        photo = self._photos.pop(key, None)
        if photo is None:
            return
        try:
            (self.root / photo.name).unlink(missing_ok=True)
        except OSError as exc:
            log.debug("google photos: could not delete %s: %s", photo.name, exc)


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"
