"""One directory on the Pi, and every rule about what may happen inside it.

The feature is a folder the phone can put things into and take things out of.
All of the filesystem thinking lives here so that neither web server has to do
any: they authenticate, they hand over a name and a stream, and they get back
either a result or an error they can report. Nothing above this module ever
builds a path.

**The directory is the boundary, and it is enforced by resolution rather than
by inspection.** A name is reduced to a bare filename, joined to the root, and
then *resolved* — symlinks and all — and the result must still be underneath
the root or nothing happens. Checking for `..` in the text instead is the
version of this that gets defeated by `%2e%2e`, by `....//`, by a symlink
somebody uploaded earlier, and by whichever encoding is next.

**An upload is never written to its final name.** It goes to a temporary file
in the same directory and is renamed only once it has arrived whole, because
the phone is on a train and half of the transfers in this feature's life will
be interrupted. `os.replace` is atomic on the same filesystem, so a name in the
listing is a complete file — there is no window in which a half-written video
looks like a video.

**Nothing here executes anything.** Files arrive, sit, and leave. That is worth
stating because the list of things people will send this thing includes `.deb`,
`.sh` and `.py`, and the mode is stripped to `0600` on the way in so that a
file cannot even be run by accident later.
"""

from __future__ import annotations

import errno
import logging
import mimetypes
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger("aipi5.files")

#: Filenames are bytes on disk. ext4 allows 255 of them, and a Chinese
#: character costs three, so this is a byte budget rather than a character one.
MAX_NAME_BYTES = 255

#: What an unfinished upload is called while it is arriving. The leading dot
#: keeps it out of the listing on its own merits, and the suffix is what
#: `sweep()` looks for after a crash.
TEMP_PREFIX = "."
TEMP_SUFFIX = ".uploading"

#: How long a temporary file has to be untouched before it is considered
#: abandoned. Longer than any plausible stall on a slow uplink, so a live
#: upload is never swept out from under itself.
STALE_TEMP_S = 6 * 3600

#: Characters that may not appear in a name here whatever the filesystem
#: thinks: the separators, and everything unprintable.
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f/\\]")

# Types the stdlib does not know about on a Raspberry Pi but a phone sends
# constantly. Registered rather than special-cased at the call site.
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("video/quicktime", ".mov")


