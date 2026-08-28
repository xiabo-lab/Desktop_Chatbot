"""What the agent is allowed to touch. The whole of it, in one file.

**This file is installed to `/usr/local/lib/aipi5-agent/`, owned by root, by a
script that `scripts/deploy.sh` deliberately does not run.** That is the point of
it. If the allowlists lived in `config/aipi5.yaml` the agent would be able to
widen its own permissions with the same `write_config` those permissions govern,
and a deploy could change what root does without anybody deciding to.

**Stdlib only, and it runs on the system `python3`, not the venv.** `pip install`
must never be able to change what root runs.

Three rules that shape everything below:

**Names, not paths.** `restart_service` takes `"aipi5"`, a key into `SERVICES`,
and the unit name is looked up here. A caller cannot ask for a unit by spelling
it, so there is no string for it to get creative with.

**Deny by absence, not by blocklist.** `NEVER` exists as a second line of
defence, but the first is that `WRITABLE` is a short list of directories and
everything else is simply not reachable. An operation that is not implemented
cannot be talked into running.

**The agent may not touch its own jailer.** `aipi5-agent-helper` is absent from
`SERVICES` on purpose, and `/usr/local/lib/aipi5-agent` is in `NEVER`.
"""

import os
import re
from pathlib import Path

#: The account AIPI5 itself runs as. Everything the agent may read or write
#: belongs to this user or to root. Editing this is an install-time decision.
OWNER = "fuwenxu"
OWNER_UID = 1000
OWNER_HOME = Path("/home/fuwenxu")
REPO = OWNER_HOME / "AIPI5"

#: The account the runtime runs as. Nothing it may write may be owned by this
#: user — see rule 6 in `resolve_write`.
AGENT_USER = "aipi5-agent"

#: **The helper's own state directory, not the runtime's.**
#:
#: Both services used to declare `StateDirectory=aipi5-agent`, pointing at
#: the same path -- and systemd hands a StateDirectory to the user its unit
#: runs as. The runtime runs as `aipi5-agent`, so it took ownership of the
#: directory the helper keeps rollback backups in, and the agent could
#: append to a `before` copy and then ask to be rolled back onto it. That
#: writes content as root with nobody asked, which is the approval gate
#: defeated.
#:
#: Found by checking ownership on the device, not by reading the units --
#: where two identical `StateDirectory=` lines look like agreement.
STATE_DIR = Path("/var/lib/aipi5-agent-helper")
CHANGES_DIR = STATE_DIR / "changes"
LOG_DIR = Path("/var/log/aipi5-agent")

# ── services ────────────────────────────────────────────────────────
#
# name -> (scope, run_as, unit). "user" units belong to OWNER's systemd
# instance and need one of the two forms in `ops._systemctl`; "system" units are
# reachable directly.
#
# `aipi5-agent-helper` is deliberately absent. The agent does not get to
# restart, stop or reload the thing that is containing it.
SERVICES = {
    "aipi5":       ("user",   OWNER, "aipi5.service"),
    "aipi5-ui":    ("user",   OWNER, "aipi5-ui.service"),
    "kodama-lite": ("user",   OWNER, "kodama-lite.service"),
    "aia":         ("user",   OWNER, "aia.service"),
    "aipi5-agent": ("system", None,  "aipi5-agent.service"),
    "tailscaled":  ("system", None,  "tailscaled.service"),
}

#: `stop` is absent for `aipi5-agent`: an agent that can stop itself leaves a
#: task half-done and no process to report it. Restart is allowed — that at
#: least comes back.
VERBS = ("status", "restart", "start", "stop")
NO_STOP = frozenset({"aipi5-agent"})

# ── the journal ─────────────────────────────────────────────────────
#
# **Warnings and errors only, and that is not a default.** There is no parameter
# for it and no configuration switch, because the journal carries the text of
# every utterance the fast router declined and every reply the model gave — this
# device listens in somebody's kitchen, and `read_journal` sends what it returns
# to OpenAI. Utterance text logs at INFO (`aia.*` and `aipi5.llm.*`), so a hard
# floor of WARNING keeps conversation out of the API by construction rather than
# by a filter somebody can pass a different argument to.
MIN_PRIORITY = "warning"

