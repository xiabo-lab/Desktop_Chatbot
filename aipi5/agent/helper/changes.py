"""Changing a file so it can be changed back.

Every mutation goes through here, and the shape is always the same: record what
was there, write the new thing, prove the device still works, and put the old
thing back if it does not. The interesting part is not the writing.

**A change is a directory, not a diff.** `/var/lib/aipi5-agent/changes/<id>/`
holds the file as it was plus a manifest naming its mode, owner, checksum and
what was asked for. Rollback reads that rather than re-deriving anything, so it
works after a reboot, after the runtime has been restarted, and after the agent
has forgotten why it did it.

**The YAML is edited in place, by key.** Not parsed and re-serialised. The
device's `config/aipi5.yaml` is six hundred lines of which most are the reasons
for the values, and it carries deliberate local deltas — `call.enabled: true`
where the repository says false, and now `agent.enabled` beside it. Round-
tripping through `yaml.safe_load` would drop every comment and silently rewrite
every quoting decision, and the diff a person is asked to approve would be the
whole file. So one line changes, and the approval shows that line.

**Rollback is not approval-gated.** Undoing is the safe direction — the same
reason `ShutdownCountdown.cancel` is permissive where `showing` is strict.
"""

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import policy

#: How many changes to keep, and for how long. Both, because twenty changes in
#: an afternoon and one change six months ago are different kinds of clutter.
KEEP_CHANGES = 50
KEEP_DAYS = 90


class Refused(Exception):
    """Policy said no. Raised here so `ops` can pass it straight through."""


def change_id(op: str, args: dict) -> str:
    """Sortable, unique, and says what it was without opening it."""
    digest = hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{op}-{digest}"


