"""Talking to the privileged helper, from the unprivileged side.

One rule, and it is the same one `aipi5/llm/tools.py:198` follows: **this never
raises.** No socket, connection refused, a timeout, a truncated line, garbage
instead of JSON — every one of them comes back as an `OpResult` carrying a
sentence. A tool that throws leaves the model holding a tool call with no
result, which the API rejects on the next request; a tool that returns "the
helper is not running" leaves it able to say so.

`refused` and `error` are kept apart all the way up. *Refused* means policy said
no, and the agent should report it and stop asking. *Error* means the operation
was allowed and went wrong, which is the only one worth retrying.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Room for a 500-line journal read.
MAX_RESPONSE_BYTES = 1024 * 1024

DEFAULT_SOCKET = Path("/run/aipi5-agent-helper.sock")
DEFAULT_TIMEOUT_S = 30.0


@dataclass
class OpResult:
    """What came back. Falsey unless the operation actually did something."""

    op: str = ""
    ok: bool = False
    result: dict = field(default_factory=dict)
    refused: str = ""
    error: str = ""
    code: str = ""
    ms: float = 0.0

    def __bool__(self) -> bool:
        return self.ok

    @property
    def why(self) -> str:
        """One sentence for the model, whichever way it went wrong."""
        return self.refused or self.error or ""

    def as_dict(self) -> dict:
        out = {"ok": self.ok}
        if self.ok:
            out.update(self.result)
        else:
            out["error"] = self.why
            # The model must be able to tell "stop asking" from "try again",
            # and the two need different words rather than different prose.
            out["retryable"] = bool(self.error) and not self.refused
        return out


class HelperClient:
    """A connection per operation, because that is the helper's framing."""

    def __init__(self, socket_path: Path | str = DEFAULT_SOCKET,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self.socket_path = Path(socket_path)
        self.timeout_s = timeout_s

    def available(self) -> bool:
        """Whether the socket is there. Not whether the helper is healthy."""
        try:
            return self.socket_path.is_socket()
        except OSError:
            return False

    def call(self, op: str, args: dict | None = None, *,
             run: str = "", timeout: float | None = None) -> OpResult:
        started = time.monotonic()
        request = {"v": 1, "id": uuid.uuid4().hex[:16], "op": op,
                   "args": args or {}, "run": run}
        try:
            raw = self._round_trip(request, timeout or self.timeout_s)
        except OSError as exc:
            # Includes FileNotFoundError for a helper that is not running and
            # ConnectionRefusedError for one that is starting.
            return OpResult(op=op, error=f"the privileged helper is not "
                                         f"answering ({exc.strerror or exc})",
                            code="unavailable",
                            ms=(time.monotonic() - started) * 1000.0)
        except socket.timeout:
            return OpResult(op=op, code="timeout",
                            error=f"{op} did not finish within "
                                  f"{timeout or self.timeout_s:.0f}s",
                            ms=(time.monotonic() - started) * 1000.0)

        ms = (time.monotonic() - started) * 1000.0
        if raw is None:
            return OpResult(op=op, code="closed", ms=ms,
                            error="the helper closed the connection without "
                                  "answering")
        try:
            reply = json.loads(raw)
            if not isinstance(reply, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            log.warning("the helper sent something that was not JSON: %s", exc)
            return OpResult(op=op, code="malformed", ms=ms,
                            error="the helper's answer could not be read")

        return OpResult(
            op=op,
            ok=bool(reply.get("ok")),
            result=reply.get("result") or {},
            refused=str(reply.get("refused") or ""),
            error=str(reply.get("error") or ""),
            code=str(reply.get("code") or ""),
            ms=float(reply.get("ms") or ms),
        )

    def _round_trip(self, request: dict, timeout: float) -> str | None:
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.socket_path))
            sock.sendall(body + b"\n")
            chunks, size = [], 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    return None
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        if not chunks:
            return None
        return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", "replace")
