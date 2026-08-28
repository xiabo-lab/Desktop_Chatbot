"""The named operations. One hand-written validator each, no shell anywhere.

Every function here takes a dict that came off a socket from a process the agent
controls, and must treat it as hostile. The shape that makes that tractable:

**One validator per operation, written out.** No generic argument coercion, no
schema library, no `**args`. A parameter that is not read is not accepted, and a
parameter that is read is checked at the point it is read.

**`subprocess.run` with a fixed argv list, never a string, never `shell=True`.**
Every element of every argv below is either a literal or something `policy` has
already turned into a literal. This is the rule `aipi5/kodama/launcher.py`
already follows for the one command AIPI5 could previously run.

**Refused is not failed.** `Refused` means policy said no and the agent should
say so and stop; an exception means it was allowed and went wrong, which is the
only case worth retrying. They reach the caller as different fields.

Stage 0 is read-only: everything here either reads or reports. The write
operations arrive in stage 2 and will go through `changes.py`.
"""

import json
import os
import re
import shutil
import threading
import subprocess
import time
from pathlib import Path

import browser
import changes
import policy

#: AIPI5 and AIA write `%(asctime)s %(levelname)-7s %(name)-20s %(message)s`
#: with `%H:%M:%S`, on stderr. See the long comment in `read_journal` for why
#: that spelling has to be parsed rather than filtered by journald.
_LEVEL = re.compile(r"^\d{2}:\d{2}:\d{2}\s+(WARNING|ERROR|CRITICAL)(\s|$)")

#: journald's own numbering: 0 emerg .. 7 debug. Four is `warning`.
_WARNING = 4

#: How long any child process may take. A journal read of 500 lines is
#: milliseconds; a `systemctl restart aipi5` can legitimately take minutes, so
#: it passes its own.
DEFAULT_TIMEOUT_S = 20.0

#: `aipi5.service` declares TimeoutStartSec=180 and a cold start really does use
#: much of it — Vosk, SenseVoice, two Piper voices, the camera, the accelerator
#: and one OpenAI probe. A restart budget shorter than the unit's own is a
#: rollback that fires because the SD card was cold.
RESTART_TIMEOUT_S = 200.0


class Refused(Exception):
    """Policy said no. Not an error, and never retried."""


def _run(argv, timeout=DEFAULT_TIMEOUT_S, env=None, cwd=None,
         as_owner=False):
    """A child process, with the environment it needs and nothing else.

    `as_owner` becomes `fuwenxu` first. Running the test suite as root would
    prove something about root, and the suite reads files under that home and
    writes nothing -- so it has to run as the user the assistant runs as.
    """
    base = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C"}
    if as_owner:
        base["HOME"] = str(policy.OWNER_HOME)
        argv = ["runuser", "-u", policy.OWNER, "--", *argv]
    if env:
        base.update(env)
    started = time.monotonic()
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=base, cwd=cwd, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{argv[0]} did not finish within {timeout:.0f}s")
    except OSError as exc:
        raise RuntimeError(f"could not run {argv[0]}: {exc}")
    return done, (time.monotonic() - started) * 1000.0


def _user_env():
    uid = policy.OWNER_UID
    return {"XDG_RUNTIME_DIR": f"/run/user/{uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus"}


def _systemctl(scope, unit, verb, timeout=DEFAULT_TIMEOUT_S):
    """Drive one unit. The `--user` case is the awkward one.

    Root cannot reach another user's systemd instance by priming
    `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` — measured on this device,
    that fails with "Failed to connect to user scope bus via local transport:
    Operation not permitted". Priming works only when the *caller* is that user,
    which is why `aipi5/kodama/launcher.py` can do it and this cannot.

    Two forms do work here, both verified 2026-08-27: `--machine=<user>@.host`
    (which needs no `systemd-container` — it is not installed on this Pi and the
    flag works regardless), and `runuser` to become the user first. `runuser` is
    used because it is util-linux and therefore certain to be present, and
    because it is the same shape the rest of the project already uses.
    """
    if scope == "user":
        argv = ["runuser", "-u", policy.OWNER, "--",
                "systemctl", "--user", verb, unit]
        return _run(argv, timeout=timeout, env=_user_env())
    return _run(["systemctl", verb, unit], timeout=timeout)


# ── read-only operations ────────────────────────────────────────────