def args_hash(op: str, args: dict) -> str:
    """The identity of a proposal, so an apply cannot drift from what was shown."""
    canonical = json.dumps({"op": op, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── reading and editing YAML by key ─────────────────────────────────


def find_setting(text: str, section: str, key: str):
    """(line index, indent, current value, trailing comment) or None.

    Deliberately line-based. See the module docstring: this file's comments are
    most of its content and a round trip through a YAML parser would throw them
    all away.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}\s*:", line):
            start = i
            break
    if start is None:
        return None

    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break                                   # the next top-level section
        found = re.match(rf"^(\s+){re.escape(key)}\s*:\s*(.*?)\s*$", line)
        if not found:
            continue
        indent, rest = found.group(1), found.group(2)
        value, comment = _split_comment(rest)
        return i, indent, value, comment
    return None


def _split_comment(rest: str) -> tuple:
    """Separate a value from its trailing comment.

    A left-to-right scan tracking quote state, because the obvious test — does
    the line have balanced quotes — answers a different question. `b: "the #
    sign"` is balanced and its `#` is not a comment, and the first version of
    this returned `"the` as the value.

    Not a YAML parser, deliberately: parsing the file is what this module
    exists to avoid, because a round trip drops every comment in it.
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


def edit_setting(text: str, section: str, key: str, value: str) -> tuple:
    """Return (new text, before, after). Raises Refused if the key is absent.

    A key that is not already in the file is refused rather than added.
    `_from_mapping` in `aipi5/core/config.py` is hand-written and ignores keys
    it does not know, so inventing one produces a file that looks changed and a
    device that behaves identically — the worst possible outcome to hand back
    to somebody who just approved it.
    """
    found = find_setting(text, section, key)
    if found is None:
        raise Refused(f"there is no {section}.{key} in this file. This tool "
                      f"changes settings that already exist; it does not add "
                      f"new ones.")
    index, indent, current, comment = found
    value = _match_quoting(current, value)
    lines = text.splitlines(keepends=True)
    ending = "\n" if lines[index].endswith("\n") else ""
    rebuilt = f"{indent}{key}: {value}"
    if comment:
        rebuilt += f"  # {comment}"
    lines[index] = rebuilt + ending
    return "".join(lines), current, value


def _match_quoting(current: str, value: str) -> str:
    """Quote the new value the way the old one was quoted.

    **This is not cosmetic.** Measured on the device with the project's own
    PyYAML: `08:00` unquoted loads as the string "08:00", but `21:01` unquoted
    loads as the *integer* 1261 — YAML 1.1 sexagesimal. So dropping the quotes
    from a time is a change that works for some values and silently destroys
    others, and leaves a file that still parses either way, so nothing
    downstream notices.

    Matching what was there is also the least surprising rule for the person
    approving the diff: the line they are shown differs from the line above it
    in exactly the way they asked for, and in no other way.
    """
    if not current or len(current) < 2:
        return value
    quote = current[0]
    if quote not in "\"'" or current[-1] != quote:
        return value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value                        # already quoted, leave it alone
    if quote in value:
        # Cannot reuse that quote character without escaping, and escaping
        # rules differ between YAML's two quoting styles. The other one is
        # always safe here because the value cannot contain both without
        # having been quoted already.
        other = "'" if quote == '"' else '"'
        if other not in value:
            return f"{other}{value}{other}"
        return value
    return f"{quote}{value}{quote}"


def diff_line(section: str, key: str, before: str, after: str) -> str:
    return (f"{section}.{key}\n"
            f"-  {key}: {before}\n"
            f"+  {key}: {after}")


# ── the change store ────────────────────────────────────────────────


def record(ident: str, path: Path, op: str, args: dict, run: str) -> Path:
    """Copy the file aside and write the manifest. Before anything is touched."""
    directory = policy.CHANGES_DIR / ident
    (directory / "before").mkdir(parents=True, exist_ok=True)
    raw = path.read_bytes()
    stat = path.stat()
    shutil.copy2(path, directory / "before" / path.name)
    manifest = {
        "id": ident, "op": op, "args": args, "run": run,
        "path": str(path), "name": path.name,
        "mode": stat.st_mode & 0o777, "uid": stat.st_uid, "gid": stat.st_gid,
        "before_sha": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw), "at": time.time(),
        "applied": False, "rolled_back": False,
    }
    _write_manifest(directory, manifest)
    _share(directory)
    return directory


def finish(ident: str, path: Path, ok: bool, detail: str = "") -> dict:
    manifest = read_manifest(ident)
    manifest["applied"] = bool(ok)
    manifest["detail"] = detail
    try:
        manifest["after_sha"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        manifest["after_sha"] = ""
    _write_manifest(policy.CHANGES_DIR / ident, manifest)
    return manifest


def read_manifest(ident: str) -> dict:
    path = policy.CHANGES_DIR / ident / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"there is no record of a change called {ident!r} ({exc})")


def restore(ident: str, force: bool = False) -> dict:
    """Put the file back. Refuses if somebody else has edited it since.

    Not an error — a refusal, so the agent says "somebody changed that file
    after I did; do you want me to overwrite their change?" rather than
    silently discarding an edit made by a person.
    """
    manifest = read_manifest(ident)
    path = Path(manifest["path"])
    kept = policy.CHANGES_DIR / ident / "before" / manifest["name"]
    if not kept.exists():
        raise Refused(f"the saved copy for {ident} is missing")

    if path.exists() and not force:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = manifest.get("after_sha") or ""
        if expected and current != expected:
            raise Refused(
                f"{path.name} has been edited since I changed it, so rolling "
                f"back would discard somebody else's work. Ask again with "
                f"force if you want it overwritten.")

    _atomic_copy(kept, path, manifest.get("mode", 0o644),
                 manifest.get("uid", 0), manifest.get("gid", 0))
    manifest["rolled_back"] = True
    manifest["rolled_back_at"] = time.time()
    _write_manifest(policy.CHANGES_DIR / ident, manifest)
    return manifest


def write(path: Path, text: str) -> None:
    """Replace a file's contents, atomically, keeping its mode and owner.

    The temporary file is made in the *same directory* so `os.replace` is a
    rename within one filesystem and therefore atomic. A reader either sees the
    old file or the new one, never a half-written one — which matters because
    the assistant re-reads this file on every restart and a torn write is a
    device that will not start.
    """
    stat = path.stat()
    temporary = path.with_name(f".{path.name}.agent-{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.st_mode & 0o777)
        _chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def listing(limit: int = 20) -> list:
    out = []
    if not policy.CHANGES_DIR.exists():
        return out
    for directory in sorted(policy.CHANGES_DIR.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        try:
            manifest = json.loads((directory / "manifest.json")
                                  .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: manifest.get(k) for k in
                    ("id", "op", "path", "at", "applied", "rolled_back",
                     "detail", "run")})
        if len(out) >= limit:
            break
    return out


def prune() -> int:
    """Oldest first, by count and by age. Run at the top of every apply."""
    if not policy.CHANGES_DIR.exists():
        return 0
    directories = sorted((d for d in policy.CHANGES_DIR.iterdir() if d.is_dir()),
                         reverse=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = 0
    for position, directory in enumerate(directories):
        try:
            age = directory.stat().st_mtime
        except OSError:
            continue
        if position >= KEEP_CHANGES or age < cutoff:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed


# ── plumbing ────────────────────────────────────────────────────────


def _chown(path, uid: int, gid: int) -> None:
    """Ownership where the platform has it.

    Absent on Windows, where the helper never runs but its tests do. Guarding
    here rather than skipping the tests keeps the rollback path exercised on
    the machine the code is written on.
    """
    if not hasattr(os, "chown"):
        return
    try:
        os.chown(path, uid, gid)
    except (OSError, AttributeError):
        pass


def _write_manifest(directory: Path, manifest: dict) -> None:
    path = directory / "manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=1, default=str),
                         encoding="utf-8")
    os.replace(temporary, path)


def _atomic_copy(source: Path, target: Path, mode: int, uid: int, gid: int) -> None:
    temporary = target.with_name(f".{target.name}.rollback-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        _chown(temporary, uid, gid)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _share(directory: Path) -> None:
    """The agent may read the record of what it asked for. Not write it."""
    try:
        import pwd
        gid = pwd.getpwnam(policy.AGENT_USER).pw_gid
    except (KeyError, ImportError):
        return
    for path in [directory, *directory.rglob("*")]:
        try:
            _chown(path, 0, gid)
            os.chmod(path, 0o750 if path.is_dir() else 0o640)
        except OSError:
            continue
