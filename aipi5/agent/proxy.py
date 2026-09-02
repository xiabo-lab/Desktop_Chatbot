"""The call server's entire knowledge of the agent: a socket path.

`CallServer` already does the hard part — it authenticates the phone against
`TrustedDevices` on every route. So the agent's routes are that same check
followed by a forward, and this object is the forwarding.

**Never raises.** A runtime that is not running is a 503 with a sentence, which
`api()` in `phone.html` already turns into "AIPI5 is not answering. It may be
restarting." — a message that happens to be true, because the most common reason
the agent is unreachable is that somebody asked it to restart the assistant.

This runs inside `aipi5.service`, as `fuwenxu`. It holds no agent state at all:
the transcript, the run and the mailbox all live on the other side of the
socket, so restarting the assistant loses the phone's *connection* and nothing
else.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Comfortably inside `_Handler.timeout` on the call server (POLL_TIMEOUT_S +
#: 15 = 40 s) and comfortably outside the agent's own 20 s long poll, so a poll
#: that waits its whole window trips neither layer.
DEFAULT_TIMEOUT_S = 30.0

#: How long a snapshot is reused. The phone's out-of-call poll runs every four
#: seconds and `_state` is on the same request as everything else; without this
#: the agent would be woken twice a second by a screen nobody is looking at.
SNAPSHOT_CACHE_S = 2.0


class _UnixConnection(http.client.HTTPConnection):
    """`HTTPConnection` over a Unix socket. Only `connect` differs."""

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class AgentProxy:
    def __init__(self, socket_path: Path | str,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self.socket_path = Path(socket_path)
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None

    def available(self) -> bool:
        try:
            return self.socket_path.is_socket()
        except OSError:
            return False

    def poll(self, since: int, timeout: float | None = None) -> tuple[int, dict]:
        return self._request("GET", f"/agent/v1/poll?since={int(since)}",
                             timeout=timeout)

    def say(self, message: dict) -> tuple[int, dict]:
        return self._request("POST", "/agent/v1/say", body=message)

    def open_page(self, url: str, timeout: float | None = None) -> tuple[int, dict]:
        """Put a page in the agent's browser, without starting a run.

        Its own method rather than a `say` at the call site, because this is
        the one message the *assistant* originates about a page rather than
        forwarding on the phone's behalf, and because it wants its own timeout:
        the helper navigates and then reads the page, which is seconds, and
        `DEFAULT_TIMEOUT_S` is sized for a long poll instead.
        """
        return self._request("POST", "/agent/v1/say",
                             body={"type": "agent.open", "url": url},
                             timeout=timeout)

    # ── reminders ───────────────────────────────────────────────────
    #
    # Their own methods rather than a `say` at the call site, for the reason
    # `open_page` has its own: these are messages the *assistant* originates
    # rather than forwards on the phone's behalf, and they want their own
    # timeout. `DEFAULT_TIMEOUT_S` is sized for a long poll; what happens on
    # the other side of these is a validation and a file write, so a wait
    # measured in seconds is a wait for something that has already failed.

    #: Long enough for an fsync on an SD card that is busy, short enough that
    #: a person waiting to hear "nine o'clock, on your phone" is not left
    #: standing there.
    RPC_TIMEOUT_S = 5.0

    def reminder_create(self, when: str, text: str, deliver: str = "push",
                        ) -> tuple[int, dict]:
        return self._request("POST", "/agent/v1/say",
                             body={"type": "assistant.reminder.create",
                                   "when": when, "text": text,
                                   "deliver": deliver},
                             timeout=self.RPC_TIMEOUT_S)

    def reminder_list(self, limit: int = 20) -> tuple[int, dict]:
        return self._request("POST", "/agent/v1/say",
                             body={"type": "assistant.reminder.list",
                                   "limit": int(limit)},
                             timeout=self.RPC_TIMEOUT_S)

    def reminder_cancel(self, ident: str) -> tuple[int, dict]:
        return self._request("POST", "/agent/v1/say",
                             body={"type": "assistant.reminder.cancel",
                                   "id": ident},
                             timeout=self.RPC_TIMEOUT_S)

    def snapshot(self) -> dict | None:
        """Cheap enough for the four-second idle poll. Never blocks for long."""
        now = time.monotonic()
        with self._lock:
            if self._cached and now - self._cached[0] < SNAPSHOT_CACHE_S:
                return self._cached[1]
        status, payload = self._request("GET", "/agent/v1/state", timeout=2.0)
        answer = payload if status == 200 else None
        with self._lock:
            self._cached = (now, answer)
        return answer

    # ── the one place a request is made ─────────────────────────────

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float | None = None) -> tuple[int, dict]:
        wait = self.timeout_s if timeout is None else timeout
        connection = _UnixConnection(str(self.socket_path), wait)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = ({"Content-Type": "application/json",
                        "Content-Length": str(len(payload))} if payload else {})
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                answer = json.loads(raw or b"{}")
            except ValueError:
                return 502, {"error": "the agent sent something unreadable"}
            return response.status, (answer if isinstance(answer, dict) else {})
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            log.debug("agent proxy %s %s: %s", method, path, exc)
            return 503, {"error": "the agent is not answering. It may be "
                                  "starting, or not installed."}
        finally:
            try:
                connection.close()
            except Exception:                       # noqa: BLE001
                pass