def read_journal(args):
    """Recent WARNING-and-above lines for one allowed unit.

    The priority floor is not a parameter — see `policy.MIN_PRIORITY` for why.

    **It cannot be done with `journalctl -p` alone, and finding that out was the
    point of testing this on the device.** AIPI5 logs through Python's
    `logging` to stderr, and systemd captures a service's stderr at a single
    fixed priority — `info` — for every line. So journald believes a line
    reading `WARNING google photos: ...` is priority 6, and `-p warning` filters
    out the entire assistant. Measured here: `-p warning` against
    `_SYSTEMD_USER_UNIT=aipi5.service` returns nothing at all, at any hour.

    So both are read: journald's real `PRIORITY`, which is meaningful for
    systemd's own messages about the unit and for anything not going through
    Python, and the level token Python wrote into the text. A line is kept if
    either says warning or worse.

    **Default deny.** A line that matches neither is dropped, including
    traceback continuation lines. That loses some useful detail, and it is the
    right way round: this text goes to OpenAI, and the failure of a
    keep-by-default filter is somebody's kitchen conversation leaving the
    house.

    `contains` is applied here and never handed to `journalctl -g`, which takes
    a *regex*: a model-supplied pattern against a large journal is a denial of
    service with root's CPU, for no benefit over a substring test.
    """
    unit = args.get("unit")
    found = policy.journal_unit(unit)
    if found is None:
        raise Refused(f"{unit!r} is not a journal this agent may read")
    scope, name = found

    lines = args.get("lines", 120)
    # `isinstance(True, int)` is True in Python, so a bare int check lets
    # `lines: true` through as 1. Caught by a test rather than by review.
    if (not isinstance(lines, int) or isinstance(lines, bool)
            or not 1 <= lines <= policy.MAX_JOURNAL_LINES):
        raise Refused(f"lines must be a whole number between 1 and "
                      f"{policy.MAX_JOURNAL_LINES}")

    since = args.get("since", "-6h")
    if since not in policy.SINCE:
        raise Refused(f"{since!r} is not one of {', '.join(policy.SINCE)}")

    contains = args.get("contains", "")
    if not isinstance(contains, str) or len(contains) > 80:
        raise Refused("contains must be text of at most 80 characters")

    # More than asked for, because most of what comes back is discarded by the
    # level filter below and the caller asked for N *kept* lines.
    fetch = min(lines * 12, 4000)
    argv = ["journalctl", "-n", str(fetch), "--no-pager", "-o", "json"]
    argv += ["-b"] if since == "boot" else ["--since", since]
    if scope == "user":
        # Root reads another user's unit by the raw journal fields. The
        # `--user-unit=` spelling finds nothing when run as root; this works.
        argv += [f"_SYSTEMD_USER_UNIT={name}", f"_UID={policy.OWNER_UID}"]
    elif scope == "system":
        argv += ["-u", name]

    done, ms = _run(argv, timeout=30.0)
    kept = []
    for row in (done.stdout or "").splitlines():
        try:
            entry = json.loads(row)
        except ValueError:
            continue
        message = entry.get("MESSAGE")
        if not isinstance(message, str):
            continue                       # binary or multi-part; not for us
        if not _worth_keeping(entry, message):
            continue
        if contains and contains not in message:
            continue
        kept.append(_dated(entry, message))

    kept = kept[-lines:]
    return {"unit": unit, "lines": kept, "count": len(kept),
            "priority": policy.MIN_PRIORITY, "since": since,
            "scanned": len((done.stdout or "").splitlines()),
            "ms": round(ms)}


def _worth_keeping(entry, message):
    """Warning or worse, by journald's opinion or by Python's. Default deny."""
    try:
        if int(entry.get("PRIORITY", 9)) <= _WARNING:
            return True
    except (TypeError, ValueError):
        pass
    return bool(_LEVEL.match(message))


def _dated(entry, message):
    """journald's date in front of the message, without repeating its clock.

    Python writes `%H:%M:%S` with no date, and journald knows the date but the
    message already carries the time. Printing both gives every line two
    timestamps, which is how this first came out on the device.
    """
    stamp = _stamp(entry)
    if not stamp:
        return message
    if len(message) > 9 and message[8] == " " and message[2] == message[5] == ":":
        # Drop Python's clock; the ISO stamp in front already says it.
        return f"{stamp} {message[9:]}"
    return f"{stamp} {message}"


def _stamp(entry):
    try:
        micros = int(entry.get("__REALTIME_TIMESTAMP", 0))
    except (TypeError, ValueError):
        return ""
    if not micros:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(micros / 1_000_000))


