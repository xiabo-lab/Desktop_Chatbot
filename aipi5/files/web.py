"""The HTTP half of file transfer, written once for two servers.

There are two of them and they could not be more different in what they trust:
`aipi5/call/server.py` faces the phone across a tailnet and authenticates every
route, while `aipi5/ui/server.py` is bound to loopback and serves the screen on
the Pi's own desk. What they need from this feature is identical, though —
receive a file without holding it in memory, send one back the same way — so
the mechanics live here and each server brings its own answer to "who is this".

Both handlers are `BaseHTTPRequestHandler`s, which is what makes one
implementation possible: these functions write through `send_response`,
`send_header` and `wfile` and nothing else.

Two things here are less obvious than they look.

**Nothing is ever buffered whole.** An upload goes stream → parser → disk in
64 KB pieces, and a download goes disk → socket the same way. The largest
allocation in a 2 GB transfer is one chunk.

**A refused upload closes the connection.** The alternative is reading two
gigabytes the caller was never allowed to send, purely to keep the socket
usable for a request that may not come. Every other body on these servers is
drained; this is the one that is not, and the connection goes with it.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

from aipi5.files.multipart import (LimitedStream, MultipartError, MultipartReader,
                                   boundary_of)
from aipi5.files.store import FileError, FileStore, human_size

log = logging.getLogger("aipi5.files")

#: How much of a file goes to the socket at a time.
SEND_CHUNK = 64 * 1024

#: `bytes=0-1023`, `bytes=1024-`, `bytes=-500`. One range only: several is
#: legal, needs multipart/byteranges to answer, and no browser asks for it
#: when downloading a file.
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def payload(store: FileStore, sort: str = "date", ascending: bool = False) -> dict:
    """The listing and the storage figures, which the page always wants together."""
    return {
        "files": [f.as_dict() for f in store.listing(sort, ascending)],
        "storage": store.storage(),
        "root": str(store.root),
        "max_upload": int(store.cfg.max_upload_bytes),
        "ready": store.ready,
        "error": store.error,
    }


def receive_upload(handler, store: FileStore, who: str = "") -> dict:
    """Take a `multipart/form-data` body and put its files in the folder.

    Returns what to answer with. Raises `FileError` for anything the person
    should be told, and lets a dropped connection propagate as itself.
    """
    if not store.ready:
        raise FileError(store.error or "file transfer is not available", 503)

    boundary = boundary_of(handler.headers.get("Content-Type", "") or "")
    if boundary is None:
        raise FileError("expected a multipart/form-data upload", 415)

    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0:
        raise FileError("that upload has no length", 411)

    # The envelope is bigger than the file — boundaries, headers, the trailing
    # delimiter — so the declared length is checked against the limit plus a
    # little slack rather than exactly. The real enforcement is per file, in
    # `FileStore.save`, where the number is the number of bytes written.
    store.check_room(max(0, length - 4096))

    if not store.begin_upload():
        raise FileError("too many uploads at once; try again in a moment", 429)

    stream = LimitedStream(handler.rfile, length)
    saved = []
    try:
        reader = MultipartReader(stream, boundary)
        for part in reader.parts():
            if not part.is_file or not part.filename:
                # An ordinary form field. Consumed and ignored: the page sends
                # nothing else today, and a field that goes unread would leave
                # the parser inside it.
                for _ in part.chunks():
                    pass
                continue
            stored = store.save(part.filename, part.chunks())
            saved.append(stored.as_dict())
    except MultipartError as exc:
        # A truncated body lands here, which is what a dropped connection looks
        # like from the parser's side. Anything already saved is complete and
        # stays; the one that was arriving has been removed by `save`.
        log.info("upload from %s did not arrive whole: %s", who or "a device", exc)
        raise FileError("the upload was interrupted", 400) from None
    finally:
        store.end_upload()

    if not saved:
        raise FileError("no file was in that upload", 400)
    return {"ok": True, "files": saved}


#: What may be shown *in* a page rather than handed over as a download. A
#: short list on purpose: the default is `attachment`, which is what stops an
#: uploaded `.html` — or an SVG, which can carry script — from being rendered
#: as a page of this application's own origin by anybody who can be persuaded
#: to open a link. Pictures, video and sound cannot do that, and being able to
#: look at a photo on the device it was sent to is the whole point of the
#: preview. Nothing else is ever inline, whatever the caller asks for.
INLINE_TYPES = ("image/", "video/", "audio/")


def may_show_inline(content_type: str) -> bool:
    kind = (content_type or "").split(";")[0].strip().lower()
    # SVG is an image that can run script. Named here rather than left to the
    # prefix, which would let it through.
    if kind in ("image/svg+xml", "image/svg"):
        return False
    return any(kind.startswith(prefix) for prefix in INLINE_TYPES)


def send_file(handler, store: FileStore, name: str, who: str = "",
              inline: bool = False) -> None:
    """Stream one file back, honouring a single `Range` if one was asked for.

    `inline` is a *request*, not an instruction — see `may_show_inline`. It is
    used by the Pi's own screen to show a photo somebody sent, which is a thing
    the phone does not need: iOS previews a download by itself.
    """
    path, size, content_type = store.open_for_download(name)
    inline = bool(inline) and may_show_inline(content_type)

    start, end = 0, size - 1
    partial = False
    asked = handler.headers.get("Range", "") or ""
    if asked and size:
        match = _RANGE.match(asked.strip())
        if match:
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            elif last:
                # `bytes=-500`: the last 500 bytes.
                start = max(0, size - int(last))
            if start >= size or start > end:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            partial = True

    count = end - start + 1 if size else 0
    handler.send_response(206 if partial else 200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(count))
    # Both spellings. The plain one is a fallback for anything that cannot read
    # the second; `filename*` is what carries 测试照片.jpg intact, and without
    # it Safari saves the file under a name of mojibake.
    handler.send_header(
        "Content-Disposition",
        "%s; filename=\"%s\"; filename*=UTF-8''%s"
        % ("inline" if inline else "attachment",
           _ascii_fallback(path.name), quote(path.name, safe="")))
    handler.send_header("Accept-Ranges", "bytes")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    # A file somebody uploaded is not part of this application and must never
    # be treated as though it were: no scripts, no frames, nothing inline.
    handler.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    handler.end_headers()

    log.info("download started: %s (%s%s)", path.name, human_size(count),
             " of a range" if partial else "")
    sent = 0
    try:
        with open(path, "rb") as source:
            if start:
                source.seek(start)
            while sent < count:
                chunk = source.read(min(SEND_CHUNK, count - sent))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                sent += len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        # Somebody cancelled the download, or the train went into a tunnel.
        # Nothing to clean up — this direction writes nothing to disk.
        log.info("download of %s stopped early at %s", path.name, human_size(sent))
        handler.close_connection = True
        return
    except OSError as exc:
        log.warning("could not read %s: %s", path.name, exc)
        handler.close_connection = True
        return
    log.info("download completed: %s", path.name)


def _ascii_fallback(name: str) -> str:
    """A filename old software can put in quotes without choking.

    Quotes and backslashes would end the parameter early — a filename is
    attacker-supplied text going into a header — and anything non-ASCII is
    replaced rather than mangled, because `filename*` beside it carries the
    real one.
    """
    cleaned = name.encode("ascii", "replace").decode("ascii")
    cleaned = cleaned.replace("\\", "_").replace('"', "_")
    cleaned = "".join(c for c in cleaned if 0x20 <= ord(c) < 0x7f)
    return cleaned or "download"


def refuse_upload(handler, code: int, message: str) -> None:
    """Answer an upload that is not going to happen, and hang up.

    The body is deliberately not drained. Everything else on these servers
    reads what it refuses — an unread body is the next request's problem — but
    that reasoning assumes a body measured in kilobytes. Reading a gigabyte
    that was never permitted, to keep a connection somebody may not use again,
    is the wrong trade; the connection is closed instead, which cannot
    desynchronise anything.
    """
    import json
    body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass
    handler.close_connection = True


def name_from_path(path: str, prefix: str) -> str:
    """The filename in a URL like `/api/files/download/holiday%20photo.jpg`.

    Decoded here, before it reaches the store, so that `%2e%2e%2f` is `../` by
    the time anything looks at it — a check that runs before decoding is a
    check that can be spelled around.
    """
    from urllib.parse import unquote
    tail = path[len(prefix):] if path.startswith(prefix) else ""
    return unquote(tail)
