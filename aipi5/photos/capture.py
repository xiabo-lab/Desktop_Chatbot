"""Taking a picture and keeping it, which is not what the camera already did.

`Camera.capture_still()` writes a JPEG to `/dev/shm` and keeps ten of them.
That is exactly right for "what do you see": the picture is the input to a
vision request, it is shown once beside the answer, and it is gone by the
evening. It is exactly wrong for "take a picture", where the whole request is
that the picture should still be there tomorrow.

So there are two tools and they are different on purpose:

    describe_camera_image   capture, describe, let tmpfs reclaim it
    take_photo              capture, copy into the Files folder, keep it

This is the second. It is a thin adapter over two objects that already exist —
`Camera` and `FileStore` — and its whole job is the copy in between and the
naming.

**The name comes from the clock, not from the sentence.** A label the person
gave is sanitised and appended, and a label that sanitises to nothing simply is
not there; the filename is always `photo-YYYYmmdd-HHMMSS[-label].jpg`. Text
that reached the model reaching a path is the thing this file has to make
impossible, and the way to make it impossible rather than unlikely is for the
path to be built from a timestamp with a bounded suffix rather than validated
after the fact.

**The camera has one owner at a time.** A call, a game, hand control and the
screensaver handoff all borrow it, and `Camera.lend` is how. A capture that
arrives while it is lent must say who has it rather than reopening a device
another process is reading — which on this hardware does not fail cleanly, it
produces a camera that stops working until something is restarted.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: How much of a person's label survives into a filename. Long enough for
#: "kitchen" or "the cat on the sofa", short enough that the name stays
#: readable in a file list on a phone.
MAX_LABEL = 40

#: What a label may contain once it is part of a filename. Letters, numbers,
#: spaces and dashes — an allowlist rather than a list of things to strip,
#: because a strip list is a list somebody has to keep complete.
_KEEP = re.compile(r"[^0-9A-Za-z一-鿿 \-]+")


def safe_label(text: str) -> str:
    """A person's words, reduced to something that can sit in a filename.

    CJK is kept: this house speaks Mandarin and "厨房" is a perfectly good
    label for a photograph. Everything else outside the allowlist goes,
    including every separator — so there is no arrangement of input that
    produces a `/`, a `..`, a leading dot, or a name that means something to a
    shell.
    """
    cleaned = _KEEP.sub(" ", str(text or ""))
    cleaned = " ".join(cleaned.split())[:MAX_LABEL].strip(" -")
    return cleaned


class PhotoError(RuntimeError):
    """A capture that could not happen, with a sentence about why.

    Carries the reason as prose because the caller is a tool whose result is
    read out loud: "the camera is being used by a video call" is something a
    person can act on, and an OSError is not.
    """


class PhotoCapture:
    """One still, saved into the transfer folder people can already reach.

    The folder is `FileStore`'s — the same one the phone uploads to and the
    Files page lists — because a photograph that lands somewhere only this
    module knows about is a photograph nobody can get at. Section 20's transfer
    folder is where files on this device live, and a picture is a file.
    """

    def __init__(self, camera=None, files=None, clock=time.time):
        self.camera = camera
        self.files = files
        self.clock = clock

    def available(self) -> bool:
        return self.camera is not None and self.files is not None

    def take(self, label: str = "") -> dict:
        """Capture, save, and report what ended up on disk. Raises `PhotoError`.

        Returns the *stored* filename rather than the one that was asked for.
        `FileStore.save` never overwrites — two photographs taken in the same
        second become `photo-… .jpg` and `photo-… (1).jpg` — so echoing the
        requested name would be telling somebody a file exists under a name it
        does not have.
        """
        if self.camera is None:
            raise PhotoError("there is no camera on this device")
        if self.files is None or not getattr(self.files, "ready", False):
            raise PhotoError("this device has nowhere to keep a photograph "
                             "right now")

        # Asked before the capture, so the answer names the borrower. Checking
        # afterwards would mean reporting "the camera did not take a picture",
        # which is true and useless — the person wants to know that the call
        # they are on has it.
        if getattr(self.camera, "lent", False):
            who = (self.camera.describe().get("lent_to")
                   if hasattr(self.camera, "describe") else None)
            raise PhotoError(
                f"the camera is being used by {who or 'something else'} right "
                f"now, so I could not take a picture")
        if not self.camera.available():
            raise PhotoError("the camera is not available on this device right "
                             "now")

        still = self.camera.capture_still()
        if still is None:
            raise PhotoError("the camera did not take a picture")

        name = self._name(still.taken_at, label)
        try:
            data = Path(still.path).read_bytes()
        except OSError as exc:
            log.warning("the capture at %s could not be read: %s", still.path, exc)
            raise PhotoError("the picture was taken but could not be saved") from None

        try:
            stored = self.files.save(name, [data], expected=len(data))
        except Exception as exc:                     # noqa: BLE001
            log.warning("could not save %s: %s", name, exc)
            raise PhotoError(f"the picture was taken but could not be saved "
                             f"({exc})") from None

        log.info("saved %s (%dx%d)", stored.name, still.width, still.height)
        return {
            "filename": stored.name,
            "width": still.width,
            "height": still.height,
            "bytes": len(data),
            "taken_at": still.taken_at,
            "taken": time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(still.taken_at)),
            #: A token, not a path — `/api/camera/capture?t=` takes one, and it
            #: is what puts the picture in the transcript. See `_camera_capture`
            #: in `aipi5/ui/server.py`.
            "token": str(still.taken_at),
            "folder": "the transfer folder, on the Files screen",
        }

    def _name(self, taken_at: float, label: str) -> str:
        """`photo-20260902-141500.jpg`, plus a label where there is one."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(taken_at))
        tail = safe_label(label)
        return f"photo-{stamp}{'-' + tail if tail else ''}.jpg"