def service_status(args):
    """Whether one allowed unit is up, and what it has been doing."""
    name = args.get("service")
    found = policy.service(name)
    if found is None:
        raise Refused(f"{name!r} is not a service this agent may inspect")
    scope, _, unit = found

    fields = ("ActiveState", "SubState", "Result", "NRestarts",
              "ExecMainStatus", "MainPID", "ActiveEnterTimestamp",
              "UnitFileState", "TimeoutStartUSec")
    argv_tail = ["show", unit] + [f"-p{f}" for f in fields]
    if scope == "user":
        done, ms = _run(["runuser", "-u", policy.OWNER, "--",
                         "systemctl", "--user", *argv_tail], env=_user_env())
    else:
        done, ms = _run(["systemctl", *argv_tail])

    out = {}
    for line in (done.stdout or "").splitlines():
        key, _, value = line.partition("=")
        if key:
            out[key] = value
    return {"service": name, "unit": unit, "scope": scope,
            "active": out.get("ActiveState", "unknown"),
            "sub": out.get("SubState", "unknown"),
            "result": out.get("Result", ""),
            "restarts": _int(out.get("NRestarts")),
            "exit_status": _int(out.get("ExecMainStatus")),
            "main_pid": _int(out.get("MainPID")),
            "since": out.get("ActiveEnterTimestamp", ""),
            "enabled": out.get("UnitFileState", ""),
            "start_timeout_s": _int(out.get("TimeoutStartUSec")) // 1_000_000
                               if _usec(out.get("TimeoutStartUSec")) else None,
            "ms": round(ms)}


