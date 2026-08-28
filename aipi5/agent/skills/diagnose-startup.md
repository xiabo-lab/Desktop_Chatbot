---
name: diagnose-startup
summary: The assistant is not running, or came back and should not have
---

# When AIPI5 will not start

Work through these in order. The first two cover most of it.

## 1. Is it actually failing, or was it never started?

`service_status("aipi5")` gives you `active`, `restarts` and `exit_status`.

- **`inactive (dead)` with `enabled`, and `restarts: 0`** — this is the
  dangerous one. It did not crash; systemd *deleted its start job*. That
  happens when an ordering cycle forms between `aipi5`, `kodama-lite` and
  `default.target`. Nothing failed, nothing retried, and `is-enabled` says
  everything is fine. Look for a recently added `After=` or `Requires=` in a
  unit file.
- **`activating` for a long time** — a cold start really does take a while:
  Vosk, SenseVoice, two Piper voices, the camera, the accelerator, and one
  OpenAI probe. `TimeoutStartSec` is 180 seconds and much of it gets used on a
  cold SD card. Wait before concluding anything.
- **restarts climbing** — it is crash-looping. Go to step 2.

## 2. What does it say on the way down?

`read_journal("aipi5", since="-15m")`.

Remember you only see WARNING and above. Something that fails *silently* will
show nothing here, and that is not the same as nothing having happened.

Known causes, each of which reads as something else:

| What you see | What it is |
|---|---|
| microphone or speech recognition unavailable | These two are **fatal** by design. Everything else is degraded-but-running. |
| PipeWire holding the microphone after a reboot | The older `aia.service` and this one both want it. They are `Conflicts=`, so check `aia` is not running. |
| nothing at all after a reboot | The journal here is **volatile**. It does not survive a reboot, so the evidence is gone rather than absent. |

## 3. Did somebody change the configuration?

`list_changes()` shows what this agent has altered. If a change is recent and
the trouble started after it, `rollback` it and see. That is cheap and
reversible, and it is a much faster answer than reading YAML.

`read_config_file` on `/home/fuwenxu/AIPI5/config/aipi5.yaml` if you need to
look. Note that nothing re-reads this while running — a change only takes
effect on restart, so a file that looks right and a device that behaves wrong
means it has not been restarted since.

## 4. What not to conclude

- **A blank screen is not a dead assistant.** `aipi5-ui.service` is
  `PartOf=aipi5.service`, so restarting the assistant blanks the touchscreen
  for ten to twenty seconds. If somebody says "the screen went black", check
  whether the assistant restarted at that moment before looking anywhere else.
- **`NRestarts: 0` after a reboot means nothing.** The counter resets.
