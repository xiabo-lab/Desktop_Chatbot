"""What the device is, read off the device itself.

The numbers somebody would read if they were standing at it with a terminal:
board model, CPU temperature, memory, disk, load, uptime, kernel. The settings
page shows them because the questions they answer — "why did the games get
slow", "is it running out of room", "how long has it been up" — are asked in
front of the screen, and the answer was previously only reachable over ssh.

**Why this is a second reader rather than a shared one.** The root helper has
an equivalent in `aipi5/agent/helper/ops.py::system_facts`, reached through the
agent. That file runs as root, is deliberately not a package, and
`tests/test_agent_boundary.py` asserts that nothing on the voice side reaches
into it — a rule worth more than the thirty lines it costs to duplicate. None
of the reads below need any privilege at all, which is what makes the copy
honest rather than a workaround: this is not the helper's job being smuggled
across, it is a handful of world-readable files.

**Nothing here raises.** Every field is optional and simply absent when the
file behind it is missing, because this runs on a development machine that has
no `/proc/device-tree` and no thermal zone, and a settings page that throws is
worse than one that shows a dash.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Where the Pi publishes its board name, NUL-terminated because it comes
#: straight out of the device tree blob.
MODEL = Path("/proc/device-tree/model")

#: The SoC temperature. `thermal_zone0` is the CPU on a Pi; on other boards it
#: may be something else, which is why an absent or unreadable file is simply
#: a missing field rather than a guess.
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")

UPTIME = Path("/proc/uptime")
MEMINFO = Path("/proc/meminfo")
CPUINFO = Path("/proc/cpuinfo")

#: Under-voltage and thermal throttling, as a bitmask the firmware keeps.
#:
#: **`vcgencmd`, because the sysfs file does not exist here.** The obvious
#: reading — `/sys/devices/platform/soc/soc:firmware/get_throttled`, no fork —
#: was written first and then measured on the device: it is absent on this
#: Pi 5's kernel (6.18), while `vcgencmd get_throttled` answers `throttled=0x0`.
#: A preferred path that can never run is not a fallback, it is dead code, so
#: only the one that works is here.
#:
#: The fork is paid for by `THROTTLE_CACHE_S`: the settings page polls every
#: four seconds and this runs at most twice a minute.
VCGENCMD = "vcgencmd"
THROTTLE_CACHE_S = 30.0

#: Short, because it is a firmware round trip on the request path of a page.
THROTTLE_TIMEOUT_S = 2.0

#: What each bit of that mask means, most serious first. Only the "now" bits
#: are reported: the "has happened since boot" bits are true on a device that
#: was briefly unplugged three weeks ago, which is not what somebody reading a
#: settings page is asking about.
THROTTLE_BITS = (
    (0x1, "under-voltage"),
    (0x4, "CPU throttled"),
    (0x2, "frequency capped"),
    (0x8, "temperature limited"),
)


def snapshot() -> dict:
    """Everything at once, for `/api/system`. Never raises.

    Absent fields are left out rather than set to None, so the page can ask
    "is this key here" instead of every reader repeating a None check.
    """
    facts: dict = {}
    _add(facts, "model", _model)
    _add(facts, "hostname", socket.gethostname)
    _add(facts, "serial", _serial)
    _add(facts, "kernel", lambda: os.uname().release)
    _add(facts, "arch", lambda: os.uname().machine)
    _add(facts, "cpus", os.cpu_count)
    _add(facts, "load", _load)
    _add(facts, "uptime_s", _uptime)
    _add(facts, "cpu_temp_c", _temperature)
    _add(facts, "memory", _memory)
    _add(facts, "disk", _disk)
    _add(facts, "throttled", _throttled)
    return facts


def _add(facts: dict, key: str, read) -> None:
    """Run one reader, keep what it found, and never let it end the request."""
    try:
        value = read()
    except Exception:
        # Deliberately broad. This is a page of diagnostics, and a reader that
        # fails in a way nobody predicted must cost its own row rather than
        # every other row beside it.
        log.debug("hardware: could not read %s", key, exc_info=True)
        return
    if value not in (None, "", {}, ()):
        facts[key] = value


def _model() -> str:
    return MODEL.read_text(encoding="utf-8", errors="replace").strip("\x00 \n")


def _serial() -> str:
    for line in CPUINFO.read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        if key.strip() == "Serial":
            return rest.strip()
    return ""


def _load() -> list[float]:
    return [round(value, 2) for value in os.getloadavg()]


def _uptime() -> float:
    """Seconds since the *machine* started.

    Not the same as the assistant's own uptime, which `/api/system` already
    carries. "The Pi has been up nine days and the assistant four minutes" is
    exactly what somebody needs to see after a restart nobody asked for, and
    one number cannot say it — so the page shows both, labelled apart.
    """
    return round(float(UPTIME.read_text(encoding="utf-8").split()[0]), 1)


def _temperature() -> float:
    return round(int(THERMAL.read_text(encoding="utf-8").strip()) / 1000.0, 1)


def _memory() -> dict:
    """Bytes, from the kilobyte figures `/proc/meminfo` reports.

    `MemAvailable` rather than `MemFree`: free memory on Linux is mostly cache
    and reads as alarmingly small, which is the number that makes somebody
    reboot a device that was fine.
    """
    wanted = {"MemTotal": "total", "MemAvailable": "available",
              "SwapTotal": "swap_total", "SwapFree": "swap_free"}
    found = {}
    for line in MEMINFO.read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        name = wanted.get(key.strip())
        if name:
            found[name] = int(rest.split()[0]) * 1024
    return found


def _disk() -> dict:
    """The root filesystem only.

    The helper reports `/`, `/var` and `/tmp` because a maintenance run may
    care which one filled up. On this page they are the same disk and three
    rows saying so is three times the reading for no extra fact.
    """
    usage = shutil.disk_usage("/")
    return {"total": usage.total, "free": usage.free, "used": usage.used}


#: (when it was read, what it said). Module level so the cost is paid once
#: for the device rather than once per reader.
_throttle_cache: tuple[float, str] = (0.0, "")


def _throttled() -> str:
    """What the firmware is doing about power and heat, in words.

    Empty when everything is fine, which `_add` drops — so the row appears
    only when there is something to say. Under-voltage is the classic cause of
    "the games got slow" on a Pi with a HAT and an underpowered supply, and it
    is invisible from every other reading on this page.
    """
    global _throttle_cache
    read_at, remembered = _throttle_cache
    now = time.monotonic()
    if read_at and now - read_at < THROTTLE_CACHE_S:
        return remembered

    try:
        done = subprocess.run([VCGENCMD, "get_throttled"], capture_output=True,
                              text=True, timeout=THROTTLE_TIMEOUT_S)
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        # Not a Pi, or no firmware tool. Remembered as "nothing to report" so
        # the miss is not repaid on every poll.
        _throttle_cache = (now, "")
        return ""

    _, _, raw = (done.stdout or "").strip().partition("=")
    try:
        mask = int(raw, 16) if raw.startswith("0x") else int(raw)
    except ValueError:
        mask = 0
    said = ", ".join(word for bit, word in THROTTLE_BITS if mask & bit)
    _throttle_cache = (now, said)
    return said