def read_config_file(args):
    """One allowed configuration file, capped."""
    real = policy.resolve_read(args.get("path"))
    if real is None:
        raise Refused(f"{args.get('path')!r} is not a file this agent may read")
    try:
        raw = real.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read {real}: {exc}")
    truncated = len(raw) > policy.MAX_READ_BYTES
    body = raw[:policy.MAX_READ_BYTES]
    import hashlib
    return {"path": str(real), "content": body.decode("utf-8", "replace"),
            "truncated": truncated, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mtime": real.stat().st_mtime}


def read_source(args):
    """One of this device's own source files.

    Wider than `read_config_file`, which is an enum: the agent cannot be handed
    a list of every file it might need to look at while diagnosing something.
    The path is still resolved and checked by root, and the key files are
    excluded by name rather than by being outside a tree.
    """
    real = policy.resolve_source(args.get("path"))
    if real is None:
        raise Refused("%r is not a source file this agent may read"
                      % args.get("path"))
    try:
        raw = real.read_bytes()
    except OSError as exc:
        raise RuntimeError("could not read %s: %s" % (real, exc))
    body = raw[:policy.MAX_SOURCE_BYTES]
    import hashlib
    return {"path": str(real), "content": body.decode("utf-8", "replace"),
            "truncated": len(raw) > policy.MAX_SOURCE_BYTES,
            "bytes": len(raw),
            "lines": body.count(b"\n") + 1,
            "sha256": hashlib.sha256(raw).hexdigest()}


def list_directory(args):
    """The contents of one allowed directory, capped at 200 entries."""
    real = policy.resolve_list(args.get("path"))
    if real is None:
        raise Refused(f"{args.get('path')!r} is not a directory this agent may list")
    entries = []
    try:
        for item in sorted(real.iterdir(), key=lambda p: p.name):
            try:
                stat = item.lstat()
            except OSError:
                continue
            entries.append({
                "name": item.name,
                "kind": "dir" if item.is_dir() else
                        "link" if item.is_symlink() else "file",
                "size": stat.st_size, "mtime": stat.st_mtime})
            if len(entries) >= 200:
                break
    except OSError as exc:
        raise RuntimeError(f"could not list {real}: {exc}")
    return {"path": str(real), "entries": entries, "count": len(entries),
            "truncated": len(entries) >= 200}


def system_facts(args):
    """The numbers somebody would read off the device if they were standing at it."""
    facts = {"now": time.time()}
    try:
        facts["uptime_s"] = float(Path("/proc/uptime").read_text().split()[0])
        facts["load"] = os.getloadavg()
    except (OSError, ValueError, IndexError):
        pass

    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                mem[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    facts["memory"] = mem

    disks = []
    for mount in ("/", "/var", "/tmp"):
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        disks.append({"mount": mount, "total": usage.total,
                      "free": usage.free, "used": usage.used})
    facts["disks"] = disks

    try:
        milli = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text())
        facts["cpu_temp_c"] = round(milli / 1000.0, 1)
    except (OSError, ValueError):
        pass

    try:
        done, _ = _run(["vcgencmd", "get_throttled"], timeout=5.0)
        facts["throttled"] = (done.stdout or "").strip()
    except RuntimeError:
        facts["throttled"] = ""

    facts["kernel"] = os.uname().release
    return facts


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _usec(value):
    """TimeoutStartUSec is 'infinity' as often as it is a number."""
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


# ── changing things ─────────────────────────────────────────────────
#
# Three properties this process can enforce on its own, and it enforces them
# whatever the runtime believes:
#
# **Two-phase.** An `apply` is refused unless a matching `propose` for the same
# canonical arguments was seen between three and six hundred seconds ago, from
# the same run, and has not been used. Whether a *human* said yes is enforced in
# the runtime, because this process has no channel to a person and cannot verify
# one — any token the runtime presented, the runtime could have minted. So a bug
# over there is an unapproved change from a list of allowed ones. Two-phase does
# not fix that; it makes a runaway do twice the work and leave twice the trail,
# and it makes a single malformed call incapable of changing anything.
#
# **One at a time.** Never two mutations in flight.
#
# **Twenty an hour.** Then refuse until the window rolls.

MUTATIONS_PER_HOUR = 20
PROPOSAL_MIN_AGE_S = 3.0
PROPOSAL_MAX_AGE_S = 600.0

_mutation_lock = threading.Lock()
_proposals = {}
_recent = []


def _remember_proposal(digest, run, detail):
    with _mutation_lock:
        now = time.monotonic()
        for stale in [k for k, v in _proposals.items()
                      if now - v["at"] > PROPOSAL_MAX_AGE_S]:
            del _proposals[stale]
        _proposals[digest] = {"at": now, "run": run, "detail": detail}


def _claim_proposal(digest, run):
    """Single use, and neither too fresh nor stale."""
    with _mutation_lock:
        found = _proposals.pop(digest, None)
    if found is None:
        raise Refused("I have not proposed exactly that change, or it has "
                      "already been applied. Propose it again first.")
    age = time.monotonic() - found["at"]
    if age < PROPOSAL_MIN_AGE_S:
        raise Refused("that came back faster than a person could have read it")
    if age > PROPOSAL_MAX_AGE_S:
        raise Refused("that proposal is over ten minutes old. Propose it again "
                      "so the current values are the ones shown.")
    if run and found["run"] and run != found["run"]:
        raise Refused("that proposal belongs to a different run")
    return found


def _rate_limit():
    with _mutation_lock:
        now = time.monotonic()
        _recent[:] = [t for t in _recent if now - t < 3600.0]
        if len(_recent) >= MUTATIONS_PER_HOUR:
            raise Refused("that is %d changes in an hour, which is the limit. "
                          "Nothing more until the window rolls."
                          % MUTATIONS_PER_HOUR)
        _recent.append(now)


def _config_target(args):
    """(path, section, key, value), everything validated. Or Refused."""
    section = args.get("section")
    key = args.get("key")
    value = args.get("value")
    if not isinstance(section, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}",
                                                        section or ""):
        raise Refused("a section name is required, like screensaver")
    if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,59}",
                                                    key or ""):
        raise Refused("a setting name is required, like day_start")
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        value = str(value)
    elif isinstance(value, str):
        if (len(value) > 200 or "\n" in value or "\r" in value):
            raise Refused("that value is too long, or has a line break in it")
    else:
        raise Refused("a value must be text, a number, or true/false")

    wanted = args.get("path") or str(policy.REPO / "config" / "aipi5.yaml")
    path = policy.resolve_write(wanted)
    if path is None:
        raise Refused("%r is not a file this agent may change" % wanted)
    return path, section, key, value


def propose_config(args):
    """Show exactly what would change, and remember it for a short while."""
    path, section, key, value = _config_target(args)
    text = path.read_text(encoding="utf-8")
    updated, before, after = changes.edit_setting(text, section, key, value)
    if updated == text:
        return {"no_op": True,
                "detail": "%s.%s is already %s" % (section, key, before)}

    digest = changes.args_hash("set_config", {"path": str(path),
                                              "section": section,
                                              "key": key, "value": value})
    detail = changes.diff_line(section, key, before, after)
    _remember_proposal(digest, str(args.get("run", "")), detail)
    return {"proposal": digest, "path": str(path), "section": section,
            "key": key, "before": before, "after": after, "diff": detail,
            "restarts": _restart_for(path),
            "warning": _restart_warning(_restart_for(path))}


