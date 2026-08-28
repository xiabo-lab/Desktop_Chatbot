---
name: changing-settings
summary: How to change a setting on this device without breaking it
---

# Changing a setting

`set_config` takes a **section** and a **key**, never a path. It shows the
person the exact line that would change, waits for them to approve it, then
backs the file up, writes it, checks the file still loads, restarts the
assistant, and verifies it came back — putting the old value back if any of
that fails.

## Before you propose one

**The setting has to already exist.** `config.load()` is hand-written and
silently ignores keys it does not know, so inventing one produces a file that
looks changed and a device that behaves identically. `set_config` refuses a
missing key for exactly this reason; do not try to work around it by phrasing
it differently.

Read the file first if you are unsure of the spelling. Sections that come up:

| Section | What lives there |
|---|---|
| `screensaver` | `day_start`, `night_start`, `timeout_s` — when the photo slideshow and the clock take over |
| `openai` | `model`, `max_output_tokens`, the agent's own `agent_model` |
| `weather` | `cache_seconds`, the provider |
| `games` | `round_seconds` for Fruit Ninja |
| `assistant` | `llm_enabled`, `retention_hours` |

## Say what it costs before they approve

Every change to `aipi5.yaml` restarts the assistant, which blanks the
touchscreen for ten to twenty seconds and drops the phone console for the same
window. The approval card says so. Do not also say it in prose — that is asking
twice.

## When it rolls back

A rollback is the system working, not a failure to apologise for. Say what
failed, say that the old value is back, and say that the device is healthy —
then stop. Do not immediately propose the same change again.

## Two things about this file

- It carries **deliberate local differences** from the repository:
  `call.enabled` is true here and false there, and `agent.enabled` likewise.
  Never suggest "restoring it from the repo".
- Times are quoted strings for a reason. `21:01` unquoted parses as the integer
  1261 in YAML. `set_config` preserves the quoting; do not fight it.
