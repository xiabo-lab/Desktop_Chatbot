"""Change one setting in the deployed YAML, and leave the rest of it alone.

The device's `config/aipi5.yaml` is nine hundred lines of which most are
comments explaining why each number is what it is. That file is the reason
this module is line-based and not a YAML round trip: `yaml.safe_load` followed
by `yaml.safe_dump` produces a file with the same values and none of the
reasoning, which is a worse file than the one it replaced.

**The device edits its own configuration from two places now**, and this is
the one both of them call. `VolumeControl` has written `audio.volume` since
the settings page grew a slider; the screensaver schedule is the second. One
implementation rather than two, because the rule below about quoting is the
kind of thing that gets fixed once and reintroduced by the copy.

`aipi5/agent/helper/ops.py` reaches an equivalent of this through the root
helper's own `changes.edit_setting`, and the two are deliberately separate
files. The helper is root's code, is deliberately not a package, and
`tests/test_agent_boundary.py` asserts that nothing on the voice side reaches
into it. Twenty similar lines is the cheaper mistake — the same trade the two
tool schemas already make.

**What a deploy does to a file written here: nothing.** `scripts/deploy.sh`
lists `config/aipi5.yaml` as protected and never overwrites the device's copy,
so a value set from the touchscreen survives every future deploy. That is what
makes writing here honest rather than a change that quietly disappears.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)


class SettingMissing(OSError):
    """The key is not already in the file.

    An `OSError` because every caller here is already handling one — a
    configuration write fails the way a file write fails, and a caller that
    rolls back on `OSError` should roll back on this too without being taught
    a second exception.
    """


def find_setting(text: str, section: str, key: str):
    """(line index, indent, current value, trailing comment), or None.

    Scoped to one top-level section, so `screensaver.enabled` cannot match the
    `enabled` under `games`. The scan stops at the next line that starts in
    column zero and is not a comment, which is what ends a section in this
    file.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}\s*:", line):
            start = index
            break
    if start is None:
        return None

    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break                            # the next top-level section
        found = re.match(rf"^(\s+){re.escape(key)}\s*:\s*(.*?)\s*$", line)
        if not found:
            continue
        value, comment = split_comment(found.group(2))
        return index, found.group(1), value, comment
    return None


def split_comment(rest: str) -> tuple[str, str]:
    """Separate a value from its trailing comment.

    A left-to-right scan tracking quote state, because the obvious test — are
    the quotes balanced — answers a different question. `b: "the # sign"` is
    balanced and its `#` is not a comment.
    """
    quote = ""
    for position, character in enumerate(rest):
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == "#":
            return rest[:position].strip(), rest[position + 1:].strip()
    return rest.strip(), ""


def match_quoting(current: str, value: str) -> str:
    """Quote the new value the way the old one was quoted.

    **This is not cosmetic, and the screensaver schedule is why it is here.**
    Measured with this project's own PyYAML: `08:00` unquoted loads as the
    string "08:00", but `21:01` unquoted loads as the *integer* 1261 — YAML 1.1
    reads a colon-separated number as sexagesimal. So writing a time without
    its quotes works for some values and silently destroys others, and leaves a
    file that still parses either way, so nothing downstream notices until the
    night screen never arrives.

    Matching what was there is also the least surprising rule for anybody
    reading the diff afterwards: the line differs in the way they asked for and
    in no other way.
    """
    if not current or len(current) < 2:
        return value
    quote = current[0]
    if quote not in "\"'" or current[-1] != quote:
        return value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value                         # already quoted, leave it alone
    if quote in value:
        # Cannot reuse that quote character without escaping, and the escaping
        # rules differ between YAML's two quoting styles. The other one is safe
        # here: a value containing both would have arrived quoted already.
        other = "'" if quote == '"' else '"'
        return f"{other}{value}{other}" if other not in value else value
    return f"{quote}{value}{quote}"


def set_scalar(text: str, section: str, key: str, value) -> tuple[str, str, str]:
    """Return (new text, previous value, written value).

    Raises `SettingMissing` when the key is not already there. A key that is
    absent is refused rather than added, because `_from_mapping` in
    `aipi5/core/config.py` is hand-written and ignores keys it does not know —
    inventing one produces a file that looks changed and a device that behaves
    exactly as it did.
    """
    found = find_setting(text, section, key)
    if found is None:
        raise SettingMissing(f"there is no {section}.{key} setting to change")

    index, indent, current, comment = found
    written = match_quoting(current, str(value))
    lines = text.splitlines(keepends=True)
    ending = "\n" if lines[index].endswith("\n") else ""
    rebuilt = f"{indent}{key}: {written}"
    if comment:
        rebuilt += f"  # {comment}"
    lines[index] = rebuilt + ending
    return "".join(lines), current, written


def write_scalar(path: Path, section: str, key: str, value) -> str:
    """Set one key in the file at `path`. Returns the value it replaced.

    Atomic, and keeps the file's mode: a half-written configuration is a device
    that will not start, and this runs while the assistant is up.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    updated, previous, written = set_scalar(text, section, key, value)
    if updated != text:
        _replace(path, updated)
        log.info("config: %s.%s %s -> %s", section, key, previous or "unset",
                 written)
    return previous


def _replace(path: Path, text: str) -> None:
    stat = path.stat()
    temporary = path.with_name(f".{path.name}.edit-{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