def apply_config(args):
    """The whole sequence. Anything after the write that fails rolls it back."""
    path, section, key, value = _config_target(args)
    run = str(args.get("run", ""))
    digest = changes.args_hash("set_config", {"path": str(path),
                                              "section": section,
                                              "key": key, "value": value})
    _claim_proposal(digest, run)
    _rate_limit()
    changes.prune()

    text = path.read_text(encoding="utf-8")
    updated, before, after = changes.edit_setting(text, section, key, value)
    if updated == text:
        return {"changed": False,
                "detail": "%s.%s was already %s" % (section, key, before)}

    ident = changes.change_id("set_config", {"section": section, "key": key,
                                             "value": value})
    changes.record(ident, path, "set_config",
                   {"section": section, "key": key, "value": value}, run)
    changes.write(path, updated)

    valid, why = _validate(path)
    if not valid:
        changes.restore(ident, force=True)
        changes.finish(ident, path, False, "refused: " + why)
        return {"changed": False, "change": ident, "rolled_back": True,
                "detail": "the file would not load (%s), so I put it back" % why}

    unit = _restart_for(path)
    if not unit:
        changes.finish(ident, path, True, "no restart needed")
        return {"changed": True, "change": ident, "before": before,
                "after": after, "restarted": "", "healthy": True}

    started = time.time()
    ok, detail = _restart_and_check(unit, started)
    if ok:
        changes.finish(ident, path, True, detail)
        return {"changed": True, "change": ident, "before": before,
                "after": after, "restarted": unit, "healthy": True,
                "detail": detail}

    changes.restore(ident, force=True)
    recovered, recovery = _restart_and_check(unit, time.time())
    changes.finish(ident, path, False, "rolled back: " + detail)
    return {"changed": False, "change": ident, "rolled_back": True,
            "healthy": recovered, "detail": detail, "recovery": recovery}


# ── changing source ─────────────────────────────────────────────────
#
# A configuration change is a bounded value checked by `config.load()`. A source
# change is arbitrary code that will run as `fuwenxu`, who carries
# `NOPASSWD: ALL`. The allowlist cannot tell the difference between a good line
# of Python and a bad one, so it is not the gate here.
#
# **The gate is the test suite.** A patch is written, the file is compiled, and
# then the whole suite runs on the device. If it fails, the change is put back
# before anything is restarted. That is a real check rather than a ceremonial
# one: 1200-odd tests, including sixteen in `test_tool_safety.py` that hold the
# assistant's own security guarantee and a dozen more that hold this agent's.
#
# An edit is an **exact string replacement**, not a diff and not a whole file.
# A diff needs fuzzy context matching, which is a way to apply a change
# somewhere the author did not mean. A whole file makes the approval card
# useless -- nobody reads six hundred lines on a phone to spot the one that
# moved.

#: A source change has to compile, then pass the suite, then restart the
#: assistant and be health-checked. The suite alone is a minute.
SOURCE_TIMEOUT_S = 420.0
SUITE_TIMEOUT_S = 240.0

MAX_PATCH_BYTES = 20_000


def _patch_target(args):
    """(path, old, new), everything validated. Or Refused."""
    wanted = args.get("path")
    old = args.get("old")
    new = args.get("new")
    if not isinstance(old, str) or not old:
        raise Refused("the exact text to replace is required")
    if not isinstance(new, str):
        raise Refused("the replacement text is required")
    if len(old) > MAX_PATCH_BYTES or len(new) > MAX_PATCH_BYTES:
        raise Refused("that patch is too large to review on a phone. Make the "
                      "change in smaller pieces.")
    if old == new:
        raise Refused("those are the same")
    path = policy.resolve_write(wanted)
    if path is None:
        raise Refused("%r is not a file this agent may change" % wanted)
    return path, old, new


def _apply_patch(text, old, new):
    """Exact, and exactly once. Raises Refused otherwise."""
    found = text.count(old)
    if found == 0:
        raise Refused("that text is not in the file. Read it again — it may "
                      "have changed, or the whitespace may differ.")
    if found > 1:
        raise Refused("that text appears %d times, so I cannot tell which one "
                      "you mean. Include more of the surrounding lines."
                      % found)
    return text.replace(old, new)


def _preview(path, old, new):
    """The change, as a person would want to see it on a phone."""
    losing = old.splitlines() or [""]
    gaining = new.splitlines() or [""]
    lines = ["%s" % path.name]
    for line in losing[:12]:
        lines.append("- " + line[:110])
    if len(losing) > 12:
        lines.append("  … %d more removed" % (len(losing) - 12))
    for line in gaining[:12]:
        lines.append("+ " + line[:110])
    if len(gaining) > 12:
        lines.append("  … %d more added" % (len(gaining) - 12))
    return "\r\n".join(lines)


