"""Reading `multipart/form-data` a chunk at a time.

Written rather than imported, for two reasons.

**There is nothing to import.** `cgi.FieldStorage` was the stdlib answer and it
was removed in Python 3.13, which is what the Pi runs. `email.parser` is still
here but it takes the whole message as bytes, and the whole message is the file
— a 2 GB upload would be a 2 GB allocation on a machine with 8 GB and a vision
model in it.

**A file must never be in memory.** Everything here yields chunks and the
caller writes them to disk as they arrive, so an upload of any size costs one
buffer. That is the property this module exists for; if it is ever traded away
for a simpler parser, the limit on upload size becomes the size of RAM.

The one subtle part is that a boundary can straddle a read. `--boundary` may
have its first three bytes at the end of one 64 KB chunk and the rest at the
start of the next, and a parser that searches each chunk in isolation writes
half a delimiter into the file and then fails to find the end of the part. So
the tail of the buffer is held back — never less than the delimiter length —
until enough has arrived to say for certain that it is content. There is a test
for exactly that, with the boundary split at every offset.
"""

from __future__ import annotations

from typing import Iterator

#: Read size. Large enough that a big upload is not a syscall per kilobyte,
#: small enough to be nothing on this device.
CHUNK = 64 * 1024

#: A part's headers are `Content-Disposition` and perhaps `Content-Type`. This
#: is the cap on how much will be read looking for the blank line that ends
#: them, so a body that never contains one cannot grow the buffer forever.
MAX_PART_HEADERS = 16 * 1024


class MultipartError(Exception):
    """The body is not a well-formed multipart document."""


def boundary_of(content_type: str) -> bytes | None:
    """The boundary in a `Content-Type`, or None if this is not multipart."""
    if not content_type:
        return None
    parts = [p.strip() for p in content_type.split(";")]
    if not parts or not parts[0].lower().startswith("multipart/form-data"):
        return None
    for parameter in parts[1:]:
        if parameter.lower().startswith("boundary="):
            value = parameter[len("boundary="):].strip()
            if value.startswith('"') and value.endswith('"') and len(value) > 1:
                value = value[1:-1]
            return value.encode("latin-1") if value else None
    return None


class LimitedStream:
    """`Content-Length` bytes of a socket and not one more.

    Reading past the body of a request blocks until the peer sends another one,
    which on a kept-alive connection is a hung thread rather than an error. So
    the parser is given a view that ends exactly where the body does.
    """

    def __init__(self, stream, length: int):
        self._stream = stream
        self.remaining = max(0, int(length))

    def read(self, size: int) -> bytes:
        if self.remaining <= 0:
            return b""
        chunk = self._stream.read(min(size, self.remaining))
        self.remaining -= len(chunk)
        return chunk

    def drain(self) -> None:
        """Swallow whatever is left, so the connection stays usable.

        The same rule as every other POST on these servers: a body that is not
        read is a body the *next* request gets parsed from.
        """
        while self.remaining > 0:
            if not self.read(CHUNK):
                break


class Part:
    """One part of the body: its headers, and its content as chunks."""

    def __init__(self, reader: "MultipartReader", headers: dict[str, str]):
        self._reader = reader
        self.headers = headers
        disposition = headers.get("content-disposition", "")
        self.name = _parameter(disposition, "name") or ""
        self.filename = _parameter(disposition, "filename")
        self.content_type = headers.get("content-type", "") or ""

    @property
    def is_file(self) -> bool:
        # A `filename` attribute is what distinguishes a file from an ordinary
        # form field, even when it is empty — which is what a browser sends for
        # a file input nobody chose a file with.
        return self.filename is not None

    def chunks(self) -> Iterator[bytes]:
        """The content, in pieces. Must be consumed before the next part."""
        return self._reader._part_chunks()


def _parameter(header: str, name: str) -> str | None:
    """One parameter out of a header value, quoted or not.

    Deliberately small. RFC 2231's encoded parameters are not produced by any
    browser for this field — they send UTF-8 in quotes and have for years — and
    a partial implementation of an encoding nobody sends is more likely to
    mangle a Chinese filename than to rescue one.
    """
    for parameter in header.split(";")[1:]:
        parameter = parameter.strip()
        if not parameter.lower().startswith(name.lower() + "="):
            continue
        value = parameter[len(name) + 1:].strip()
        if value.startswith('"'):
            # Ends at the closing quote, which may be followed by nothing.
            value = value[1:]
            closing = value.rfind('"')
            value = value[:closing] if closing >= 0 else value
        return value
    return None