JOURNAL_UNITS = {
    "aipi5":       ("user",   "aipi5.service"),
    "aipi5-ui":    ("user",   "aipi5-ui.service"),
    "kodama-lite": ("user",   "kodama-lite.service"),
    "aia":         ("user",   "aia.service"),
    "aipi5-agent": ("system", "aipi5-agent.service"),
    "tailscaled":  ("system", "tailscaled.service"),
    "system":      ("all",    ""),
}

MAX_JOURNAL_LINES = 500
SINCE = ("-15m", "-1h", "-6h", "-24h", "today", "yesterday", "boot")

# ── files ───────────────────────────────────────────────────────────
#
# Readable is an explicit list of files rather than a directory, because stage 1
# has no free-text path anywhere: every path-shaped tool parameter is an enum,
# so there is nothing for a model to compose.
READABLE = frozenset({
    REPO / "config" / "aipi5.yaml",
    REPO / "systemd" / "aipi5.service",
    REPO / "systemd" / "aipi5-ui.service",
    OWNER_HOME / ".config" / "systemd" / "user" / "aipi5.service",
    OWNER_HOME / ".config" / "systemd" / "user" / "aipi5-ui.service",
    Path("/etc/chromium.d/00-rpi-vars"),
})

LISTABLE = frozenset({
    REPO / "config",
    REPO / "systemd",
    REPO / ".deploy-backups",
    CHANGES_DIR,
    LOG_DIR,
    Path("/etc/chromium.d"),
})

#: Where writes may land, what they may be called, and whether the rule reaches
#: into subdirectories.
#:
#: **This grew to include source code, and that is a genuinely larger
#: permission than the rest of this file.** A configuration value is bounded and
#: is checked by `config.load()`; a line of Python is arbitrary code that will
#: run as `fuwenxu`, who carries `NOPASSWD: ALL`. So the gate on a source change
#: is not this list — it is the test suite, which has to pass on the device
#: before the change is kept. See `_validate` in `ops.py`.
#:
#: The four exclusions below are what make that gate meaningful, and they live
#: in `NEVER` rather than here so they are checked twice.
WRITABLE = (
    # (directory, name patterns, recurse into subdirectories)
    (REPO / "config",                  ("*.yaml",),                    False),
    (OWNER_HOME / ".config" / "aipi5", ("*.json", "*.yaml"),           False),
    (REPO / "aipi5",                   ("*.py", "*.html", "*.js",
                                        "*.css", "*.md"),             True),
)

#: The second line of defence, checked before *and* after symlink resolution.
#: Every entry is somewhere that grants `fuwenxu` — and `fuwenxu` is root here,
#: because that account carries `NOPASSWD: ALL`. A write into any of these is a
#: privilege escalation, not a configuration change.
NEVER = (
    OWNER_HOME / ".bashrc",
    OWNER_HOME / ".profile",
    OWNER_HOME / ".bash_profile",
    OWNER_HOME / ".ssh",
    OWNER_HOME / ".config" / "systemd",
    OWNER_HOME / ".config" / "autostart",
    OWNER_HOME / ".local" / "bin",
    # ── the four the agent may never touch, now that it can edit code ──
    #
    # Each of these would let a source change become a permanent, unreviewed
    # one, and each is excluded by name rather than by being outside a
    # directory somebody might later widen.
    #
    # **Its own runtime.** An agent that can edit `aipi5/agent/` can edit the
    # code that asks for approval, the code that talks to this helper, and the
    # tool definitions. One approved change and there is nothing left to
    # approve.
    REPO / "aipi5" / "agent",
    # **The installer**, which is what puts root-owned code in place.
    REPO / "scripts",
    # **What starts at boot.** A unit file is code that runs before anybody is
    # watching, and systemd's own failure mode here -- `enabled` and `inactive`
    # after an ordering cycle -- looks healthy to an automated check.
    REPO / "systemd",
    REPO / ".venv",
    Path("/etc/sudoers"),
    Path("/etc/sudoers.d"),
    Path("/etc/systemd"),
    Path("/etc/cron.d"),
    Path("/usr/local/lib/aipi5-agent"),
    Path("/root"),
)