def propose_patch(args):
    path, old, new = _patch_target(args)
    text = path.read_text(encoding="utf-8")
    _apply_patch(text, old, new)            # raises if it does not fit
    digest = changes.args_hash("patch_file", {"path": str(path),
                                              "old": old, "new": new})
    detail = _preview(path, old, new)
    _remember_proposal(digest, str(args.get("run", "")), detail)
    return {"proposal": digest, "path": str(path), "diff": detail,
            "warning": "The whole test suite runs before this is kept. If it "
                       "fails the change is put back automatically. That takes "
                       "a couple of minutes."}


def apply_patch(args):
    path, old, new = _patch_target(args)
    run = str(args.get("run", ""))
    digest = changes.args_hash("patch_file", {"path": str(path),
                                              "old": old, "new": new})
    _claim_proposal(digest, run)
    _rate_limit()
    changes.prune()

    text = path.read_text(encoding="utf-8")
    updated = _apply_patch(text, old, new)

    ident = changes.change_id("patch_file", {"path": str(path), "old": old})
    changes.record(ident, path, "patch_file",
                   {"path": str(path), "lines": len(new.splitlines())}, run)
    changes.write(path, updated)

    valid, why = _validate(path)
    if not valid:
        changes.restore(ident, force=True)
        changes.finish(ident, path, False, "refused: " + why)
        return {"changed": False, "change": ident, "rolled_back": True,
                "detail": why}

    unit = _restart_for(path)
    if not unit:
        changes.finish(ident, path, True, "no restart needed")
        return {"changed": True, "change": ident, "restarted": "",
                "healthy": True, "detail": "the suite passed"}

    ok, detail = _restart_and_check(unit, time.time())
    if ok:
        changes.finish(ident, path, True, detail)
        return {"changed": True, "change": ident, "restarted": unit,
                "healthy": True, "detail": "the suite passed, and " + detail}

    changes.restore(ident, force=True)
    recovered, recovery = _restart_and_check(unit, time.time())
    changes.finish(ident, path, False, "rolled back: " + detail)
    return {"changed": False, "change": ident, "rolled_back": True,
            "healthy": recovered, "detail": detail, "recovery": recovery}


def _suite_passes():
    """Run the device's own tests. This is the gate on every source change.

    Run as the owner, in the checkout, with the venv's interpreter -- the same
    way a person would, because a suite that passes under different conditions
    from the ones the assistant runs in has proved something else.

    `pytest` is not installed on this device; `unittest discover` is what works
    here. One error is expected and is not a regression: `test_yoga_coach3d`
    imports pytest at module scope and fails to load. Counting failures rather
    than trusting the exit code is what lets that be tolerated without
    tolerating everything.
    """
    done, _ = _run([str(policy.REPO / ".venv" / "bin" / "python"),
                    "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                   timeout=SUITE_TIMEOUT_S,
                   env={"AIA_HOME": str(policy.OWNER_HOME / "AI_Assit"),
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONDONTWRITEBYTECODE": "1"},
                   cwd=str(policy.REPO), as_owner=True)
    output = (done.stderr or "") + (done.stdout or "")
    ran = re.search(r"^Ran (\d+) tests", output, re.M)
    if not ran:
        return False, "the test suite did not run at all"

    failures = re.findall(r"^(FAIL|ERROR): (\S+)", output, re.M)
    unexpected = [name for kind, name in failures
                  if "test_yoga_coach3d" not in name]
    if unexpected:
        return False, ("the test suite failed: " +
                       ", ".join(sorted(set(unexpected))[:4]))
    return True, "%s tests passed" % ran.group(1)


def restart_service(args):
    """Restart one allowed unit and prove it came back."""
    name = args.get("service")
    found = policy.service(name)
    if found is None:
        raise Refused("%r is not a service this agent may restart" % name)
    if name == "aipi5-agent":
        raise Refused("restarting myself would end this run mid-sentence")

    digest = changes.args_hash("restart_service", {"service": name})
    if args.get("propose"):
        _remember_proposal(digest, str(args.get("run", "")), "restart " + name)
        return {"proposal": digest, "service": name,
                "warning": _restart_warning(name)}

    _claim_proposal(digest, str(args.get("run", "")))
    _rate_limit()
    ok, detail = _restart_and_check(name, time.time())
    return {"restarted": name, "healthy": ok, "detail": detail}


def rollback_change(args):
    """Put a change back. Not approval-gated: undoing is the safe direction."""
    ident = args.get("change")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9A-Za-z._-]{1,80}",
                                                      ident or ""):
        raise Refused("a change id is required")
    manifest = changes.restore(ident, force=bool(args.get("force")))
    path = Path(manifest["path"])
    unit = _restart_for(path)
    if not unit:
        return {"rolled_back": ident, "restarted": "", "healthy": True}
    ok, detail = _restart_and_check(unit, time.time())
    return {"rolled_back": ident, "restarted": unit, "healthy": ok,
            "detail": detail}


