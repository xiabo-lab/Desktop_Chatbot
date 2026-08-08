"""Find the AIA checkout and put it on the import path.

Every subsystem AIA already has — the wake word, capture, VAD, SenseVoice,
Piper, the fast router, the Kodama-Lite and system plugins — is imported from
there rather than reimplemented here. This module is the whole of the mechanism
that makes that possible, and it exists as its own file so there is exactly one
answer to "where does AIA live", instead of a `sys.path` insert at the top of
whichever module happened to need it first.

**Why a path insert rather than a copy or a git submodule.**

A copy is the option that looks tidiest and is worst. The measurements AIA's
config carries — mixer gain 8/30 because 12 dB of headroom is what the
endpointer needs, `min_speech_ms` at 500 because misses held 300-660 ms of
voiced audio, `save_lyrics` at min_score 0.90 because "Se lyrics." scores 0.889
— are load-bearing numbers derived from real captures on this device. Copied,
they immediately begin to drift from the file they were measured into, and the
way that drift shows up is an assistant that mishears one command in four with
no diff to point at.

A submodule would pin a commit, which is worse for the opposite reason: AIA is
under active development on the same Pi, and a fix to the wake word should
reach this assistant when it is made, not when someone remembers to bump a
pointer.

So: AIA is a peer checkout, found at import time, and its version is reported
on the settings page so the pairing is at least visible.

Order of search, most explicit first:

    AIA_HOME=/path/to/AI_Assit     an override, for a checkout somewhere else
    ~/AI_Assit                     where the AIA README and its systemd units
                                   both hardcode it
    ../AI_Assit                    beside this checkout, which is how a
                                   development machine usually has it
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# This file is aipi5/core/aia_bridge.py, so the project root is two up.
ROOT = Path(__file__).resolve().parents[2]


class AiaMissing(RuntimeError):
    """AIA could not be found, so there is no assistant to build on.

    Fatal, and fatal early. Everything that makes this thing a voice assistant
    rather than a web page comes from AIA, so continuing without it would mean
    starting up, reporting healthy, and then failing on the first sound anybody
    made.
    """


def _candidates() -> list[Path]:
    override = os.environ.get("AIA_HOME")
    found = [Path(override).expanduser()] if override else []
    found.append(Path.home() / "AI_Assit")
    found.append(ROOT.parent / "AI_Assit")
    return found


def _is_checkout(path: Path) -> bool:
    """Does this directory hold the AIA package rather than merely exist?

    Checked against a file that would have to be present in any usable
    checkout, not against the directory name. A `~/AI_Assit` that is an empty
    directory left behind by a failed deploy is the case this catches, and its
    symptom without this check is `ImportError: No module named aia.audio`
    twenty lines later, from a module that has no idea why.
    """
    return (path / "aia" / "__init__.py").is_file()


def locate() -> Path:
    """The AIA checkout this assistant is built on."""
    tried = []
    for candidate in _candidates():
        tried.append(str(candidate))
        if _is_checkout(candidate):
            return candidate
    raise AiaMissing(
        "cannot find the AIA checkout this assistant is built on. Looked in: "
        + ", ".join(tried)
        + ". Clone it (https://github.com/xiabo-lab/AIA) to ~/AI_Assit, or set "
        "AIA_HOME to where it already is."
    )


_installed: Path | None = None


def install() -> Path:
    """Put AIA on `sys.path`. Idempotent; returns where it was found.

    Appended rather than prepended. AIPI5 has modules whose names deliberately
    echo AIA's — `core.config`, `ui.server` — and while they are inside
    different packages today, a path entry that wins over this project's own
    is the kind of thing that only shows up as a wrong module being imported
    long after the change that caused it.
    """
    global _installed
    if _installed is not None:
        return _installed

    home = locate()
    if str(home) not in sys.path:
        sys.path.append(str(home))
    _installed = home
    log.info("using AIA from %s", home)
    return home


def version() -> str:
    """AIA's version, for the settings page.

    AIA stamps this in with `git archive` at deploy time and has no constant to
    read on a plain checkout, so "unknown" is a normal answer on a development
    machine and not a fault.
    """
    install()
    try:
        from aia._version import __version__  # type: ignore[import-not-found]

        return str(__version__)
    except Exception:
        return "unknown"


# Imported for the side effect, by every module in this package that needs
# anything from `aia`. Doing it here rather than at each call site means an
# `import aia.something` at the top of a module always works.
install()