class MultipartReader:
    """Streams the parts of a `multipart/form-data` body.

    Usage is strictly forward: take a part, consume its chunks, take the next.
    A part that is abandoned half way is drained before the next one starts, so
    the caller cannot leave the stream pointing into the middle of a file.
    """

    def __init__(self, stream, boundary: bytes, chunk: int = CHUNK):
        if not boundary:
            raise MultipartError("no boundary")
        self._stream = stream
        self._chunk = chunk
        #: What separates parts, including the CRLF that precedes it. That CRLF
        #: belongs to the delimiter and not to the file — a copy that keeps it
        #: is two bytes longer than the original, every time.
        self._delimiter = b"\r\n--" + boundary
        self._buffer = b""
        self._eof = False
        self._finished = False
        self._in_part = False
        self._started = False

    # ── the buffer ───────────────────────────────────────────────────

    def _fill(self) -> bool:
        """One more read into the buffer. False at the end of the stream."""
        if self._eof:
            return False
        data = self._stream.read(self._chunk)
        if not data:
            self._eof = True
            return False
        self._buffer += data
        return True

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        """Everything up to `marker`, consuming it. Raises past `limit`."""
        while True:
            found = self._buffer.find(marker)
            if found >= 0:
                head, self._buffer = self._buffer[:found], self._buffer[found + len(marker):]
                return head
            if len(self._buffer) > limit:
                raise MultipartError("a part header is implausibly long")
            if not self._fill():
                raise MultipartError("the body ended inside a header")

    # ── parts ────────────────────────────────────────────────────────

    def parts(self) -> Iterator[Part]:
        """Each part in turn."""
        while True:
            part = self._next_part()
            if part is None:
                return
            yield part

    def _next_part(self) -> Part | None:
        if self._finished:
            return None
        if self._in_part:
            # Whatever the caller did not read still has to come off the
            # stream, or the next part starts in the middle of a file.
            for _ in self._part_chunks():
                pass
        if not self._started:
            # The preamble, and the first delimiter — which is the only one
            # with no CRLF in front of it, so it is matched without one.
            self._skip_preamble()
            self._started = True
        if self._finished:
            return None

        headers: dict[str, str] = {}
        raw = self._read_until(b"\r\n\r\n", MAX_PART_HEADERS)
        for line in raw.split(b"\r\n"):
            if not line:
                continue
            name, _, value = line.partition(b":")
            if not _:
                raise MultipartError("a part header has no colon")
            headers[name.decode("latin-1").strip().lower()] = \
                value.decode("utf-8", "replace").strip()
        self._in_part = True
        return Part(self, headers)

    def _skip_preamble(self) -> None:
        opening = self._delimiter[2:]           # without the leading CRLF
        while True:
            found = self._buffer.find(opening)
            if found >= 0:
                self._buffer = self._buffer[found + len(opening):]
                self._after_delimiter()
                return
            # Keep enough to catch a delimiter split across two reads.
            keep = len(opening)
            if len(self._buffer) > keep:
                self._buffer = self._buffer[-keep:]
            if not self._fill():
                raise MultipartError("no opening boundary")

    def _after_delimiter(self) -> None:
        """Read the two bytes that say whether another part follows."""
        while len(self._buffer) < 2:
            if not self._fill():
                # A final delimiter with nothing after it. Tolerated: the part
                # before it was complete, which is what matters.
                self._finished = True
                return
        marker, self._buffer = self._buffer[:2], self._buffer[2:]
        if marker == b"--":
            self._finished = True
        elif marker != b"\r\n":
            # Transport padding is allowed between the boundary and the CRLF.
            # Rare, and cheap to accept.
            newline = self._buffer.find(b"\r\n")
            if marker[0:1] not in (b" ", b"\t") or newline < 0:
                raise MultipartError("a boundary is followed by rubbish")
            self._buffer = self._buffer[newline + 2:]

    def _part_chunks(self) -> Iterator[bytes]:
        """The content of the current part, ending at the next delimiter."""
        if not self._in_part:
            return
        delimiter = self._delimiter
        while True:
            found = self._buffer.find(delimiter)
            if found >= 0:
                content = self._buffer[:found]
                self._buffer = self._buffer[found + len(delimiter):]
                self._in_part = False
                if content:
                    yield content
                self._after_delimiter()
                return
            # Everything except a possible partial delimiter at the tail is
            # certainly content. The `- 1` is what makes a boundary split
            # across two reads impossible to miss.
            safe = len(self._buffer) - (len(delimiter) - 1)
            if safe > 0:
                content, self._buffer = self._buffer[:safe], self._buffer[safe:]
                yield content
            if not self._fill():
                self._in_part = False
                self._finished = True
                raise MultipartError("the body ended inside a file")