def read_skill(args):
    """One installed skill, in full.

    Read-only by construction: there is no operation that writes one. See
    `policy.SKILLS_DIR` for why that matters now the agent can read web pages.
    """
    name = args.get("name")
    path = policy.skill(name)
    if path is None:
        known = ", ".join(n for n, _ in policy.skills()) or "none installed"
        raise Refused("there is no skill called %r. There is: %s"
                      % (name, known))
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError("could not read %s: %s" % (path, exc))
    return {"name": path.stem, "body": body[:policy.MAX_SKILL_BYTES],
            "truncated": len(body) > policy.MAX_SKILL_BYTES}


def list_skills(args):
    return {"skills": [{"name": n, "summary": s} for n, s in policy.skills()]}


def list_changes(args):
    limit = args.get("limit", 20)
    if (not isinstance(limit, int) or isinstance(limit, bool)
            or not 1 <= limit <= 50):
        raise Refused("limit must be a whole number between 1 and 50")
    return {"changes": changes.listing(limit)}


# ── health, validation and the restart map ──────────────────────────


def _restart_for(path):
    """Which unit must be restarted for a change to this file to take effect.

    Nothing in AIPI5 re-reads its configuration while running: `config.load()`
    is called once, at startup, and every section is a frozen dataclass.
    """
    if path.name == "aipi5.yaml":
        return "aipi5"
    # Source too. `WebUI.page()` reads index.html once and caches the bytes for
    # the life of the process, so a page change needs `aipi5` restarted and not
    # `aipi5-ui` -- restarting the browser reloads the same stale bytes out of
    # the assistant's memory.
    if str(path).startswith(str(policy.REPO / "aipi5")):
        return "aipi5"
    return ""


def _restart_warning(name):
    if name == "aipi5":
        return ("This also blanks the touchscreen for ten to twenty seconds, "
                "because aipi5-ui.service is PartOf=aipi5.service. The phone "
                "console goes quiet for the same window and reconnects by "
                "itself.")
    return ("Restarts " + name + ".") if name else ""


def _validate(path):
    """Does the file still load? Asked of the code that will have to load it.

    Run as the owner rather than as root: `config.load()` resolves paths against
    the checkout and reads files under that home, so a root process succeeding
    would prove nothing about whether the assistant can.
    """
    if path.suffix == ".py":
        # Compile first: a syntax error is found in milliseconds, and there is
        # no sense spending a minute of test suite discovering it.
        done, _ = _run([str(policy.REPO / ".venv" / "bin" / "python"),
                        "-m", "py_compile", str(path)],
                       timeout=60.0, cwd=str(policy.REPO), as_owner=True)
        if done.returncode != 0:
            tail = (done.stderr or "").strip().splitlines()
            return False, ("it does not compile: " +
                           (tail[-1][:160] if tail else "no reason given"))
        return _suite_passes()

    if path.suffix in (".html", ".js", ".css", ".md"):
        # Nothing to compile, but the suite covers these too -- there are
        # assertions over `index.html` and `phone.html` that catch a page which
        # no longer contains something the script addresses.
        return _suite_passes()

    if path.name != "aipi5.yaml":
        return True, ""
    script = "from aipi5.core import config; config.load(%r)" % str(path)
    done, _ = _run(["runuser", "-u", policy.OWNER, "--",
                    str(policy.REPO / ".venv" / "bin" / "python"),
                    "-c", script],
                   timeout=90.0,
                   env={"AIA_HOME": str(policy.OWNER_HOME / "AI_Assit"),
                        # Without this the interpreter starts somewhere else
                        # and `import aipi5` fails, which the rollback path
                        # then correctly reports as "the file would not load".
                        # A perfectly good change, refused for the wrong
                        # reason -- and the only sign was a ModuleNotFoundError
                        # in a sentence about YAML.
                        "PYTHONPATH": str(policy.REPO),
                        "PYTHONIOENCODING": "utf-8"})
    if done.returncode == 0:
        return True, ""
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return False, (tail[-1][:200] if tail else "it did not load")