# ── the browser ─────────────────────────────────────────────────────
#
# A second Chromium with its own profile, driven over a pipe rather than a
# port. **Deliberately not the kiosk's profile** at `~/.cache/aipi5-ui`: that
# one carries the camera and microphone grants for the assistant's own page,
# seeded by `scripts/aipi5-ui.sh`, and those must not be shared with whatever
# pages the agent is asked to visit.
BROWSER_PROFILE = OWNER_HOME / ".cache" / "aipi5-agent-browser"

#: Where a screenshot of a page goes: the folder the phone's Files screen
#: already reads, so a picture is something the person can simply look at.
TRANSFER_DIR = OWNER_HOME / "Downloads" / "AIPI5"


# ── skills ──────────────────────────────────────────────────────────
#
# Written-down procedure: how to diagnose a device that will not start, what to
# know before changing a setting. Knowledge this project learned the hard way,
# so the agent does not rediscover it one conversation at a time.
#
# **Installed beside this file, root-owned, and read-only to the agent.** That
# is the point. The agent can now read the open web, and a page that could get
# a sentence into a file the agent later treats as procedure would be a
# persistent instruction from a stranger. Skills are written by a person and
# installed by hand; there is no operation that creates one.
SKILLS_DIR = Path("/usr/local/lib/aipi5-agent/skills")
MAX_SKILL_BYTES = 24 * 1024

_SKILL_NAME = re.compile(r"[a-z][a-z0-9-]{1,48}")


def skill(name):
    """The path of an installed skill, or None. Name only, never a path."""
    if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
        return None
    path = SKILLS_DIR / (name + ".md")
    # Resolved and re-checked, so a symlink planted in the directory cannot
    # point somewhere else. The directory is root-owned, so this is belt and
    # braces -- but it is the same belt every other path here wears.
    real = Path(os.path.realpath(path))
    if real.parent != Path(os.path.realpath(SKILLS_DIR)):
        return None
    return real if real.is_file() else None


def skills():
    """Every installed skill, as (name, summary). Cheap enough to call often."""
    out = []
    try:
        found = sorted(SKILLS_DIR.glob("*.md"))
    except OSError:
        return out
    for path in found:
        summary = ""
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[:8]:
                if line.lower().startswith("summary:"):
                    summary = line.split(":", 1)[1].strip()
                    break
        except OSError:
            continue
        out.append((path.stem, summary))
    return out


# ── packages ────────────────────────────────────────────────────────
#
# Empty, and that is the correct starting value. `apt` runs maintainer scripts
# as root, so every name added here is a standing decision to let that package's
# postinst run as root whenever the agent asks. Approval is still required for
# each install; the list bounds what may be asked for at all.
PACKAGES = frozenset()

_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]{1,60}")

MAX_READ_BYTES = 64 * 1024


def service(name):
    """(scope, run_as, unit) for an allowed service name, else None."""
    return SERVICES.get(name)

def may_stop(name):
    return name in SERVICES and name not in NO_STOP

def journal_unit(name):
    """(scope, unit) for an allowed journal target, else None."""
    return JOURNAL_UNITS.get(name)

def package(name):
    """True only for a name that is both well-formed and on the list.

    The set check alone would do. The pattern is here so that a future list read
    from a file cannot smuggle an option through, and so the intent is written
    down next to the check.
    """
    if not isinstance(name, str) or not _PACKAGE_NAME.fullmatch(name):
        return False
    return name in PACKAGES


def _under(path, parent):
    """True when `path` is `parent` or inside it. Pure lexical comparison."""
    return path == parent or parent in path.parents


def _forbidden(path):
    return any(_under(path, never) for never in NEVER)


