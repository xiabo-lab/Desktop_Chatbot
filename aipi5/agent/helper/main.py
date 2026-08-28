"""The privileged helper: a root service that will do a short list of things.

    aipi5-agent (unprivileged)  --NDJSON over AF_UNIX-->  this, as root

**What this buys, precisely.** The agent cannot touch anything that is not in
`policy.py`. That is the whole claim, and it is worth having: the runtime runs as
a user with no sudo entry, no access to `/home/fuwenxu` (which is mode 700), and
no way to reach root except by asking here and being told no.

**What it does not buy.** Whether a human approved an operation is enforced in
the *runtime*, because this process has no channel to a person and cannot verify
one — any token the runtime presented, the runtime could have minted. So a bug in
the runtime is an unapproved operation from a list of allowed ones. That is
bounded here by three things this process can check alone (two-phase apply, one
mutation in flight, and a rate limit) and by tests over there. It is not
eliminated, and the README says so rather than letting this file read as a
stronger control than it is.

**Framing: one operation per connection.** The connection's lifetime is the
operation's lifetime — a hung operation is a hung connection, visible in
`ss -x`, and there is no multiplexing state machine to get wrong.

**Stdlib only, on the system interpreter.** Nothing here imports from `aipi5`,
and it does not run in the venv, so `pip install` cannot change what root runs.
"""

import json
import logging
import os
import pwd
import signal
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ops        # noqa: E402
import policy     # noqa: E402

log = logging.getLogger("aipi5-agent-helper")

SOCKET_PATH = Path("/run/aipi5-agent-helper.sock")
AUDIT_PATH = policy.LOG_DIR / "helper.jsonl"

#: Matches `MAX_BODY` on the call server. A request is one line of JSON naming an
#: operation and its arguments; nothing legitimate is close to this.
MAX_REQUEST_BYTES = 64 * 1024
#: A journal read of 500 lines is the largest thing that comes back.
MAX_RESPONSE_BYTES = 1024 * 1024
#: How long a connection may sit without sending its request line.
IDLE_TIMEOUT_S = 5.0

#: Rotate at four megabytes, keeping five. Done here rather than by logrotate:
#: one fewer package to install and one fewer thing to be misconfigured on a
#: device nobody logs in to.
MAX_LOG_BYTES = 4 * 1024 * 1024
KEEP_LOGS = 5
#: One `os.write` under PIPE_BUF is atomic, so two writers cannot interleave.
MAX_LOG_LINE = 4096


def _agent_gid():
    """The agent's group, resolved from the name in policy. Never from a request."""
    try:
        return pwd.getpwnam(policy.AGENT_USER).pw_gid
    except KeyError:
        return -1


def _share(path, gid, mode):
    """Root owns it; the agent's group may read it and never write it.

    Done here rather than in the installer because systemd's `LogsDirectory=`
    and `StateDirectory=` re-create these root:root on every start, quietly
    undoing whatever the installer set. Found by the agent being unable to read
    the record of its own requests -- so this runs after each start and is
    self-healing.
    """
    if gid < 0:
        return
    try:
        os.chown(path, 0, gid)
        os.chmod(path, mode)
    except OSError as exc:
        log.warning("could not share %s with the agent: %s", path, exc)