class FileError(Exception):
    """Something the person can be told about. The message is user-facing."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class StoredFile:
    name: str
    size: int
    modified: float
    type: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "modified": self.modified,
            # ISO 8601 as well as the epoch, because one is for sorting and the
            # other is for reading, and computing the second in three different
            # places on two pages is how they end up disagreeing.
            "modified_iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                          time.localtime(self.modified)),
            "type": self.type,
        }


def sanitize_filename(raw: str) -> str:
    """A name that is certainly one file, inside one directory, or nothing.

    Unicode survives on purpose — 测试照片.jpg is a normal thing to send from
    this phone — so this removes structure rather than restricting the alphabet.
    """
    if not isinstance(raw, str):
        return ""
    # A Windows or iOS client may send a full path in the `filename` parameter;
    # both separators are taken as separators, whichever platform this runs on.
    name = raw.replace("\\", "/").split("/")[-1]
    # NFC first: 测试.jpg decomposed and composed are different byte strings
    # and would otherwise be two files with the same name on the screen.
    name = unicodedata.normalize("NFC", name)
    name = _FORBIDDEN.sub("", name).strip()
    # Leading dots would make it hidden — invisible in the listing it was
    # uploaded to appear in — and `.` and `..` are not names at all.
    name = name.lstrip(".").strip()
    if not name or name in (".", ".."):
        return ""
    return _fit(name)


def _fit(name: str) -> str:
    """Truncate to what the filesystem takes, keeping the extension."""
    if len(name.encode("utf-8")) <= MAX_NAME_BYTES:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or len(extension) > 16:
        stem, extension = name, ""
    budget = MAX_NAME_BYTES - len((("." + extension) if extension else "")
                                  .encode("utf-8"))
    encoded = stem.encode("utf-8")[:max(1, budget)]
    # The cut may have landed inside a character; drop the partial one.
    stem = encoded.decode("utf-8", "ignore") or "file"
    return f"{stem}.{extension}" if extension else stem


class FileStore:
    """The transfer directory. Created at startup, checked, and then used.

    Never raises on construction: a device whose file transfer could not start
    is a device that still answers questions, plays music and takes calls. The
    failure is recorded in `error` and reported by the startup checks.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.root: Path = Path(cfg.root).expanduser()
        self.error: str = ""
        self.ready: bool = False
        self._uploads = 0
        #: Bumped by every change this process makes, so `revision` is exact
        #: for them however coarse the filesystem's clock is.
        self._changes = 0
        import threading
        self._lock = threading.Lock()

    # ── startup ──────────────────────────────────────────────────────

    def start(self) -> bool:
        """Make the directory, prove it is writable, and say so in the log."""
        if not self.cfg.enabled:
            self.error = "file transfer is switched off in the configuration"
            log.info("file transfer: disabled by configuration")
            return False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # Writability is proved rather than assumed. `os.access` answers a
            # different question — it asks the permission bits, not the
            # filesystem, and is wrong on a read-only mount every time.
            probe = self.root / f".aipi5-write-test-{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            self.error = f"{self.root} is not writable: {exc.strerror or exc}"
            log.error("file transfer disabled: %s", self.error)
            return False

        self.ready = True
        self.error = ""
        storage = self.storage()
        log.info("file transfer: root=%s", self.root)
        log.info("file transfer: storage available=%s of %s",
                 human_size(storage["free"]), human_size(storage["total"]))
        swept = self.sweep()
        if swept:
            log.info("file transfer: removed %d abandoned upload(s)", swept)
        log.info("file transfer: ready (max upload %s, reserve %s)",
                 human_size(self.cfg.max_upload_bytes),
                 human_size(self.cfg.reserve_bytes))
        return True

    def sweep(self) -> int:
        """Delete temporary files left by uploads that never finished."""
        removed = 0
        now = time.time()
        for path in self._temp_files():
            try:
                if now - path.stat().st_mtime < STALE_TEMP_S:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def _temp_files(self) -> Iterator[Path]:
        try:
            for entry in self.root.iterdir():
                if entry.name.endswith(TEMP_SUFFIX) and entry.is_file():
                    yield entry
        except OSError:
            return

    # ── reading ──────────────────────────────────────────────────────

    def storage(self) -> dict:
        """Free and used bytes on the filesystem holding the directory."""
        try:
            usage = shutil.disk_usage(self.root)
        except OSError as exc:
            log.warning("cannot measure the disk: %s", exc)
            return {"free": 0, "used": 0, "total": 0, "usable": 0,
                    "reserve": int(self.cfg.reserve_bytes)}
        reserve = int(self.cfg.reserve_bytes)
        return {
            "free": usage.free,
            "used": usage.used,
            "total": usage.total,
            # What may actually be accepted, which is the number that decides
            # whether an upload starts. Never negative.
            "usable": max(0, usage.free - reserve),
            "reserve": reserve,
        }

    def revision(self) -> str:
        """A value that changes whenever the folder's contents change.

        What both screens watch, so a file sent from the phone appears on the
        Pi without anybody pressing Refresh.

        Three things rather than one, because each covers what the others miss.
        The directory's mtime notices a file replaced in place; the number of
        entries notices a create or a delete even where that mtime is coarse —
        measured: two writes inside one tick on NTFS produced an identical
        stamp, so mtime alone would have skipped a refresh; and the counter
        notices anything this process did, exactly, whatever the filesystem
        reports. Files also arrive by scp and by somebody dropping one in over
        ssh, which is why a counter alone will not do either.

        One `stat` and one `scandir` per poll, on a folder with a handful of
        files. The alternative is reading the whole listing twice a second.
        """
        try:
            stamp = self.root.stat().st_mtime_ns
            with os.scandir(self.root) as entries:
                count = sum(1 for _ in entries)
        except OSError:
            stamp, count = 0, 0
        return f"{stamp}-{count}-{self._changes}"

    def _changed(self) -> None:
        with self._lock:
            self._changes += 1

    def listing(self, sort: str = "date", ascending: bool = False) -> list[StoredFile]:
        """Every file in the directory. Directories and temporaries are not files."""
        files: list[StoredFile] = []
        try:
            entries = list(self.root.iterdir())
        except OSError as exc:
            log.warning("cannot read %s: %s", self.root, exc)
            return files

        for entry in entries:
            try:
                if not entry.is_file() or entry.is_symlink():
                    # A symlink here would resolve outside the directory as
                    # easily as inside it, and this feature promises one folder.
                    continue
                if entry.name.endswith(TEMP_SUFFIX) or entry.name.startswith("."):
                    continue
                stat = entry.stat()
            except OSError:
                continue
            guessed, _ = mimetypes.guess_type(entry.name)
            files.append(StoredFile(name=entry.name, size=stat.st_size,
                                    modified=stat.st_mtime,
                                    type=guessed or ""))

        keys = {
            "name": lambda f: f.name.lower(),
            "size": lambda f: f.size,
            "date": lambda f: f.modified,
        }
        key = keys.get(sort, keys["date"])
        # Newest first by default: the file somebody just sent is the file they
        # came to the page for.
        files.sort(key=key, reverse=not ascending if sort != "name" else not ascending)
        return files

    def resolve(self, name: str) -> Path:
        """The one path this name may mean. Raises `FileError` otherwise.

        Every route that touches a named file goes through here, including
        delete and download, and it is the only place a path is built.
        """
        safe = sanitize_filename(name)
        if not safe:
            raise FileError("that is not a filename", 400)
        root = self.root.resolve()
        candidate = (root / safe).resolve()
        # `is_relative_to` compares resolved paths, so a symlink that points
        # out of the directory has already become the place it points to.
        if candidate != root and not candidate.is_relative_to(root):
            log.warning("refused a path outside the transfer directory: %r", name)
            raise FileError("that file is not in the transfer folder", 403)
        if candidate == root:
            raise FileError("that is not a filename", 400)
        return candidate

    def open_for_download(self, name: str) -> tuple[Path, int, str]:
        """The path, its size and its type — checked, and still there."""
        path = self.resolve(name)
        try:
            stat = path.stat()
        except OSError:
            raise FileError("no such file", 404) from None
        if not path.is_file() or path.is_symlink():
            raise FileError("no such file", 404)
        guessed, _ = mimetypes.guess_type(path.name)
        return path, stat.st_size, guessed or "application/octet-stream"

    # ── writing ──────────────────────────────────────────────────────

    def unique_name(self, name: str) -> str:
        """`photo.jpg`, or `photo (1).jpg` if that is taken.

        Never overwrites, and never asks. Two people sending holiday photos
        from two phones both called `IMG_0001.jpeg` is the ordinary case, and
        silently replacing the first one is a lost file nobody knows about.
        """
        safe = sanitize_filename(name) or "file"
        candidate = self.root / safe
        if not candidate.exists():
            return safe
        stem, dot, extension = safe.rpartition(".")
        if not dot:
            stem, extension = safe, ""
        counter = 1
        while True:
            attempt = f"{stem} ({counter})" + (f".{extension}" if extension else "")
            attempt = _fit(attempt)
            if not (self.root / attempt).exists():
                return attempt
            counter += 1
            if counter > 9999:
                raise FileError("too many files with that name", 409)

    def check_room(self, expected: int) -> None:
        """Refuse before the first byte rather than after the last one."""
        if expected > self.cfg.max_upload_bytes:
            raise FileError(
                f"that file is larger than the {human_size(self.cfg.max_upload_bytes)}"
                " limit", 413)
        storage = self.storage()
        if expected and expected > storage["usable"]:
            raise FileError(
                f"not enough room: {human_size(storage['usable'])} free after the "
                f"{human_size(storage['reserve'])} reserve", 507)

    def save(self, name: str, chunks: Iterable[bytes],
             expected: int = 0) -> StoredFile:
        """Write a stream to the directory. The name is decided here.

        Returns the file as it ended up, which is not necessarily the name that
        was asked for — see `unique_name`.
        """
        if not self.ready:
            raise FileError(self.error or "file transfer is not available", 503)
        self.check_room(expected)

        final_name = self.unique_name(name)
        temporary = self.root / (TEMP_PREFIX + final_name + "." + str(os.getpid())
                                 + TEMP_SUFFIX)
        written = 0
        limit = int(self.cfg.max_upload_bytes)
        log.info("upload started: %s%s", final_name,
                 f" size={human_size(expected)}" if expected else "")
        try:
            # 0600 from the moment it exists. The mode is set on the descriptor
            # rather than after the write, so there is no instant at which the
            # file is readable by anybody else.
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "wb") as sink:
                for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > limit:
                        raise FileError(
                            f"that file is larger than the {human_size(limit)} limit",
                            413)
                    sink.write(chunk)
                sink.flush()
                # The rename below is atomic, but only the rename. Without this
                # the metadata can reach the disk before the contents do, and a
                # power cut leaves a listed file full of zeroes.
                os.fsync(sink.fileno())
        except FileError:
            _remove(temporary)
            raise
        except (ConnectionError, TimeoutError):
            # The phone went away mid-upload, which is ordinary — a tunnel, a
            # handover from Wi-Fi to cellular, a locked screen. Re-raised as
            # itself rather than dressed up as a disk error: there is nobody
            # left to answer, and the route above needs to be able to tell the
            # difference between "the network stopped" and "this device is
            # broken".
            _remove(temporary)
            log.info("upload of %s was interrupted; the partial file is gone",
                     final_name)
            raise
        except OSError as exc:
            _remove(temporary)
            if exc.errno == errno.ENOSPC:
                raise FileError("the disk filled up", 507) from None
            log.warning("upload of %s failed: %s", final_name, exc)
            raise FileError("could not write the file", 500) from None
        except Exception:
            # A dropped connection lands here, and the half-written file must
            # not survive it. Nothing partial ever gets a real name.
            _remove(temporary)
            log.info("upload of %s did not finish; the partial file is gone",
                     final_name)
            raise

        try:
            os.replace(temporary, self.root / final_name)
        except OSError as exc:
            _remove(temporary)
            log.warning("could not put %s in place: %s", final_name, exc)
            raise FileError("could not save the file", 500) from None

        log.info("upload completed: %s (%s)", final_name, human_size(written))
        self._changed()
        stat = (self.root / final_name).stat()
        guessed, _ = mimetypes.guess_type(final_name)
        return StoredFile(name=final_name, size=stat.st_size,
                          modified=stat.st_mtime, type=guessed or "")

    def delete(self, name: str) -> str:
        """Remove one file. Returns the name that went."""
        path = self.resolve(name)
        if not path.is_file():
            raise FileError("no such file", 404)
        try:
            path.unlink()
        except OSError as exc:
            log.warning("could not delete %s: %s", path.name, exc)
            raise FileError("could not delete that file", 500) from None
        self._changed()
        log.info("delete: %s", path.name)
        return path.name

    # ── how many at once ─────────────────────────────────────────────

    def begin_upload(self) -> bool:
        """Take one of the upload slots, or refuse. Paired with `end_upload`."""
        with self._lock:
            if self._uploads >= max(1, int(self.cfg.max_concurrent)):
                return False
            self._uploads += 1
            return True

    def end_upload(self) -> None:
        with self._lock:
            self._uploads = max(0, self._uploads - 1)

    def describe(self) -> dict:
        """For the settings page and the startup checks."""
        return {
            "enabled": bool(self.cfg.enabled),
            "ready": self.ready,
            "error": self.error,
            "root": str(self.root),
            "max_upload": int(self.cfg.max_upload_bytes),
            "storage": self.storage() if self.ready else None,
        }


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def human_size(count: float) -> str:
    """Bytes as something a person reads. Used in logs and on both screens."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < step or unit == "TB":
            if unit == "B":
                return f"{int(count)} B"
            return f"{count:.1f} {unit}"
        count /= step
    return f"{count:.1f} TB"