def _restart_and_check(name, since):
    """Restart, then prove three separate things before calling it healthy.

    `is-active` alone is not enough: a unit that starts, throws, and is about to
    be restarted reads `active` for a second or two. So its own endpoint has to
    answer as well, and the log since the restart must carry no traceback.

    The window comes from the unit's own `TimeoutStartSec`, not a constant here.
    `aipi5.service` declares 180 s and a cold start really does use much of it —
    Vosk, SenseVoice, two Piper voices, the camera, the accelerator and one
    OpenAI probe. A fixed short budget rolls back perfectly good changes on a
    cold SD card, which is a failure that only ever happens to somebody else.
    """
    scope, _, unit = policy.service(name)
    budget = _start_budget(scope, unit)
    done, _ = _systemctl(scope, unit, "restart", timeout=budget)
    if done.returncode != 0:
        return False, (done.stderr or "systemctl refused it").strip()[:200]

    deadline = time.time() + budget
    last = "it never became active"
    while time.time() < deadline:
        time.sleep(2.0)
        active, _ = _systemctl(scope, unit, "is-active", timeout=15.0)
        state = (active.stdout or "").strip()
        if state in ("activating", "reloading"):
            last = "still " + state
            continue
        if state != "active":
            return False, "it is " + (state or "not running")
        healthy, why = _endpoint_ok(name)
        if not healthy:
            last = why
            continue
        bad = _traceback_since(name, since)
        if bad:
            return False, "it started but the log has " + bad
        return True, "active, answering, and nothing bad in the log"
    return False, last


def _start_budget(scope, unit):
    show, _ = _systemctl(scope, unit, "show", timeout=15.0)
    for line in (show.stdout or "").splitlines():
        if line.startswith("TimeoutStartUSec="):
            try:
                return max(45.0, int(line.split("=", 1)[1]) / 1_000_000.0)
            except (TypeError, ValueError):
                break
    return RESTART_TIMEOUT_S


#: The one thing each unit can be asked that proves it is really up, rather
#: than merely running.
ENDPOINTS = {"aipi5": "http://127.0.0.1:8092/api/state"}


def _endpoint_ok(name):
    url = ENDPOINTS.get(name)
    if not url:
        return True, ""
    done, _ = _run(["curl", "-sf", "-m", "5", "-o", "/dev/null",
                    "-w", "%{http_code}", url], timeout=12.0)
    code = (done.stdout or "").strip()
    if code == "200":
        return True, ""
    return False, "its own page answered " + (code or "nothing")


def _traceback_since(name, since):
    found = policy.journal_unit(name)
    if not found:
        return ""
    scope, unit = found
    argv = ["journalctl", "--no-pager", "-o", "cat", "-n", "400",
            "--since", "@%d" % int(since)]
    if scope == "user":
        argv += ["_SYSTEMD_USER_UNIT=" + unit, "_UID=%d" % policy.OWNER_UID]
    else:
        argv += ["-u", unit]
    done, _ = _run(argv, timeout=25.0)
    for line in (done.stdout or "").splitlines():
        if "Traceback (most recent call last)" in line:
            return "a traceback in it"
        if line.strip().startswith("CRITICAL"):
            return line.strip()[:160]
    return ""


#: The dispatch table. A name that is not a key here does not exist, which is
#: the only sense in which an operation can be "blocked".
READ_ONLY = {
    "read_journal": read_journal,
    "service_status": service_status,
    "read_config_file": read_config_file,
    "list_directory": list_directory,
    "system_facts": system_facts,
}

#: Kept separate from READ_ONLY so that "does this operation change the
#: device" is a property of which table it is in, not of reading its body.
MUTATING = {
    "propose_config": propose_config,
    "apply_config": apply_config,
    "restart_service": restart_service,
    "rollback_change": rollback_change,
    "propose_patch": propose_patch,
    "apply_patch": apply_patch,
}

#: Reading the record of what was changed is not itself a change.
READ_ONLY["list_changes"] = list_changes
READ_ONLY["read_source"] = read_source
READ_ONLY["read_skill"] = read_skill
READ_ONLY["list_skills"] = list_skills

# The browser. Opening a page changes nothing on this device, but it does put a
# window over the kiosk, so it is neither read-only nor a mutation in the sense
# the rate limiter means. It gets its own table.
#
# What makes this safe is not the list below — it is that there is **no
# listening debugging port**. `--remote-debugging-pipe` puts CDP on two file
# descriptors held by this process, so the only way to reach the browser is to
# ask here, by name, for one of these seven things.
BROWSER = dict(browser.OPS)

# One exception type across both modules. `browser.py` cannot import `ops`
# without a cycle, so the binding is made here, once, at load.
browser.Refused = Refused

OPS = {**READ_ONLY, **MUTATING, **BROWSER}
