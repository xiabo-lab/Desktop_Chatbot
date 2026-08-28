---
name: fixing-code
summary: Changing this device's own source, and the test suite that decides whether it survives
---

# Fixing something in the code

You can edit this device's own source. That is a larger thing than changing a
setting, and the way it is made safe is not that somebody approves it — it is
that **the whole test suite runs before the change is kept**. Around 1260 tests,
including the ones that hold the assistant's security guarantees. If any of them
fails that is not already failing, your change is put back and the assistant is
restarted on the old code before anything else happens.

That takes a couple of minutes. Say so when you propose one.

## Work in this order

1. **Reproduce it in evidence.** `read_journal`, `service_status`,
   `read_config_file`. A fix for a fault you have not seen is a guess with a
   test suite in front of it.
2. **Read the code.** `read_source` takes a path relative to the checkout, like
   `aipi5/tools/weather.py`. Read the whole function, not the line you think is
   wrong — this project's comments carry the reasons, and the reason is usually
   what you need.
3. **Change the smallest thing that could work.** `patch_file` replaces one
   exact piece of text. It must appear in the file exactly once, so include the
   surrounding lines rather than a bare fragment.
4. **Say what you expect to happen** before you propose it. If the suite then
   fails, you learn something; if you did not say, you learn nothing.

## What you cannot touch, and why

| | |
|---|---|
| `aipi5/agent/**` | Your own runtime. Editing the code that asks for approval, or that talks to the helper, would leave nothing to approve. |
| `scripts/**` | The installer, which is what puts root-owned code in place. |
| `systemd/**` | What starts at boot. A unit file runs before anybody is watching, and its worst failure — `enabled` and `inactive` — looks healthy to a check. |
| `config/aipi5.yaml` | Not forbidden, but use `set_config`. It is safer and the diff is one line. |

These are refused by root, not by you. If you find yourself needing one of
them, that is the answer to report, not an obstacle to route around.

## Things about this codebase that will catch you

- **`WebUI.page()` caches `index.html` for the life of the process.** A change
  to the page needs `aipi5` restarted, not `aipi5-ui` — restarting the browser
  reloads the same stale bytes out of the assistant's memory. This has cost two
  rounds of debugging a fix that was already on disk.
- **Config is read once, at startup.** Every section is a frozen dataclass.
  Nothing re-reads it.
- **`aipi5/llm/tools.py` carries a written security guarantee** and sixteen
  tests holding it. If a change of yours needs `test_tool_safety.py` edited,
  the change is wrong.
- **The comments are the documentation.** When you change a line whose comment
  explains why it is that way, change the comment too or delete it. A comment
  that now describes something else is worse than none.
- Tests live in `tests/` and you can read them. When something is unclear, the
  test that covers it usually says what it is for.

## When the suite fails

That is the system working. Report which tests failed, say the change was put
back, say the device is healthy. Then either try a smaller change or say what
you would need to know. Do not resubmit the same patch.