class Audit:
    """Append-only, root-owned, and readable by the agent but not writable.

    The record of what was done to the device is deliberately not editable by
    the thing that did it.
    """

    def __init__(self, path=AUDIT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.gid = _agent_gid()
        _share(self.path.parent, self.gid, 0o750)
        for directory in (policy.STATE_DIR, policy.CHANGES_DIR):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            _share(directory, self.gid, 0o750)
        if self.path.exists():
            _share(self.path, self.gid, 0o640)

    def write(self, event):
        event.setdefault("t", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        event.setdefault("v", 1)
        line = json.dumps(event, separators=(",", ":"), default=str)
        if len(line) > MAX_LOG_LINE:
            line = json.dumps({"t": event["t"], "v": 1, "kind": "truncated",
                               "op": event.get("op", ""),
                               "note": "event too large to record in full"},
                              separators=(",", ":"))
        try:
            self._rotate_if_needed()
            fresh = not self.path.exists()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            if fresh:
                # A file created by root belongs to root:root, and the
                # agent could not read the history of its own requests.
                _share(self.path, self.gid, 0o640)
        except OSError as exc:                      # never fail an op over this
            log.error("could not write the audit log: %s", exc)

    def _rotate_if_needed(self):
        try:
            if self.path.stat().st_size < MAX_LOG_BYTES:
                return
        except FileNotFoundError:
            return
        for n in range(KEEP_LOGS, 0, -1):
            older = self.path.with_suffix(f".jsonl.{n}")
            if n == KEEP_LOGS and older.exists():
                older.unlink()
            newer = (self.path if n == 1
                     else self.path.with_suffix(f".jsonl.{n - 1}"))
            if newer.exists():
                newer.replace(older)


def peer_of(conn):
    """(pid, uid, gid) of whoever opened this connection.

    Belt and braces with the socket's 0660 root:aipi5-agent mode. Either alone
    would do; having both means a `chmod` accident is not a hole.
    """
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                          struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def _agent_uid():
    """Resolved once at startup, from the name in policy — never from a request."""
    try:
        return pwd.getpwnam(policy.AGENT_USER).pw_uid
    except KeyError:
        log.error("there is no %s user; refusing every connection",
                  policy.AGENT_USER)
        return None


def handle(conn, audit, agent_uid):
    """One connection, one operation."""
    try:
        pid, uid, _gid = peer_of(conn)
    except OSError:
        return
    if uid != 0 and uid != agent_uid:
        log.warning("refusing a connection from uid %d pid %d", uid, pid)
        audit.write({"kind": "refuse", "op": "", "actor": {"uid": uid, "pid": pid},
                     "refused": "not the agent user"})
        return

    conn.settimeout(IDLE_TIMEOUT_S)
    line = _read_line(conn)
    if line is None:
        return

    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        _reply(conn, {"ok": False, "code": "malformed",
                      "error": f"the request was not JSON: {exc}"})
        return

    ident = str(request.get("id", ""))[:64]
    op = request.get("op")
    args = request.get("args") or {}
    run = str(request.get("run", ""))[:64]
    if not isinstance(args, dict):
        _reply(conn, {"id": ident, "ok": False, "code": "malformed",
                      "error": "args must be an object"})
        return

    handler = ops.OPS.get(op)
    if handler is None:
        # An operation that is not in the table does not exist. This is the
        # only sense in which anything is "blocked".
        audit.write({"kind": "refuse", "op": str(op)[:40], "run": run,
                     "actor": {"uid": uid, "pid": pid},
                     "refused": "no such operation"})
        _reply(conn, {"id": ident, "ok": False, "code": "unknown_op",
                      "refused": f"there is no operation called {str(op)[:40]!r}"})
        return

    started = time.monotonic()
    timeout = ops.RESTART_TIMEOUT_S if op in ops.MUTATING else ops.DEFAULT_TIMEOUT_S
    conn.settimeout(timeout + 5.0)
    try:
        result = handler(args)
    except ops.Refused as exc:
        ms = (time.monotonic() - started) * 1000.0
        audit.write({"kind": "refuse", "op": op, "run": run, "ok": False,
                     "actor": {"uid": uid, "pid": pid},
                     "refused": str(exc), "ms": round(ms)})
        _reply(conn, {"id": ident, "op": op, "ok": False, "code": "not_allowed",
                      "refused": str(exc), "ms": round(ms)})
        return
    except Exception as exc:                        # noqa: BLE001 — never leak
        ms = (time.monotonic() - started) * 1000.0
        log.exception("%s failed", op)
        audit.write({"kind": "op", "op": op, "run": run, "ok": False,
                     "actor": {"uid": uid, "pid": pid},
                     "error": str(exc), "ms": round(ms)})
        _reply(conn, {"id": ident, "op": op, "ok": False, "code": "failed",
                      "error": str(exc), "ms": round(ms)})
        return

    ms = (time.monotonic() - started) * 1000.0
    audit.write({"kind": "op", "op": op, "run": run, "ok": True,
                 "actor": {"uid": uid, "pid": pid},
                 "args": _summarise(args), "ms": round(ms)})
    _reply(conn, {"id": ident, "op": op, "ok": True, "result": result,
                  "ms": round(ms)})


def _summarise(args):
    """Arguments as recorded. Values are capped so one cannot flood the log."""
    out = {}
    for key, value in list(args.items())[:12]:
        if isinstance(value, str):
            out[str(key)[:32]] = value[:200]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[str(key)[:32]] = value
        else:
            out[str(key)[:32]] = f"<{type(value).__name__}>"
    return out


def _read_line(conn):
    chunks, size = [], 0
    while True:
        try:
            chunk = conn.recv(8192)
        except (socket.timeout, OSError):
            return None
        if not chunk:
            return None
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            _reply(conn, {"ok": False, "code": "too_large",
                          "error": "the request exceeded 64 KiB"})
            return None
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", "replace")


def _reply(conn, payload):
    payload.setdefault("v", 1)
    payload.setdefault("ok", False)
    payload.setdefault("refused", "")
    payload.setdefault("error", "")
    body = json.dumps(payload, separators=(",", ":"), default=str)
    if len(body) > MAX_RESPONSE_BYTES:
        body = json.dumps({"v": 1, "id": payload.get("id", ""), "ok": False,
                           "code": "too_large", "refused": "",
                           "error": "the result was too large to return"},
                          separators=(",", ":"))
    try:
        conn.sendall(body.encode("utf-8") + b"\n")
    except OSError:
        pass


def _listener():
    """The socket, from systemd if it handed us one, else our own.

    Taking it from systemd means the socket is created with the right owner and
    mode before this process starts, so there is no window in which it exists
    with the wrong permissions.
    """
    if os.environ.get("LISTEN_FDS") and os.environ.get("LISTEN_PID") == str(os.getpid()):
        return socket.socket(fileno=3, family=socket.AF_UNIX,
                             type=socket.SOCK_STREAM)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o660)
    sock.listen(16)
    return sock


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S")
    if os.geteuid() != 0:
        log.error("the helper must run as root")
        return 2

    agent_uid = _agent_uid()
    audit = Audit()
    sock = _listener()
    stopping = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.append(True))

    audit.write({"kind": "start", "op": "", "ops": sorted(ops.OPS),
                 "mutating": sorted(ops.MUTATING)})
    log.info("helper ready on %s; %d operations, %d of them mutating",
             SOCKET_PATH, len(ops.OPS), len(ops.MUTATING))

    sock.settimeout(1.0)
    while not stopping:
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            # The accept timeout is the helper's only heartbeat, so the idle
            # sweep hangs off it. A browser left open is a few hundred
            # megabytes on a machine with eight, and a window over the kiosk
            # that makes the device look broken to somebody walking past.
            if ops.browser.sweep():
                log.info("closed the agent browser after %.0fs idle",
                         ops.browser.IDLE_TIMEOUT_S)
                audit.write({"kind": "op", "op": "browser_close", "ok": True,
                             "detail": "idle timeout"})
            continue
        except OSError as exc:
            log.error("accept failed: %s", exc)
            continue
        try:
            handle(conn, audit, agent_uid)
        except Exception:                           # noqa: BLE001
            log.exception("a connection failed outside the operation")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # Whatever else happens, the screen goes back to the assistant.
    try:
        ops.browser.shutdown()
    except Exception:                               # noqa: BLE001
        log.exception("could not close the agent browser")
    audit.write({"kind": "stop", "op": ""})
    log.info("helper stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