def resolve_read(raw):
    """The real path of an allowed readable file, or None.

    Membership is by resolved path, so a symlink whose name happens to be on the
    list does not get you the file it points at.
    """
    path = _sane(raw)
    if path is None:
        return None
    real = Path(os.path.realpath(path))
    if _forbidden(path) or _forbidden(real):
        return None
    allowed = {Path(os.path.realpath(p)) for p in READABLE}
    return real if real in allowed else None


def resolve_list(raw):
    """The real path of an allowed listable directory, or None."""
    path = _sane(raw)
    if path is None:
        return None
    real = Path(os.path.realpath(path))
    if _forbidden(path) or _forbidden(real):
        return None
    allowed = {Path(os.path.realpath(p)) for p in LISTABLE}
    return real if real in allowed else None


#: What may be *read* as source. Wider than what may be written: the agent has
#: to be able to look at its own runtime and at the installer to explain them,
#: and reading `aipi5/agent/tools.py` tells it nothing it does not already know
#: from its own tool list. Writing them is what is refused.
READABLE_TREES = (
    (REPO / "aipi5", ("*.py", "*.html", "*.js", "*.css", "*.md")),
    (REPO / "tests", ("*.py",)),
)

MAX_SOURCE_BYTES = 120 * 1024


def resolve_source(raw):
    """The real path of a readable source file, or None."""
    path = _sane(raw)
    if path is None:
        return None
    real = Path(os.path.realpath(path))
    # Secrets are not source, whatever they are called or where they sit.
    if real.name in ("openai API.txt", "openai_api_key.txt", ".openai_key"):
        return None
    for directory, globs in READABLE_TREES:
        root = Path(os.path.realpath(directory))
        if _under(real, root) and any(real.match(g) for g in globs):
            return real if real.is_file() else None
    return None


def resolve_write(raw):
    """The real path a write may land on, or None. Seven checks, in order.

    The ordering matters. `NEVER` is tested on the literal path *and* again on
    the resolved one: the first catches an obvious attempt, the second catches a
    symlink planted by an earlier operation — which is the whole reason a write
    allowlist that only checks the string it was given is not one.
    """
    path = _sane(raw)
    if path is None:
        return None
    # 2. the literal path
    if _forbidden(path):
        return None
    # 3. resolve
    real = Path(os.path.realpath(path))
    parent = Path(os.path.realpath(path.parent))
    # 4. and again, on what it actually points at
    if _forbidden(real) or _forbidden(parent):
        return None
    # 5. the parent must be somewhere we write into, and the name must fit.
    #
    #    Two shapes. A flat rule matches one directory exactly, which is what
    #    configuration wants -- `config/` and nothing under it. A recursive one
    #    matches a whole tree, which is what source needs, because
    #    `aipi5/ui/server.py` and `aipi5/games/yoga/game.py` are both source and
    #    neither is a direct child.
    #
    #    Both compare **resolved** paths. A tree check on the string would be
    #    satisfied by `aipi5/../../etc`, and `NEVER` above would then be the
    #    only thing standing in the way rather than the second of two.
    if not _permitted(real, parent):
        return None
    # 6. the parent must belong to root or to the owner — never to the agent,
    #    which would let it make a directory and then write into it
    try:
        owner = parent.stat().st_uid
    except OSError:
        return None
    if owner not in (0, OWNER_UID):
        return None
    # 7. an existing target must be a regular file, and not a symlink
    if real.is_symlink() or path.is_symlink():
        return None
    if real.exists() and not real.is_file():
        return None
    return real


def _permitted(real, parent):
    """True when a resolved path is somewhere WRITABLE allows."""
    for directory, globs, recurse in WRITABLE:
        root = Path(os.path.realpath(directory))
        if recurse:
            if not _under(real, root):
                continue
        elif parent != root:
            continue
        if any(real.match(pattern) for pattern in globs):
            return True
    return False


def _sane(raw):
    """A plausible absolute path, or None. The cheapest checks first."""
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        return None
    if "\x00" in raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else None
