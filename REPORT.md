# AIPI5 — engineering report

The report section 38 of the implementation procedure asks for, written against
what has actually been done on the device rather than against what the code
intends.

**Deployed and running on `aipi5.local`.** All ten startup checks pass; 184
tests pass on the Pi as well as off it. Verified in the room: a spoken command,
live weather and local news through the speaker, a camera description of the
actual room, person detection on the accelerator, and the screensaver
completing a full engage / clear / return cycle. Re-verified after the camera
was replaced with a USB Brio 101 — the device opens by name, the detector reads
it every 500 ms, and "what do you see" reached the speaker with a correct
description of the room. What has *not* been done is in
§25 — chiefly Cantonese, and most of the Kodama commands by voice.

Verified in the room, from the journal: the wake word in Mandarin, a Chinese
question answered through a tool (`现在天气怎么样？` → `get_weather` → a spoken
Chinese answer), the camera describing the person in front of it in Chinese,
local news summarised in Chinese, and the screensaver clearing on
`person returned; leaving the screensaver`.

---

## 1. Project architecture

```
Microphone ─→ Wake word ─→ VAD ─→ STT ─→ Intent router ─┬─→ Kodama-Lite plugin
   (AIA)       (AIA)       (AIA)  (AIA)      (AIA)       ├─→ System plugin
                                                          ├─→ Kodama launcher  ← new
                                                          └─→ OpenAI + tools   ← new
                                                                   │
                                                    ┌──────────────┼─────────────┐
                                                 weather         news         camera
                                                  (new)          (new)     vision (new)
                                                                   │
                                        Piper TTS (AIA) ←──────────┘
                                             │
                                          Speaker

Brio 101 (USB) ─→ person detection (AI HAT+ 2, new) ─→ presence ─→ screensaver
                                                                       (new)
1280×800 touchscreen ← local HTTP server (new)
```

Two paths and one fork. Anything the phrase router recognises is executed
deterministically and never reaches a model. Anything it declines goes to
OpenAI with a tool list. That fork is the whole of what this project adds to the
voice loop; everything before it is AIA's.

## 2. AIA components reused

Imported and driven as they are, with no modification to the AIA checkout:

| component | module | why not reimplemented |
|---|---|---|
| Wake word 小艾同学 | `aia.audio.wake` | Vosk phrase matcher with a pinyin comparison and an onset guard, tuned over 40 attempts by this speaker; the variants list encodes that the leading 小 drops about half the time |
| Microphone capture | `aia.audio.capture` | 48 kHz capture decimated by exactly 3 because the TI PCM2902 refuses 16 kHz outright; two named mic profiles with measured gains |
| Endpointing | `aia.audio.vad` | every threshold derived from real captures — `min_speech_ms` 500, `min_run_ms` 400, `preroll_ms` 700 |
| Speech recognition | `aia.stt` (SenseVoiceSmall INT8) | Cantonese CER 0.06 against Whisper's 0.66; per-utterance language detection |
| Synthesis | `aia.tts.piper` | resident process per voice, because loading is 452/1190 ms against 304/224 ms to synthesise |
| Reply language | `aia.tts.language` | the one place a language code maps to a voice; Cantonese answered in Mandarin |
| Intent router | `aia.router.fast` | pinyin matching, bare-trigger detection, the `_is_command` guard, multi-command splitting |
| Kodama commands | `aia.plugins.kodama` | 22 commands including the three lyrics commands whose separation rests on raised score floors |
| System commands | `aia.plugins.system` | shutdown, reboot, network — all `confirm=True` where destructive |
| Plugin contract | `aia.plugins.base` | `CommandSpec.confirm` is what this project's tool filter keys on |
| Confirmation | `aia.main.CONFIRM_PROMPT`, `is_affirmative` | compares Chinese by sound, because 确定 arrives simplified and traditional from the same speaker in one evening |
| Conversation store | `aia.ui.history` | queue-and-return so the voice loop never waits on the SD card |
| Retention | `aia.ui.retention` | 24-hour expiry with the newest-100-recordings floor |
| Ducking | `aia.audio.ducking` | pauses the player before capture, restores after |
| Turn timing | `aia.core.state` | judges on time-to-audio, not total |

Mechanism: `aipi5/core/aia_bridge.py` locates the checkout (`AIA_HOME`, then
`~/AI_Assit`, then `../AI_Assit`) and appends it to `sys.path`. The systemd unit
sets `AIA_HOME` explicitly.

**Nothing in AIA was modified.** The AIA repository is untouched by this work.

## 3. Kodama-Lite components reused

Nothing is duplicated. Kodama-Lite is driven exactly as AIA drives it:

* **MPRIS via `playerctl`** for transport — play, pause, next, previous, stop,
  metadata.
* **The control endpoint** at `~/.local/state/kodama-lite/control.json` (mode
  0600, per-launch random token and port, re-read on every command) for
  everything the frontend owns: search, play-by-query, volume, shuffle, repeat,
  like, the three lyrics actions, karaoke, home, play local, play liked, quit.
  Its 14 actions are validated server-side by Kodama-Lite and a 400 means the
  installed build predates the action.
* **The systemd user unit** `kodama-lite.service` for launching — the only
  addition this project makes, and it uses the unit rather than the binary
  because Kodama-Lite's README is explicit that launching the binary directly
  starts a second copy with its own stream-server port.

No new protocol, no changes to Kodama-Lite.

## 4. Files created

38 files, all new, none outside this repository.

```
aipi5/core/aia_bridge.py          finds AIA, one place
aipi5/core/config.py              YAML settings layered over AIA's Config
aipi5/core/presence.py            debounce + screensaver policy (no I/O)
aipi5/core/preflight.py           section 31 checks; decides what is fatal
aipi5/llm/client.py               OpenAI client, tool loop, request negotiation
aipi5/llm/conversation.py         bounded, self-expiring history
aipi5/llm/tools.py                the security boundary
aipi5/llm/prompts.py              the whole system prompt, readable in one file
aipi5/tools/weather.py            Open-Meteo, cached, stale-on-failure
aipi5/tools/news.py               RSS/Atom, interleaved, de-duplicated
aipi5/tools/clock.py              the device's clock, not the model's guess
aipi5/tools/story.py              bedtime story length, subject and safety rules
aipi5/vision/camera.py            one V4L2 handle, shared by both readers
aipi5/vision/describe.py          capture → vision model → sentence
aipi5/vision/person_detection.py  hailo / cpu / disabled + the watcher thread
aipi5/kodama/launcher.py          the one command AIA lacks
aipi5/ui/server.py                page + 4 JSON routes, loopback
aipi5/ui/state.py                 the shared snapshot and the action queue
aipi5/ui/web/index.html           the 1280×800 screen and the screensaver
aipi5/main.py                     the loop
config/aipi5.yaml                 the settings a person changes
systemd/aipi5.service             the assistant
systemd/aipi5-ui.service          the kiosk browser
scripts/check_hardware.sh         phase 2, to run on the Pi
scripts/install-service.sh        install, with preflight
scripts/get_person_model.sh       fetches the HEF for the fitted accelerator
scripts/aipi5-ui.sh               Chromium, full-screen, waits for the server
tests/ (12 modules, 184 tests)
README.md, REPORT.md, requirements.txt, .gitignore
```

## 5. Files modified

**None.** No file in `~/AI_Assit` or in `Kodama-Lite` was changed. That was a
design goal — section 39 rule 26, do not remove existing functionality to make
implementation easier — and it is also what makes the reuse honest: AIA
improvements reach this assistant when they are made.

## 6. Wake-word implementation

AIA's, unchanged: **小艾同学**, a small Vosk Chinese recogniser with the phrase
matched in its output by toneless pinyin at similarity 0.72, with a first-
syllable onset guard that stops 同学 (ordinary speech, 0.875) from waking it.
Four accepted variants collapsing to two distinct sounds. `AIA_NO_WAKE=1` still
bypasses it. Porcupine remains available as a backend behind the same interface.

No new wake-word system was created. Section 7 and rule 4.

## 7. STT implementation

AIA's, unchanged: **SenseVoiceSmall INT8 through sherpa-onnx**, in-process,
`language: auto`, ITN on. Recognises zh/en/yue and reports which; `_REPLY_IN`
folds Cantonese onto the Mandarin voice. Offline — no network at runtime, no API
key, no cloud fallback. whisper.cpp remains as the fallback backend behind the
same interface via `stt.backend` / `AIA_STT_BACKEND`.

No benchmarking of alternatives was done, because rule 5 says to reuse it unless
testing proves otherwise and no such testing has been run.

## 8. TTS implementation

AIA's, unchanged: **Piper**, one resident process per voice
(`en_US-lessac-medium`, `zh_CN-huayan-medium`), warmed at startup, output to
`/dev/shm`. `Speaker.warm()` still probes the output device and reports a dead
sink loudly. Cantonese is answered in the Mandarin voice because Piper ships no
`yue` voice.

## 9. OpenAI implementation

`aipi5/llm/client.py`. One `OpenAI` client built at startup and kept for the
life of the process — the client object, the connection pool and the TLS session
are all constructed once, which is the honest reading of section 12's "loaded to
RAM at boot" for a model reached over an API. Nothing is downloaded to the Pi.

* Model from `openai.model` in the YAML. **`gpt-5.6-luna`** — see §9a.
* `probe()` sends one real completion at startup and reports the outcome by
  name. A rejected model is a degraded mode, not a boot failure.
* Retries: SDK retries disabled (`max_retries=0`) so the configured timeout
  bounds the whole attempt; one retry here for transient failures only —
  timeouts, connection errors, 408/409/429/5xx. Never for 400 or 401.
* Request shape negotiated once per process between `max_completion_tokens` and
  `max_tokens`, from the API's own rejection rather than from the model name.
* Tool loop bounded at 3 rounds; the final round is sent with no tools offered
  so the turn always ends in a sentence.
* `_explain()` turns API failures into one actionable line.

## 9a. Model selection

**Correction to an earlier statement in this report: GPT-5.6 Terra is a real
model.** It was reported here as not existing. It does — GPT-5.6 shipped in
three tiers (Sol, Terra, Luna) and Terra is the middle one, at $2.00/$12.00 per
million tokens.

The deployed model is nonetheless **`gpt-5.6-luna`**, chosen on cost and
latency together:

| tier | id | input / output per 1M | est. cost here |
|---|---|---|---|
| Sol | `gpt-5.6-sol` | $5.00 / $30.00 | ~$16/mo |
| Terra | `gpt-5.6-terra` | $2.00 / $12.00 | ~$6.50/mo |
| **Luna** | **`gpt-5.6-luna`** | **$0.20 / $1.20** | **~$0.65/mo** |

Estimate basis: ~30 turns/day; per turn a ~1,250-token prefix (system prompt +
tool schemas, cacheable at a 75–90% discount), ~640 tokens of carried history,
a ~20-token utterance and an ~80-token reply, with about half of turns making
one tool round trip. Long-context rates (above the standard threshold) are
roughly double across all three tiers and this workload never approaches them —
the context is bounded at 8 turns by design.

Why Luna is sufficient rather than merely cheap:

* **Capabilities.** Vision input, function/tool calling, prompt caching and
  structured outputs. Those are the only four this project uses.
* **Tool shape.** Five tools, arguments constrained by enum, and the Kodama
  command name checked against a fixed table on return. The routing decisions
  the model makes here are easy ones; the hard routing is the phrase matcher's
  and never reaches a model.
* **Vision.** Describing a room is a reasoning task, and reasoning is Luna's
  strongest vision result (87%, 2nd of 16 on Roboflow's Vision Evals) rather
  than its weakest (OCR, 88.4%, 12th of 16). `vision_model` is left empty so it
  follows `model`.
* **Latency.** The decisive argument. The model sits on the slow path — known
  commands are matched in ~9 ms and never touch it — so the API round trip is
  the entire wait a person experiences on an open question. Luna is the tier
  built for latency-sensitive chat.

**When to move up.** Bedtime stories are the one output here judged on prose
rather than on being correct and short. If they come back thin, set
`openai.model: gpt-5.6-terra` — one line, about six dollars a month. The
`vision_model` field exists so the two can be split if only one needs it.

Sources: [OpenAI GPT-5.6 pricing](https://www.eesel.ai/blog/gpt-5-6-pricing),
[GPT-5.6 tiers compared](https://poyo.ai/hub/gpt-5-6-benchmarks-sol-terra-luna),
[Luna vision evals](https://playground.roboflow.com/models/openai/gpt-5-6-luna).

## 10. Conversation context implementation

`aipi5/llm/conversation.py`. Bounded by **turns**, not tokens, because a turn is
what a person perceives and a token limit trims at a boundary nobody can
predict. Default 8 turns. Forgotten entirely after 600 s of silence — somebody
arriving an hour later is starting a new conversation.

Tool calls travel with the turn that caused them: `_trim` cuts only at user
messages, so an assistant message carrying `tool_calls` is never separated from
its `tool` results. The API rejects that pair being split with an error about
mismatched ids that says nothing about trimming, and it only happens once a
conversation is long enough to trim. Pinned by
`test_a_tool_call_and_its_result_are_never_separated`.

The system prompt is not stored — it is rebuilt every request because it carries
the current time and the language of the utterance.

## 11. Weather implementation

`aipi5/tools/weather.py`. **Open-Meteo**, no API key, San Jose 95127 at
37.3708/−121.8163, Fahrenheit, `America/Los_Angeles`, current conditions plus a
four-day forecast. WMO codes mapped to phrasing chosen to be *said* aloud.

Cached ten minutes, which turns a screensaver's worth of demand from 86,400
requests a day into ~144. A failed refresh keeps the last good reading rather
than blanking the screensaver. Never raises.

One representation: the screensaver and the model are handed the same
dictionary, so the temperature on screen and the temperature in the spoken
answer cannot disagree.

## 12. Local-news implementation

`aipi5/tools/news.py`. Three feeds — Google News scoped to San Jose / Santa
Clara County, the Mercury News' Santa Clara County feed, San José Spotlight —
parsed with the standard library, RSS and Atom both.

Round-robin interleaved so the busiest publisher cannot fill every slot, and
de-duplicated by Jaccard ≥ 0.6 over keyword sets so one council vote covered by
three outlets is one story. Headlines and blurbs only, never article bodies; the
model summarises 3–5. Cached 15 minutes; one dead feed is skipped, not fatal.

## 13. Bedtime-story implementation

`aipi5/tools/story.py`. Length is derived from **speech rate** — 150 words/min
English, 240 chars/min Mandarin, measured against this project's Piper voices —
so "a four-minute story" becomes a word budget. Adjectives ("very short",
"long"), explicit minute counts, and Mandarin forms are all parsed, and the
subject is extracted from the transcript.

Six safety rules, as a readable list rather than a paragraph, sent verbatim:
nothing frightening, nothing sad at the end, no romance or brands or real
people, gentle pacing, TTS-friendly text, answer in the language asked. A test
asserts every one of them reaches the model.

## 13a. The screen's five pages, and what protects them

Added after the first deployment, alongside the camera change.

**Pages, not windows.** The five buttons — Talk, Camera, News, Weather, Music —
each open a dedicated destination, and those destinations are views in the one
kiosk document rather than browser windows. Chromium runs full-screen here
under a compositor with no title bars and no taskbar: a second window is a page
nobody can get back from. It also turns "no duplicate instances of the same
page" from a rule into a property — `show()` is idempotent, so pressing Camera
twice cannot produce two camera pages.

Deep links (`/#weather`) navigate without posting the action. That exists for
the device rather than for anybody using it: the panel is Wayland with no way
to inject a tap over ssh, so without it there is no way to look at a page you
have just changed without standing in front of the assistant.

**A ten-second cooldown per button, on the server.** `UiState.request` refuses a
repeat and publishes the seconds remaining, which the button draws as a
countdown — a button that is merely dead reads as broken, which is what makes
somebody press it again. Independent per action, so Camera never disables
Weather. `wake` is deliberately exempt: rationing the way a person gets the
assistant's attention would mean a device that ignores somebody who tried to
talk to it twice.

**The camera page** streams `multipart/x-mixed-replace` from the shared camera
at 6 fps, which an `<img src>` understands natively — no decoding code, no
reconnection logic. 6 rather than 15 because the budget being spent is the
camera *lock*, not the network: the person detector wants the same lock twice a
second. Measured with a preview open, the detector held its full 2.0 fps and
the stream ran at 5.4.

The answer is drawn over the picture it is about and fades ten seconds after
the speaking stops — the timer starts on the edge out of `speaking`, so a long
reply holds its text for its whole length and the ten seconds is ten seconds of
silence rather than ten seconds total. Descriptions carry an incrementing id
rather than being compared by text, because two identical descriptions of an
unchanged room are two answers and the second must re-show.

**The weather and news pages speak less than they show.** The weather page
displays temperature, high/low, feels-like, UV index, humidity, wind and chance
of rain; `Weather.brief` says the sky, the temperature, the day's range and at
most one thing worth acting on — an umbrella at 40% rain, sunscreen at UV 6.
Reading back a screen somebody is already looking at is the most common way a
device like this becomes tiresome. The news page shows the stories and the
assistant summarises the important ones in two sentences.

Page-spoken lines are recorded under their own role (`aia:weather`,
`aia:news`, `aia:camera`, `aia:music`) and `/api/feed?roles=user,aia` filters
them out of the Talk page. They stay in the 24-hour transcript because they
were audible in the room and that record should not lie; they are kept out of
the conversation because a conversation is a conversation.

**Music raises rather than relaunches.** `KodamaLauncher.raise_window` runs the
binary — the one place that is allowed — because Kodama-Lite is built with
`tauri-plugin-single-instance`, so a second launch hands its argv to the
running process, which raises its window, and exits. Verified rather than
assumed: with the player running, the process count stayed at 1, the launched
copy exited on its own, and `playerctl -l` still listed one `kodamalite`.
`wmctrl` and `xdotool` are both installed and both are X11 clients on a Wayland
session, so there is no alternative on this hardware.

## 13b. Audio priority

`aipi5/core/audio_priority.py`. The assistant's voice outranks everything else
in the room: whatever is playing is paused for the duration and resumed where
it stopped. Pausing over MPRIS rather than muting is AIA's existing decision
and the right one — a muted song keeps playing and loses the seconds it was
silent for.

What is new is that **every** path that speaks holds it. The voice loop already
ducked around a whole turn; a button never went through the voice loop, so
until now a Weather or News press talked straight over the music.

Making the button paths duck introduces a subtler bug than it fixes, which is
what this module is for. `Ducker.duck()` begins by clearing its memory of what
it paused, so two overlapping ducks — a button pressed mid-turn, a page
speaking while the loop holds the floor — leave the inner call remembering
nothing and the outer call's memory gone with it. The music never comes back,
and never comes back *silently*. `AudioPriority` counts holders behind a lock
and only the outermost touches the bus.

Measured on the device: playing at 106.5 s, `Paused` at 108.7 when the Weather
page spoke, `Playing` again at 108.9 nine seconds later — resumed from
position, not restarted.

## 14. Camera implementation

`aipi5/vision/camera.py`. **One `cv2.VideoCapture` on a V4L2 node**, opened
once and shared under a lock, because the camera allows one owner and two
consumers want frames — the detector twice a second and the vision question
when asked.

The hardware changed after the first deployment: the CSI **Camera Module 3 was
replaced with a USB Logitech Brio 101**, so picamera2 (which speaks to
libcamera on the ribbon connector and does not see a webcam at all) gave way to
OpenCV over V4L2. Three things followed from that and none of them is a
like-for-like port:

*The two-stream trick is gone.* picamera2 produced a 1280×720 `main` and a
640×480 `lores` from one sensor read. UVC gives one stream at one size, and the
loss is nil: the detector resizes to its model's input as its first step, so
`lores` was only ever pixels it threw away.

*Frames have to be drained, and not for the obvious reason.* V4L2 is a queue
and returns the oldest filled buffer, so the first instinct is to walk to the
end of the queue. That is not enough. Both readers arrive 500 ms apart at the
soonest, the driver fills its queue within a few frame periods of the previous
read and then drops frames until somebody returns — so *every* buffer in the
queue was captured just after the last read, and the newest of them is still
~450 ms old. `Camera._read` therefore drains the queue **empty** and takes the
next frame the sensor produces: the one grab that blocks. Grabbing without
retrieving costs no JPEG decode, so the whole call is one frame period.

How many grabs that takes is asked of the device rather than assumed. This
driver honours `CAP_PROP_BUFFERSIZE=1`, so two grabs suffice; OpenCV's default
of four would need five. Assuming the default cost 200 ms a read against the 68
ms it actually takes — most of what a person waits for after "what do you see".
Measured on the Brio: `frame()` 59–70 ms (of which ~5 ms is decode; the rest is
the wait, and the camera halves its own frame rate in a dim room),
`capture_still()` 144 ms including the JPEG encode, Hailo inference 40–49 ms on
top. picamera2 gave the current frame for nothing.

*The device is found by driver, then by name.* `/dev/video0` is not a stable
identity; the Brio claims two nodes and the metadata one opens cleanly and
never yields an image. `_candidates` ranks on the sysfs driver (`uvcvideo` is
every USB webcam and nothing else here), then `name_hint`, then the UVC node
index — and `_try_open` accepts a node only once it has produced a decoded
frame. The same reasoning AIA applies to matching the microphone by name rather
than card number.

The eighteen ISP and HEVC-decoder nodes this Pi also has are **dropped, not
merely ranked last**, and that came out of a measurement: with the camera
merely busy, refusing all of them took **81 seconds** — during which `open()`
is on the startup path and the microphone is not up yet. An assistant that
cannot see must not also be a minute of an assistant that cannot hear. After
the filter, the same failure takes 0.8 s, and `SEARCH_BUDGET_S` bounds whatever
is left.

Warm-up became real reads rather than a sleep, because a UVC sensor does not
stream — and so its auto-exposure does not converge — until buffers are being
dequeued. A sleep there would have warmed up nothing.

A still is captured fresh on every request (section 19), written to
`/dev/shm/aipi5-camera`, base64'd at send time, pruned to the last ten. Frames
are never sent to OpenAI except when somebody asks. A missing or broken camera
is `None` and a log line, never an exception.

## 15. Person-detection implementation

`aipi5/vision/person_detection.py`. Local, on the AI HAT+ 2, never uploaded.
YOLOv8n HEF through HailoRT; SSD-MobileNet through onnxruntime as an explicit
CPU alternative; `disabled` as a third. **No automatic fallback between them.**

Output parsing handles three shapes (Hailo NMS per-class lists, SSD parallel
arrays, single N×6 arrays) because that varies more between model versions than
between families. Detection runs on a daemon thread at 500 ms, sleeping the
remainder so a slow inference does not stretch the cadence, and it never dies —
an exception is logged and the next frame is tried.

Debounce in `aipi5/core/presence.py`, which has no camera in it: 2 consecutive
frames to arrive, 8 to leave, starting at `UNKNOWN` rather than absent so the
first seconds after boot do not begin the screensaver countdown.

## 16. 1280×800 UI implementation

`aipi5/ui/`. A local page served from a daemon thread, opened full-screen in
Chromium. Header with the weather, conversation feed, status line with a state
dot, five buttons. Polls state at 500 ms and the transcript at 1 s, with chained
timeouts rather than intervals so a device that was asleep does not fire a burst
of missed ticks.

The old 1920×440 geometry is not inherited anywhere; AIA's layer-shell strip is
not used. The buttons make this UI non-read-only, which is a deliberate
departure from AIA's stance — mitigated by the action list being a fixed tuple
containing nothing destructive.

## 17. Screensaver implementation

Same page, an overlay at full 1280×800: a 210 px clock redrawn every second from
the browser's clock corrected against the Pi's, the date, and the current San
Jose weather.

Up 60 s after presence is lost; down the instant presence returns, with no touch
required. A touch also takes it down, and so does speaking to the device from
outside the camera's view (`ScreensaverPolicy.suppress`). All of the timing is
tested off-device.

## 18. Kodama integration

AIA's plugin registered unchanged — all 22 commands, both languages, MPRIS plus
the control endpoint. One command added: `open_kodama`, which starts
`kodama-lite.service` and then polls MPRIS until the player answers or
`start_timeout_s` expires, because `systemctl start` returns long before a Tauri
webview has published MPRIS.

`KodamaLauncher.available()` is always True, unlike AIA's Kodama plugin — the
command exists precisely for when the app is closed, and a plugin reporting
itself unavailable then would have its own launch command refused by the check
that protects the others.

`tests/test_routing.py` verifies every existing command still routes, in both
languages, with the new plugin in the registry.

## 19. systemd / startup implementation

Two **user** services, because the assistant needs the session bus (MPRIS, and
starting Kodama-Lite) and the Wayland display.

* `aipi5.service` — `ExecStartPre` waits for the compositor socket;
  `Conflicts=aia.service`; `StartLimitIntervalSec=0` so it retries forever;
  `Restart=on-failure` with `RestartSec=5` because the microphone is exclusive
  and a dying instance still holds it; `TimeoutStartSec=180`; `Nice=5`.
* `aipi5-ui.service` — `Requires=aipi5.service`, runs the Chromium script,
  which waits for the server before opening so it cannot land on an error page
  an `--app` window has no address bar to leave.

`install-service.sh` checks eight prerequisites before installing, disables
`aia.service`, and stops hand-started instances.

## 20. Configuration and API-key setup

`config/aipi5.yaml` — display, location, OpenAI, weather, news, story, camera,
person detection, screensaver, Kodama, assistant. Missing file is defaults (all
of them the specified values); malformed file raises at startup.

Everything about *sound* is deliberately absent and stays in AIA's config where
it was measured. `Settings.aia_config()` changes exactly two AIA fields: AIA's
own web UI off, and retention hours from this project's setting.

Credentials: `OPENAI_API_KEY` first, then a key file beside the project
(`openai API.txt`, `openai_api_key.txt`, `.openai_key`, or an explicit
`OPENAI_API_KEY_FILE`). Never from the YAML. Never logged —
`describe_credentials()` returns presence, source and the last four characters.
Every readable filename is in `.gitignore`, asserted by
`test_every_key_filename_is_gitignored`.

## 21. Test results

```
Ran 119 tests in 0.60s
OK
```

| module | tests | covers |
|---|---|---|
| `test_config.py` | 20 | defaults, malformed YAML, zero-value guards, AIA layering, credentials |
| `test_tool_safety.py` | 16 | what is offered, what is refused, invented names, malformed JSON, argument binding |
| `test_news.py` | 16 | RSS + Atom parsing, entity/markup order, syndication suffix, interleaving, de-duplication, broken XML |
| `test_presence.py` | 15 | debounce, dropped frames, consecutive-run rule, screensaver timing, return, suppress |
| `test_story.py` | 15 | subject extraction (both languages), length parsing, safety rules |
| `test_routing.py` | 14 | every existing Kodama command, both languages, launcher phrase margins |
| `test_weather.py` | 12 | parsing, cache hits and expiry, stale-on-failure, phrasing in both languages |
| `test_conversation.py` | 11 | trimming, tool-call/result pairing, idle expiry, follow-ups |

Two of these found real defects during development, both in work written in this
session:

* `test_the_mandarin_phrases_keep_their_measured_margin` disproved a comment
  claiming 打开音乐 scores 0.80 against 播放音乐. It scores **0.609**. The
  comment was corrected and the phrase — which the measurement showed is safe —
  was added rather than excluded.
* `test_respects_the_limit` showed that headlines whose only distinguishing word
  is short collapse as duplicates. The behaviour is correct; it is now pinned.

**Not tested, because it needs the device:** wake word, capture, endpointing,
STT accuracy, Piper output, MPRIS, the control endpoint, the camera, Hailo
inference, the browser, systemd, and every OpenAI request.

## 22. Measured latency

Measured on `aipi5.local`, 2026-08-07.

**A real spoken turn**, wake word to speaker, English:

```
wake phrase detected: heard '小爱同学' (1.00) in '小爱同学'
stt <Transcript en 187ms 'Stop the music.'> (2430 ms audio, RTF 0.08)
fast path: <Intent kodama.pause {} score=0.86>
turn 2757ms to audio [OVER by 257ms] · captured=2501 stt=2688 routed=2714
                                        acted=2747 audio_out=2757
```

Read the deltas rather than the total. Capture is 2,501 ms of that — 2,430 ms
of the person actually speaking plus the endpointer's silence window — and
everything the assistant does with it takes **256 ms**: 187 ms to transcribe,
26 ms to route, 33 ms to act, 10 ms to start speaking. The 257 ms overrun is a
sentence that took two and a half seconds to say, not an assistant that was
slow, and the budget counts from the wake word so a slow speaker spends it.
STT at RTF 0.08 matches AIA's measured figure.

| stage | measured | note |
|---|---|---|
| model load (SenseVoice) | 1,949–1,971 ms | once, at boot |
| SenseVoice warm | 55 ms | on 500 ms of silence |
| Piper voice load | 0 ms | resident process, warmed after |
| Piper warm — en / zh | 535 / 160 ms | once, at boot |
| audio output probe | 173–177 ms | proves the sink before a reply needs it |
| wake model load | 517–524 ms | Vosk |
| **fast-path routing** | **9.7–22.7 ms** | see below |
| weather turn, to audio | **1,117 ms** `[OK]` | live Open-Meteo + Piper |
| OpenAI, plain completion | 1,875 / 3,042 / 6,803 ms | the startup probe, three boots |
| OpenAI, one tool round trip | 3,839 ms | `get_local_news` → summary |
| total boot to "ready" | ~23 s | cold, including the API probe |

Routing, per utterance, on the device:

```
pause                    -> pause          10.4 ms
下一首                    -> next            9.7 ms
play hotel california    -> play           22.7 ms
打开音乐播放器             -> open_kodama     12.9 ms
```

That confirms the design premise: a known command is answered two orders of
magnitude faster than the model could, and never reaches it. The `play` case is
slower because it also runs the "is this argument really a command" guard.

A real journal line, after the button-path timing fix:

```
turn 1117ms to audio [OK] of 9179ms total · acted=0 audio_out=1117
```

The 9,179 ms total against 1,117 ms judged is the point of judging on
time-to-audio: eight of those seconds are Piper reading the answer, which is
the answer arriving, not latency.

**The LLM path does not meet the 2.5 s budget and is not expected to** — 3.8 s
for a tool round trip is the API, and it is the entire reason the fast path
exists.

## 23. CPU / RAM usage

Measured with the assistant idle, Kodama-Lite playing, and the other AI stack
on this Pi (hailo-ollama, open-webui) also resident:

```
aipi5:  717 MB RSS, 26.0% CPU
system: 3,760 MB used of 7,950
load:   1.93 (1 min), 4 cores
```

717 MB is consistent with AIA's measured ~603 MB for SenseVoice plus the Vosk
wake recogniser, two Piper processes and this project's threads. There is
comfortable headroom on an 8 GB Pi.

Not separated out yet: Chromium's share, and the wake recogniser's ~49%-of-a-
core while somebody is speaking. Both want a measurement with a person in the
room.

## 24. AI HAT+ 2 utilisation

**Working. YOLOv8m person detection at 28 ms an inference, twice a second.**

Two findings, both resolved, and both worth recording because either one alone
looks like a dead accelerator:

**The accelerator is a Hailo-10H, not a Hailo-8 or 8L.** Their HEFs are not
interchangeable — the wrong one fails at configure time with an architecture
mismatch. `scripts/get_person_model.sh` originally hardcoded the
`hailo8`/`hailo8l` model-zoo paths; it now reads the architecture from
`hailortcli` and finds the matching model. On this device that needs no
download at all: `hailo-all` installs
`/usr/share/hailo-models/yolov8m_h10.hef`, which the configuration points at.
yolov8m rather than yolov8n because the nano model has no compiled h10 variant,
and at two frames a second on an accelerator the difference is not perceptible.

**HailoRT's classic inference API is not implemented on the 10H.** Every
Raspberry Pi example uses `ConfigureParams.create_from_hef` →
`network_group.activate()` → `InferVStreams`, and on this part every one of
them ends at:

```
libhailort failed with error: 7 (HAILO_NOT_IMPLEMENTED)
```

The device was seated and `hailortcli fw-control identify` answered correctly
throughout, which is what made this look like broken hardware. The fix is
`VDevice.create_infer_model()` — the API HailoRT 4.18+ recommends generally, so
this is the current way rather than a 10H workaround. The device, the model and
the configured model are built once and held; configuring per frame would put
the multi-context load of a 5-context HEF on every frame.

**The output is post-processed on-chip**, so what comes back is not boxes but a
flat float32 buffer in HAILO NMS-BY-CLASS layout: per class, a count followed by
that many 5-float boxes. The sizes confirm it rather than assume it —
`hailortcli parse-hef` reports 80 classes at 100 boxes each, and
80 × (1 + 100 × 5) = 40,080 floats, which is the buffer size the model
declares. `decode_nms_by_class` walks that in plain Python (a few hundred floats
of the forty thousand are ever read) and is unit-tested off-device in
`tests/test_nms_decode.py`, including truncated and negative-count buffers.

Measured: **28 ms** per inference, 640×640×3 UINT8 in, at `interval_ms: 500`.

## 25. Known issues

**Open:**

1. **Cantonese has not been spoken to it.** English and Mandarin are both
   verified by real utterances; `yue` is recognised by SenseVoice and answered
   in the Mandarin voice by design, but nobody has said anything in it.
2. **Most Kodama commands are untested by voice.** `pause` is confirmed end to
   end. The other twenty-one route correctly in `tests/test_routing.py` but
   have not been spoken.
3. **The wake word can fire on ordinary speech.** Observed once:
   `heard '碍同学' (1.00) in '妨碍同学'` — the recogniser produced a phrase
   containing the wake word's sound inside an unrelated word. AIA documents
   this as the cost of a general recogniser doing a wake word's job, and names
   Porcupine as the fix; the backend is already written and needs an access key.
4. **This Pi runs a second AI stack** — `hailo-ollama`, open-webui on :8080,
   `piper-tts.service`, faster-whisper models. No port collision (AIPI5 is on
   8092) but they compete for four cores, and AIA's latency budget assumes it
   has them.
5. **The journal needs `sudo`.** The user is not in `systemd-journal`, so
   `journalctl --user -u aipi5` returns "No entries". Fix:
   `sudo usermod -aG systemd-journal $USER` and log out.
6. **AIA reports its version as "unknown"** — it stamps the version in via
   `git archive` at deploy time and this is a plain `git clone`. Cosmetic.

**Fixed during deployment**, each found by running it rather than reading it:

7. **`reasoning_effort` with tools.** `gpt-5.6-luna` accepted the startup
   probe and rejected every request carrying a `tools` array:
   *"Function tools with reasoning_effort are not supported … set
   reasoning_effort to 'none'."* So the assistant booted healthy and failed on
   the first question needing a tool. Now negotiated and remembered alongside
   the token parameter, with `tests/test_client_negotiation.py` pinning it.
8. **`libportaudio2` missing.** `sounddevice` is a binding, not the library;
   the service crash-looped on `OSError: PortAudio library not found`.
9. **The venv could not see system packages.** `cv2` (`python3-opencv`) and
   `hailo_platform` are installed as Debian packages, so a venv built
   without `include-system-site-packages` reported no camera and no
   accelerator — a *degraded* start, not an error, which is the silent kind.
   The install script now checks for it.
10. **Button presses logged a spurious budget violation** the length of
    whatever was spoken — a news summary read aloud over 25 s was reported as
    `turn 28666ms [OVER by 26166ms]`. The button path now marks `audio_out`
    where the voice path does.
11. **`install-service.sh` checked the wrong Hailo path** and could not see a
    key in the systemd drop-in, so it reported a working model and a present
    key as missing. Both now read from where the values actually live.

12. **The screensaver never came back after activity.** `suppress()` cleared
    the countdown outright, which reads as "wait for presence to say the room
    is empty again" — but presence had already said so, and the tracker only
    reports *changes*. One spoken command in an empty room removed the
    screensaver permanently; verified on the device, still showing the full UI
    to nobody 75 seconds later. It now restarts the countdown from the
    activity, unless somebody is actually in frame.
13. **Restarting the assistant killed the screen for good.** `aipi5-ui` had
    `Requires=aipi5.service`, which propagates a *stop* but not a *restart*,
    and `Restart=on-failure`, which ignores a clean exit. So the documented way
    to pick up a code change left a working assistant talking to a dark
    display, with the unit sitting `inactive (success)` as though that were
    deliberate. Now `PartOf=` plus `Restart=always`.
14. **A missing microphone produced a traceback.** Opening the capture device
    is the one critical step that raises rather than returning a status, so an
    unplugged capsule reached the journal as twenty-one lines of stack. Now one
    line, naming what to plug in, with the service retrying until it appears.
15. **A false "network unavailable" banner** — the 2 s probe overran under boot
    load while OpenAI answered in 2.3 s. Boot-time probes now allow 6 s.
16. **The camera search took 81 seconds to fail.** Found while testing the new
    USB camera's degraded path, with the camera merely busy: this Pi has twenty
    `/dev/video*` nodes, eighteen of them the ISP and the HEVC decoder, and
    OpenCV takes about two seconds to refuse each. `open()` runs before the
    microphone does, so a camera somebody had unplugged would have cost a
    minute of an assistant that could not hear either — the worst kind of
    degraded mode, because it looks like a hang. Nodes are now filtered by
    their sysfs driver before anything is opened (0.8 s), with a time budget
    behind that.
17. **The hardware check reported a format the camera offers.** `v4l2-ctl
    --list-formats-ext | grep -q 1280x720` under `set -o pipefail`: `grep -q`
    exits at the first match and SIGPIPEs `v4l2-ctl`, so the pipeline fails on
    exactly the runs where the format *was* found. Read into a variable now.

**Design limits, unchanged and deliberate:**

18. The `interval_ms` sleep can drift when an inference takes longer than the
    interval; the remainder is what is slept, so it degrades to inference time.
19. The button queue is depth 2 — tapping while the assistant speaks drops
    presses with a debug line.
20. Story length is a target, not a contract. Nothing truncates, because
    truncation is read aloud as a sentence stopping mid-word.
21. No Cantonese Piper voice. Inherited from AIA: Cantonese is recognised as
    Cantonese and answered in Mandarin.
22. The UI accepts input, unlike AIA's. Mitigated by a fixed action tuple with
    nothing destructive in it.
23. Every camera read waits for a live frame rather than accepting a queued
    one, so it costs one frame period — 35 ms in a lit room, ~70 ms in a dim
    one where the camera has halved its own rate. Deliberate: the alternative
    is a description of the room as it was half a second ago.

## 26. Recovery and error handling

Section 37's reliability criteria, and where each is implemented:

| requirement | how |
|---|---|
| starts after boot | user service on `default.target`, `StartLimitIntervalSec=0` |
| not stuck after an API failure | `OpenAIClient` never raises; every path returns a `Reply` with a speakable error; the SDK's own retries are disabled so the timeout bounds the attempt |
| not stuck after an STT failure | AIA's contract — an empty `Transcript`, an apology, and the loop continues |
| camera failure does not kill it | every `Camera` method returns `None` and logs; `available()` gates the tool |
| Kodama failure does not kill conversation | separate plugin; `available()` checked before dispatch; `_control` distinguishes unreachable from unsupported |
| OpenAI failure does not kill Kodama | the fast path never touches the model |
| a turn that throws | caught, `State.ERROR`, spoken apology, music restored in `finally` |
| a detector that throws | caught per frame; the thread outlives it |
| degraded startup | `preflight.run` — only the microphone and STT are fatal; everything else is a line on screen |

## 27. Commands

```bash
systemctl --user start|stop|restart aipi5      # the assistant
systemctl --user stop aipi5-ui                 # get out of the full-screen UI
journalctl --user -u aipi5 -f                  # watch a conversation happen
journalctl --user -u aipi5 -n 40 | head -25    # the startup checks

systemctl --user stop aipi5 && .venv/bin/python -m aipi5.main   # by hand
AIPI5_NO_LLM=1 .venv/bin/python -m aipi5.main                   # as plain AIA
AIA_NO_WAKE=1 AIA_DEBUG=1 .venv/bin/python -m aipi5.main        # no wake word

./scripts/check_hardware.sh                    # verify the Pi
python -m unittest discover -s tests -t .      # 184 tests, anywhere
curl -s localhost:8092/api/system | python -m json.tool   # live settings
ssh -L 8092:127.0.0.1:8092 fuwenxu@aipi5.local           # the screen, remotely
```

## 27a. Remote video call — the Call button, and phase 1 on the device

The Call button and its page exist. The call does not. What follows is the
button, and then the hardware measurements the specification requires before
any WebRTC pipeline is designed — taken on the actual Pi, not assumed.

**The button.** `call` is a sixth entry in `ACTIONS`, a sixth button on the
main page between Talk and Camera, and a sixth in-page view. It carries the
same ten-second server-side cooldown as the other page buttons and is
deliberately not on `UNTHROTTLED` with `wake`: starting a call will claim the
camera, the microphone and the speaker away from the voice loop, which makes a
repeated press the most expensive one on the screen rather than the least. Six
buttons share the row at 191 px each on the 1280 px panel; no label overflows.

`handle_button` returns on `call` before the state machine moves and before
anything takes the audio floor — there is no spoken half of this page. The
action still travels through the queue rather than being navigation the page
does on its own, because the handoff that suspends the voice loop's ownership
of the Brio and the microphone has to happen on the Python side, and this is
where it will attach.

Two behaviours on the page are already real, because both are failures that
present silently. Leaving the page stops every track either `<video>` element
holds — clearing `srcObject` alone detaches the stream and leaves the device
claimed, and the next thing to want the Brio is the person detector, which
fails quietly and takes the screensaver with it. And an active call suppresses
the screensaver: presence is the camera's opinion of the room the *Pi* is in,
and a caller who steps out of the Brio's view for ten seconds must not come
back to a clock drawn over the person they are talking to.

### Phase 1 — Brio 101 video, measured

`v4l2-ctl` on `aipi5.local`, kernel 6.18.39, uvcvideo. USB ID `046d:094d`,
serial `2501APQAUK08`.

| node | device caps | use |
|---|---|---|
| `/dev/video0` | Video Capture, Streaming | the capture interface |
| `/dev/video1` | **Metadata Capture**, Streaming | not an image source |

This is the concrete form of the warning already in section 14: the Brio claims
two nodes and the second one is metadata. It opens and it never yields a frame.

**Stable identity:** `/dev/v4l/by-id/usb-046d_Brio_101_2501APQAUK08-video-index0`,
which is a symlink to `video0` built from vendor, product and serial and is
therefore stable across reboot and reconnection. `/dev/v4l/by-path/platform-
xhci-hcd.0-usb-0:2:1.0-video-index0` is the port-stable alternative — it
survives swapping the camera but not moving it to another socket. PipeWire
exposes the same device as node `v4l2_input.platform-xhci-hcd.0-usb-0_2_1.0`,
which is what `getUserMedia` in Chromium will select against.

**Formats: `YUYV` and `MJPG`. There is no H.264.** The Brio 101 does not expose
an encoded stream, so nothing on the camera can be offloaded to and the Pi
encodes.

**The measurement that decides the pipeline:**

| format | 1280×720 | 1920×1080 |
|---|---|---|
| `YUYV` | **5 fps, maximum** | 5 fps |
| `MJPG` | **30 / 24 / 20 / 15 / 10 / 7.5 / 5 fps** | 30 fps |

The specification's 1280×720 @ 30 target is reachable **only through MJPEG**.
Uncompressed 720p30 is 1.3 Gbit/s and does not fit in USB 2.0 high speed, which
is what the descriptor above reports the camera negotiating; the driver
advertises YUYV 720p at 5 fps because that is what fits. Any implementation
that opens this camera at 720p without asking for MJPG gets 5 fps and no error.

### Phase 1 — Brio 101 microphone and the speaker, measured

```
card 2: B101 [Brio 101], device 0: USB Audio
  Capture: S16_LE, 1 channel, MONO, rates 16000 / 32000 / 48000
```

**The Brio microphone is the only capture device on this Pi.** `arecord -l`
lists one card and it is the Brio. This settles a question the specification
leaves open: there is no other microphone to switch ownership *from*, so the
call subsystem does not need a switch — it needs arbitration. AIA's capture
stream and the call want the same capsule, and the microphone allows one
reader, which is the same constraint section 19 already handles between AIA and
AIPI5 with `Conflicts=`.

It is **mono**, and its native rates are 16/32/48 kHz. 48 kHz is what WebRTC
wants and it is available, so no resampling is forced on the capture side.

**Stable identity:** `/dev/snd/by-id/usb-046d_Brio_101_2501APQAUK08-02`, and in
PipeWire the node name
`alsa_input.usb-046d_Brio_101_2501APQAUK08-02.mono-fallback` — both carry the
serial. The ALSA card *number* is 2 today and is exactly what must not be
relied on.

**Output** is HDMI: `alsa_output.platform-107c701400.hdmi.hdmi-stereo`, cards 0
and 1 (`vc4hdmi0`, `vc4hdmi1`). There is no USB or analogue sink.

**Echo cancellation is available and does not need building.** The Pi has
`libpipewire-module-echo-cancel.so` with `libspa-aec-webrtc.so` — the WebRTC
AEC implementation — already installed alongside `libspa-aec-null.so`. The
speaker and the Brio capsule are inches apart on one panel, so this is the
piece the audio-quality requirement stands on.

**Two things found while looking, both worth acting on independently:**

* `wpctl status` reports the HDMI sink at **`vol: 0.15`**. That is the failure
  the README warns about under "Installing" and it is currently live on the
  device: ALSA at full scale, the sink at 15%, and an assistant that sounds
  broken rather than quiet. `wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0`.
* `/dev/video0` is held right now by pid 1516, the running `aipi5` service.
  This is not a fault — it is section 14's single shared handle working as
  designed — but it is the concrete reason a call cannot simply call
  `getUserMedia` and expect the camera. Chromium and the Python process are
  separate readers of a device that allows one.

### What phase 1 changes about the plan

* **MJPEG, 1280×720, 30 fps** is the capture configuration. Not a preference —
  the only one that reaches the target.
* **No camera-side H.264.** Encoding is the Pi's job, and whether that is
  Chromium's own VP8/H.264 or something upstream of it is the next thing to
  measure rather than assume.
* **Microphone ownership is arbitration, not switching.** There is one capsule.
* **AEC is a PipeWire configuration**, not code to write.
* **The Brio handle must be released by the Python side before the browser can
  have it.** That handoff is the real integration work, and it is what the
  `call` action exists to carry.

## 27b. Phase 2 — the call, and the two things that stopped it

Phase 2 is built and running on the device. What follows is the architecture,
then the two failures that took the longest to find, because both were silent.

### Both ends are browsers, and Python never touches the media

    phone (Safari)                              Pi (the kiosk Chromium)
      |  https://<pi>:8443   TLS + bearer token   |  http://127.0.0.1:8092
      |     aipi5/call/server.py                  |     aipi5/ui/server.py
      |                  \                       /
      |                   `--- SignalingHub ---'
      `============= WebRTC media, peer to peer ==='

Chromium already has an encoder, a congestion controller that lowers the
bitrate instead of freezing, and an acoustic echo canceller that works because
one process owns both the capture and the playback stream. A Python peer on
aiortc would have had none of the three, and the third is the whole of the
audio-quality requirement — the Brio capsule is inches from the speaker the
caller comes out of. So Python does signalling, authentication and hardware
arbitration, and no media.

**Two doors into one hub, because of secure contexts.** `getUserMedia` refuses
to run outside one, and `http://` on a LAN address is not one — but
`http://127.0.0.1` is, by definition. So the Pi's own page keeps using the
existing loopback server with no TLS at all, and only the phone needs a
certificate. That preserves the boundary the project already had:
`aipi5/ui/server.py` stays loopback-only and unauthenticated, and the single
listener on the network is `aipi5/call/server.py`, which authenticates every
route before it does anything.

**Signalling is long-poll, not a WebSocket.** All of this project's HTTP is
stdlib `ThreadingHTTPServer`, which has none; a call is about thirty messages,
all in the first two seconds, and a held GET answers each within a millisecond
of it being posted. A test asserts that property, because if it regresses every
call silently gains 25 seconds of handshake.

**Auto-answer is the absence of a prompt, not a rule.** The token is checked at
the door. An unknown caller never reaches a state the screen can see, so there
is no Accept button to skip. Ringing deliberately does *not* own the camera —
`CallState.LIVE` starts at `CONNECTING` — so even an authorised caller has not
turned anything on until the Pi has picked up.

### Measured on the device

| what | result |
|---|---|
| unauthenticated `GET /call/v1/state` | 401 |
| wrong token | 401, and 5 failures locks the address out for 300 s |
| correct token | 200 |
| ring → screen answers | ~1 s, no touch |
| capture format during a live call | **1280×720 `MJPG` @ 30.000 fps** |
| `/dev/video0` during a call | held by `chromium` |
| Brio microphone during a call | held by `pipewire` |
| TI microphone during a call | still held by `python` — AIA keeps hearing |
| hang up → camera back with Python | ~1 s |
| ring with nobody offering | `connecting` expires at 45 s, camera released |

The format line is Phase 1's prediction confirmed under load: 720p30 on this
camera exists only through MJPEG, and asking Chromium for 30 fps at 1280×720 is
what makes it choose that.

### The failure that took longest: no Camera portal

`getUserMedia` did not fail. It never settled — the promise stayed pending
forever, with Python having released the Brio exactly as designed and Chromium
never taking it. The call sat in `connecting` with the camera belonging to
nobody, and nothing was logged anywhere, because nothing had gone wrong in the
sense any component could detect.

The cause: Chromium prefers to reach cameras through the xdg-desktop-portal
`Camera` interface, and **this Pi has no backend that implements it**.
`/usr/share/xdg-desktop-portal/portals/` holds `wlr.portal` (ScreenCast, not
Camera), `gtk.portal` and `gnome-keyring.portal`; none declares Camera. The
request waits on a portal that will never answer.

`--disable-features=PipeWireCamera,WebRtcPipeWireCamera` in
`scripts/aipi5-ui.sh` sends Chromium to V4L2 directly — the same path the
assistant's own camera code uses — and the camera opened immediately. Note that
Chromium keeps only the *last* `--disable-features` it is given, so this had to
be merged with the existing `TranslateUI` rather than added beside it.

Two changes came out of that hunt and both are worth more than the fix:

* **`getUserMedia` is bounded** (`MEDIA_TIMEOUT_MS`, 10 s) and the reason is
  sent to the server, where it reaches the journal. A kiosk has no keyboard, so
  a failure that lives only in devtools is a failure nobody will ever read. The
  requirement asks that this fail cleanly rather than hang; it now does, and the
  journal line names the device.
* **Camera and microphone are opened separately.** One combined call is the
  usual shape and it is what this did first — but it is also one promise, so a
  microphone that never answers takes the camera down with it and the call fails
  with nothing to say about which half was at fault. Split, a busy microphone
  costs the sound rather than the call, and the caller still sees the room.

### A third: revocation that needed a restart

Found by rotating a token. The old one kept working and the newly issued one
was refused — the file on disk and the dictionary in memory had become two
different answers.

`scripts/pair-phone.sh` is its own process writing the same file, and
`TrustedDevices` read it once at construction. So the running assistant never
saw a phone that had just been paired, and — the half that matters — never saw
one that had just been revoked. A revocation that silently waits for a restart
is not a revocation, and the requirement asks for one in as many words.

`_reload_if_changed` now compares `(mtime_ns, size)` before every
authentication and re-reads when it moves. Size as well as mtime, because a
revoke and a re-pair inside one filesystem timestamp tick would otherwise look
like nothing had happened. The instance's own writes update the stamp, so
recording `last_seen` on a successful call does not make the next request
re-read the file it just wrote; lockouts are held separately from the device
map, so re-pairing a phone does not hand an attacker a fresh set of attempts.

Three tests cover it, and all three were confirmed to fail with the fix
disabled — a regression test that has never been seen red is a regression test
that may be asserting nothing.

Verified on the device afterwards: two revoked tokens answer 401 and the
current one 200, with no restart, and the journal shows the re-read.

### The other silent one: the page is cached in memory

`WebUI.page()` reads `index.html` once and holds it. Restarting the *kiosk*
therefore does not pick up a changed page — the browser reloads and is served
the same bytes from the assistant's memory. Two rounds of debugging were spent
on a fix that was on disk and not in the process. **Changing `index.html` means
restarting `aipi5`, not `aipi5-ui`.**

### Confirmed end to end

**A call from the iPhone to the Pi worked, 2026-08-11**, on the same network,
against the deployment described above: MJPEG 720p30 off the Brio, Chromium at
both ends, long-poll signalling, token authentication, auto-answer with no
touch on the panel. That closes phase 2 — the media leg was the one part the Pi
could not prove on its own.

What that test did *not* measure, and phase 2 of the procedure asks for:

* **Echo cancellation under real conditions.** The mechanism is there — the
  requested constraints, and Chromium owning capture and playback in one
  process — but "the remote caller does not hear a delayed copy of their own
  voice" is a judgement made in the room, with the speaker at a normal volume,
  and it has not been made.
* **Latency**, as a number. Nothing here timestamps the media path.

Both want the room rather than the journal, and both are worth doing before
phase 3 adds a relay that can only make them worse.

Phases 3–6 are untouched: TURN for the cellular path, and the reliability
matrix — different networks, slow links, a dropped connection, a Pi reboot, the
Brio unplugged mid-call, the signalling or TURN server unreachable.

## 27c. Phase 3 — reaching the Pi from the Internet

Everything that does not depend on where infrastructure lives is built. What is
left is a decision about hosting, because a call from a cellular network needs
something with a public address and this device does not have one it can use.

### Two separate problems, and only one of them is NAT traversal

**Signalling** has to reach the Pi before any WebRTC exists. From the Internet
that means either an inbound port on the home router — which the requirement
rules out in as many words — or the Pi holding an outbound connection to a
rendezvous with a public address.

**Media** is the ICE problem. STUN tells each peer what its own address looks
like from outside, which is enough whenever both NATs accept a packet from
somewhere they have just sent one to. TURN relays when they will not, which on
mobile carriers is common: symmetric NAT gives a different external port per
destination, so the address the phone learned from STUN is not the address the
Pi's packets arrive at.

Measured here: the house has a **real public IPv4** (not in 100.64.0.0/10,
so not carrier-grade NAT), and IPv6 is a **ULA only**
(`fd14:…`), so there is no globally routable v6 to fall back on. A public v4
means a forwarded port *would* work; it is excluded by the requirement, not by
the network.

### What is built

* **`aipi5/call/turn.py`** — Coturn's `use-auth-secret` scheme. A username of
  `<expiry>:<name>` and a password of `base64(HMAC-SHA1(secret, username))`,
  computed per call. The shared secret stays on the Pi; what reaches the phone
  expires within the hour.

  This is not ceremony. A fixed TURN password has to be sent to the phone to be
  used, so it lives in local storage on a device somebody can lose, and a
  leaked one is an open relay on somebody else's bill. Eleven tests cover it,
  including one that computes the expected password from the specification
  rather than from our own function — a test that calls the same code twice
  proves only that it is deterministic — and one asserting the secret does not
  appear anywhere in what is sent to a peer.

* **ICE servers are delivered per call**, to both ends, in the ring response
  and the answer response, because the credentials expire. A page holding stale
  ones is a call that fails on the cellular path only.

* **Route reporting.** Both pages read `getStats()` on connect and send the
  selected candidate pair to the journal: `host`, `srflx` or `relay`, with the
  round-trip time. This is the diagnostic phase 3 cannot do without —
  "connected" and "connected *through the relay*" look identical on screen and
  are completely different facts, one of them costing bandwidth on a server
  somebody pays for.

* **Degradation preference and a bitrate cap.** `balanced`, not the default
  `maintain-framerate`, which holds 30 fps and destroys resolution until the
  picture is unrecognisable. This is the requirement's "reduce quality rather
  than repeatedly freeze" in one setting. The cap is on the home connection's
  *upstream*, which is the scarce direction.

* **Automatic ICE restart.** The phone is the caller, so renegotiation is its
  job. A Wi-Fi to cellular handover gives the phone an entirely new address and
  nothing in the old candidate set can reach it; only a restart re-gathers.
  Bounded at 30 s, after which the call ends cleanly and releases the Brio —
  which is what the requirement asks for once recovery has not worked.

* **`scripts/setup-turn.sh`** — configures Coturn on a public host and writes
  the Pi's half. It refuses to be the Pi, and the config denies relaying to
  every private range, because a TURN server will otherwise forward to anything
  on its own network if asked.

### The camera that did not come back

Found while testing the above, and it is the more serious find.

`Camera.reclaim()` ran the instant a call ended, **while the browser still held
`/dev/video0`**. `open()` failed, the borrower flag had already been cleared,
and nothing ever tried again. The assistant then had no camera for the rest of
the session — no person detection, no screensaver, no camera page — from a call
that had ended perfectly normally. The journal said `the call is over: camera
reclaimed`, which was simply false.

Two things were wrong and both are fixed. The first attempt now arms a retry
that the voice loop's idle path drives every two seconds for a minute, then
gives up with one error rather than a warning forever; and the log line reports
what happened instead of what was intended. Measured after the fix: the first
attempt fails, the third succeeds three seconds later, `lent_to` clears and the
camera is running again. Nine tests cover the lend/reclaim cycle, including
that an idle retry opens nothing — it runs on every frame.

### Phase 3, as deployed

Tailscale, chosen over a Cloudflare tunnel or a VPS. The end state:

```
iPhone (any network) ──tailnet──▶ aipi5.<tailnet>.ts.net:443
                                  tailscale serve  (real Let's Encrypt cert)
                                        │ proxies to
                                        ▼
                                  127.0.0.1:8443   aipi5/call/server.py
```

`config/aipi5.yaml` now carries `host: 127.0.0.1`, `tls: false`. Verified with
`ss`: the call server listens on loopback **only**, where it used to be on
`0.0.0.0`. The old LAN address answers nothing. The certificate is a real one
valid to 9 Nov 2026 and Tailscale renews it, so the fingerprint ceremony is
gone — which is worth more than the convenience, because the habit it was
building was clicking through certificate warnings.

Measured after the change: 401 without a token, 200 with one, the previous
token dead, a long poll held for 25.02 s through the proxy, and a full call —
ring, auto-answer, `chromium` holding `/dev/video0` at **1280×720 MJPG**,
`pipewire` holding the Brio microphone, `python` still holding the TI
microphone so AIA never goes deaf — then a clean hang-up with the camera back
in about a second.

### Three bugs the proxy exposed

None of these were caused by Tailscale. All three were already there and only
became visible once a connection-pooling proxy sat in front.

**An unread request body desynchronised the next request.** `/call/v1/ring`
never read its body, and the phone posts `{}` to it. On a kept-alive HTTP/1.1
connection those two bytes stay in the socket and the *following* request is
parsed starting from them: `501 Unsupported method ('{}POST')`. It cost a lost
`bye` — which left a call up with the camera lent — and a failed page load, and
it survived a hundred clean retries in between, because it only bites when the
proxy reuses a connection.

`do_POST` now reads the body before dispatching, so no route can reintroduce
it, and the oversized and unauthorised paths drain it too — refusing without
draining desynchronises just as thoroughly. Five tests cover it on a single
reused connection, and they were confirmed to fail against the old behaviour.
Any test that opens a fresh connection per request is blind to this.

**A call that timed out never gave the hardware back.** `SignalingHub.sweep()`
would expire an abandoned call and return the hub to idle, but nothing told the
assistant: `on_call_change` was only ever called from an HTTP handler. So a
phone that rang and vanished left the camera lent to a browser and the music
paused — permanently, from a call nobody had hung up. Both loop paths now go
through the one reconciler, which is idempotent, so there is no call site left
to forget. Verified by ringing and abandoning: timeout at 45 s, audio restored,
camera reclaimed three seconds later.

**The lockout became global.** Behind the proxy every request arrives from
`127.0.0.1`, so rate-limiting on the peer address would have meant five bad
guesses from any device locking out the phone that is allowed to call — a
defence turned into a denial of service against its own user.
`X-Forwarded-For` is now used, but **only when the connection came from
loopback**; trusting it from a remote peer would let anyone claim a fresh
address per request and never be limited at all. Six tests.

The listen backlog was also raised from the stdlib default of 5 to 64. That was
hardening rather than a diagnosis — it went in before the desync was found, on
the theory that a burst was being dropped, and it is kept because 5 is thin in
front of a pooling proxy.

### Measured over 5G: no relay is needed

A call from the iPhone on cellular, with the Pi behind the home router,
2026-08-11:

```
call route: host/udp -> prflx rtt 34ms              (the Pi)
call route: phone prflx/udp -> host rtt 37ms        (the phone)
tailscale:  active; direct <phone's cellular address>  (the transport underneath)
```

**No `relay` at either layer.** WireGuard punched directly through to the
phone's cellular address rather than falling back to Tailscale's DERP, and
WebRTC then paired the two tailnet addresses peer to peer on top of it. Media
crosses the Internet with nothing in the middle. Round trip is 34–37 ms, which
is comfortably inside conversational range.

`prflx` — peer-reflexive — is the expected shape here and not a fault: the
address the packets arrived from was not in the candidate list exchanged
beforehand, so it was learned during connectivity checks, which is what happens
when a WireGuard interface appears alongside the real ones.

So **Coturn is not needed**, and `stun_servers` / `turn_servers` stay empty.
`scripts/setup-turn.sh` remains for a carrier that turns out less cooperative;
there would be nowhere on this network to run it, since a relay behind the same
router is unreachable for exactly the reason the Pi is.

### The diagnostic that reported nothing

Worth recording because it nearly cost the answer above. The first version of
`reportRoute` looked for a candidate pair the strictest way the specification
allows — `nominated && state === "succeeded"` — and **returned quietly when it
found none**. The first real call over 5G connected, worked, and left no route
line at all.

Two reasons it found nothing: WebKit does not populate `nominated` the way
Chromium does, and the stats lag `connectionState` by a moment, so the first
look is too early. Now it tries the transport's own `selectedCandidatePairId`,
then a nominated succeeded pair, then any succeeded pair; retries four times a
second apart; and **reports unconditionally**, even when the report is "getStats
gave me no candidate pair".

This is the second time in this feature that a silent early return hid a real
answer — the first was `getUserMedia` never settling. The rule worth keeping:
on this device nobody can open devtools, so a diagnostic that says nothing when
it fails is indistinguishable from one that was never called.

### A connected call with no phone behind it

Found while inspecting a live call. A connected call deliberately has no
deadline — a long conversation is not a stuck one — which left exactly one
uncovered failure: a phone that disappears without hanging up. App killed,
handset off, battery flat. The call would stay `connected` forever, the Brio
lent to a browser, and the assistant without a camera until a restart.

The phone holds a long poll continuously, so its silence is a reliable signal.
Three missed polls (75 s) now ends the call and posts `bye` to the Pi's page so
it tears its own side down rather than holding a peer connection to nobody.
Four tests, including that a quiet-but-present phone is not hung up on, and
that the rule does not apply before the call connects — `ringing` and
`connecting` have their own deadlines, and the phone has not started polling
yet.

## 27d. Echo, and why the browser could not fix it

Reported from a real call: the caller heard their own voice come back. That is
the failure the audio-quality requirement is written about — the Brio capsule
sits inches from the speaker the caller comes out of.

### Measuring it instead of guessing

Nobody was at the Pi, and echo is normally judged by ear. But WebRTC exposes
the two numbers that settle it, and they can be read from anywhere:
`track.getSettings()` reports the constraints as *applied*, and the audio
`media-source` stats carry `echoReturnLoss` and `echoReturnLossEnhancement` in
decibels — but only while a canceller is actually running.

Sampled twice during a live call, thirty seconds apart:

```
aec=true ns=true agc=true rate=48000  erl=-30.0  erle=0.2
aec=true ns=true agc=true rate=48000  erl=-29.5  erle=0.2
```

So the browser's canceller **was** enabled and was removing essentially
nothing: 0.2 dB where tens of dB is healthy, and flat over time, so not a
matter of convergence. The negative ERL — the microphone about 30 dB stronger
than the reference being subtracted — is what a canceller looks like when it
cannot align the two signals.

That is structural. A browser has to *estimate* the delay around
`Chromium → PipeWire → HDMI → speaker → air → Brio → PipeWire → Chromium`.
HDMI sinks buffer deeply, and the Brio runs on its own USB clock while the sink
runs on the display clock, so the two drift. AEC3's delay search cannot hold it,
and no constraint fixes that.

### Moving cancellation into the audio graph

PipeWire has no such problem: it *is* the graph, so it knows exactly which
samples went to the sink and when. `libspa-aec-webrtc` was already installed —
see section 27a — and `config/pipewire-echo-cancel.conf` wires it into a
virtual microphone and a virtual speaker that the call opts into by name.

### The mistake, which cost a call to find

The first attempt deliberately left the default sink alone, so that the
assistant's own speech could not be affected — the reasoning being that a
mistake there makes the device silent in a room nobody is in. The call would
opt in on its own: capture from `aipi5_call_mic`, and point the remote audio
element at `aipi5_call_speaker` with `setSinkId`.

The diagnostic then reported `canceller=pipewire` and `erle=0.2` — routing
taken, echo unchanged. What the numbers could not show, and the live graph
could, was this:

```
Chromium:output_FL |-> aipi5_call_speaker:playback_FL              (cancelled)
Chromium:output_FL |-> alsa_output...hdmi.hdmi-stereo:playback_FL  (not)
```

**Chromium opens more than one playback stream.** `setSinkId` moves only the
one attached to that element; the other carries no target and follows the
default sink. So the caller's voice reached the speaker twice — once through
the canceller and once around it — and a canceller can only subtract what it
played. The second path was echo it could never remove.

The caution about the default sink was wrong, and provably so: the canceller
forwards its own playback straight to HDMI, so making it the default cannot
silence anything. Verified afterwards by making the assistant speak and finding
Piper's stream routed through it —
`alsa_playback.python3.13:output_FL -> aipi5_call_speaker`, forwarded to HDMI,
with the spoken line in the journal.

Two lessons worth more than the fix. **`erle` stopped being a useful number the
moment PipeWire went in front of Chromium's canceller** — the browser reports
low enhancement both when it is redundant and when it is failing, so the two
became indistinguishable and the graph is now the check. And a diagnostic that
reports a component is *connected* is not a diagnostic that the component is
*exclusive*; the second output path was invisible to every measurement taken
until somebody looked at the links.

### The canceller sent silence, and was removed

**It is not installed, and `scripts/setup-echo-cancel.sh` now carries a warning
block saying why.** Everything below about the bypass and the default sink was
correct as far as it went; the canceller itself was worse than the problem.

Reported from a real call across networks: the phone could hear nothing from
the Pi. Recording the two sources side by side settled it in eight seconds:

```
raw Brio   rms  -40.8 dBFS  peak  -24.4 dBFS  nonzero 100.0%
cancelled  rms -180.0 dBFS  peak -180.0 dBFS  nonzero   0.0%
```

`aipi5_call_mic` was emitting **pure zeros**. Why was never established, and
deliberately so — the feature was removed rather than debugged, because it was
solving a problem that has never been confirmed to exist (see the next
section), and it had broken one that certainly does.

**Every signal available said the call was healthy.** The microphone opened,
the constraints applied (`aec=true ns=true agc=true`), the track existed, the
route was direct at 11–16 ms, and the diagnostic printed a tidy line about echo
return loss. The only thing that knew was the person on the phone. Two rounds
of verification had been run over this configuration — routing through the
graph, and links in `pw-link` — and both confirmed the canceller was
*connected*. Neither asked whether it was *carrying anything*.

That is the lesson worth keeping from the whole episode: **a component
verified as connected is not a component verified as working**, and on a device
nobody is standing next to, the difference is invisible until somebody calls.

Two things came out of it that stay:

* `reportAudio` now sends `audioLevel` and `totalAudioEnergy`, and writes
  `** THE MICROPHONE IS SENDING SILENCE **` into the journal when the energy is
  zero. A silent microphone is now a log line rather than a phone call.
* `config/wireplumber-reserve-aia-mic.conf` is **kept**. It stops PipeWire
  claiming the capsule AIA opens exclusively, which is what made the assistant
  restart-loop after a reboot, and it has nothing to do with cancellation.

Confirmed working on the raw Brio afterwards, by the person on the other end.

### The echo may not have been the Pi's at all

Worth recording, because it reframes everything above. The echo was heard while
testing **with the phone in the same room as the Pi**, and two devices in one
room are an acoustic loop no canceller can win: the Pi's speaker reaches the
phone's microphone and the phone's speaker reaches the Brio, each carrying the
other's audio back. Neither canceller is wrong; there is simply a path between
them through the air that neither can model.

In use the phone is somewhere else, which is the case that matters and the one
that was never tested for echo. So this is not known to be a defect, and it is
not blocking.

The work stands anyway. The bypass — Chromium reaching the speaker by a second
path the canceller could not see — was measured in the live graph and was real
regardless of what the caller heard.

### Still unmeasured

Whether a caller in a *different* room hears themselves. That is a judgement
made with somebody at the Pi, and cannot be read from here. An attempt to
measure it remotely — playing a loud speech-band stimulus and comparing the raw
Brio against the cancelled source — failed outright: the Brio never registered
the stimulus above its own noise floor (peak −39.6 dBFS against a floor of
−40.9), so the recording proves nothing about cancellation. The 17 dB
difference between the two captures is noise suppression on room tone.

If echo does appear from a different room, the level mismatch is the first
suspect: the Brio captures at 0.88 into a sink sitting at 0.15.

Also unmeasured: latency of the *media* as opposed to the transport. The
34–37 ms in section 27c is the ICE round trip, not glass-to-glass.

### The alternatives, for the record

Phase 3 needs a public address, and every way of getting one is a choice about
money, accounts and where things live that cannot be made from here:

| route | signalling | media | cost |
|---|---|---|---|
| Cloudflare Tunnel | outbound from the Pi, real certificate, any browser | still needs STUN/TURN | free; a named tunnel wants a domain |
| Tailscale | tailnet address, real certificate | the tailnet carries it; TURN likely unnecessary | free; needs the app on the phone |
| VPS | reverse proxy or rendezvous | Coturn on the same host | ~$5/month, wants a domain |

Nothing is installed on the Pi today — no `cloudflared`, `tailscale`,
`coturn`, `wg` or `zerotier`. Whichever is chosen, publishing this service is
an outward-facing change to a device with a camera in a room somebody lives in,
so it is not one to make on somebody's behalf.

## 27e. Phase 6 — reliability, and two boot failures

### Audio priority, verified with music actually playing

Phase 5 asks that a call pause Kodama and resume it afterwards. That had been
implemented and never exercised. With a track playing:

```
before call : Playing at 19.705
during call : Paused  at 21.456
still during: Paused  at 21.456     <- position frozen
after call  : Playing at 29.386     <- resumed from where it stopped
```

The position does not advance during the call, which is the distinction this
project insists on: **paused over MPRIS, not muted.** A muted song keeps
playing and loses the seconds it was silent for.

### The assistant did not start after a reboot

Rebooting had never been tested — section 28 still listed it as outstanding.
It fails, and in the worst possible shape: both units sat `inactive (dead)`
while `is-enabled` reported `enabled`. Nothing failed, nothing retried, and
every status command said the system was fine.

```
default.target: Found ordering cycle on aipi5-ui.service/start
default.target: Found dependency on aipi5.service/start
default.target: Found dependency on kodama-lite.service/start
default.target: Found dependency on default.target/start
default.target: Job aipi5.service/start deleted to break ordering cycle
```

`kodama-lite.service` is `After=default.target` *and* `WantedBy=default.target`.
Our `After=kodama-lite.service` closed the loop, and systemd breaks a cycle by
deleting jobs from it — the job it chose was ours.

The ordering was insurance against a race the code already handles:
`KodamaLauncher` waits for the player to reach the bus, and AIPI5 starts Kodama
on request rather than depending on it. Removed. See `systemd/aipi5.service`.

### Fixing that exposed a second one, which this work had caused

With the cycle gone the assistant started — and then restarted **nine times in
two minutes**, never once listening:

```
cannot open the microphone: the microphone matching 'USB PnP Sound Device'
exists but could not be opened — it is almost certainly already in use.
```

PipeWire was holding it. Specifically, the echo canceller was:

```
echo-cancel-capture:input_MONO
  |<- alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00...
```

`capture.props.node.target` names the Brio, but at boot the USB camera has not
enumerated yet, so the target does not resolve — and **a PipeWire stream whose
target is missing does not fail, it links to the default source instead.** That
was AIA's microphone. The canceller took it, held it for the session, and
cancelled echo out of the wrong capsule while the assistant died in a loop.

Two fixes, and the second matters more than the first:

* `node.dont-reconnect = true` on both ends of the canceller, so a target that
  is not there yet means *no link* rather than the wrong link.
* `config/wireplumber-reserve-aia-mic.conf` disables the TI device in PipeWire
  entirely. AIA opens that capsule directly through ALSA and an ALSA capture
  device allows one reader; PipeWire managing it at all was a race waiting for
  a slow boot, canceller or no canceller. Nothing on this device wants it
  through PipeWire.

### After the fixes, measured on a cold boot with nothing started by hand

| check | result |
|---|---|
| services | `active active`, **0 restarts** |
| ordering cycle errors | 0 |
| TI microphone | held by `python` — the assistant is listening |
| echo canceller | capturing from the **Brio**, correctly |
| camera | running, Brio 101 |
| call server | listening, reachable at the tailnet URL |
| default sink | still the canceller (persisted) |
| tailnet + serve | up, certificate valid |
| unauthenticated request | 401 |
| ring → auto-answer | camera taken by `chromium` |
| hang up | camera back with Python in ~3 s |

### The Brio unplugged, and the assistant that did not notice

Simulated by unbinding the USB device rather than by pulling the cable, which
is the same thing as far as the kernel is concerned. Two findings, one of them
the more serious kind: a status that lies.

**Unplugged, the assistant kept reporting `running=True`.** It survived — zero
restarts, still answering — but `available()` returns `_started`, and nothing
cleared it. The settings page said the camera was fine, on `/dev/video0`, while
that node did not exist. Every symptom downstream is silence: no person
detection, no screensaver, a black camera page, and "what do you see" answering
without looking.

**Replugged, it did not come back.** The by-name search that makes `device:
auto` work only runs inside `open()`, and after startup nothing calls it again.

And the search is exactly what a replug needs, because **the node number
moves**. Measured across two unbind/rebind cycles: `/dev/video0` →
`/dev/video1` → `/dev/video0`. Numbers are handed out in order of arrival, so a
reopen that assumed the old path would fail with a working camera sitting in
front of it.

The first test of this appeared to pass, and did so for the wrong reason: it
included a call, and the call's lend/reclaim happened to run `open()` again.
Re-run without a call, it failed. A test that exercises the fix by accident is
a test that will not notice when the fix goes away.

Fixed: a failed read marks the camera lost — closing the handle and clearing
`_started`, so `available()` stops lying — and arms a reopen that the voice
loop's idle path drives. **Unbounded, unlike the reclaim after a call**: a
borrowed camera comes back in seconds or something is wrong, but an unplugged
one comes back when somebody plugs it in, which may be tomorrow. What is
bounded is the noise — every two seconds for the first minute, then every
thirty.

Measured after the fix, with no call and no restart anywhere in the test:

```
before:  running=True   lost=False  device=/dev/video1   frame 456127 bytes
unplug:  running=False  lost=True
replug:  running=True   lost=False  device=/dev/video0   <- different node
20s on:  running=True   lost=False  device=/dev/video0   frame 455029 bytes
```

### The microphone unplugged: recovers, but takes two and a half minutes

Nearly reported as a defect and is not one. Unplugged, the assistant stays up
and logs the failure; replugged, it recovers on its own with **no service
restart**. The first measurement said otherwise only because the observation
window was 45 s and recovery is slower than that:

```
RECOVERED after 150s
microphone recovered after 8 failed attempts
```

That is AIA's own retry, backing off from 1 s to a 30 s ceiling, and it is
tuned where it was measured. Worth knowing rather than changing: a cable
knocked out and pushed back in leaves the assistant deaf for about two and a
half minutes, silently, while every status command reports it healthy.

One consequence found while reading that code: `frames()` blocks inside the
generator during an outage, so the voice loop is parked for the duration — call
sweeps and camera retries do not run either. Nothing depends on them during a
microphone outage today, but a future timeout that does would not fire.

### An unreachable TURN relay does not break calls

Configured a relay at a hostname that does not resolve, with a missing secret
file, and rang. The ICE list was still offered, the call still reached
`connecting`, and Chromium still took the camera — the peers pair on host
candidates and the dead relay is simply never used. The credential-less entry
is offered with a warning rather than dropped, which is the designed
degradation.

### A degraded link, and a link cut in half

Shaped with `netem` on **`tailscale0` rather than `wlan0`**, deliberately: the
call rides the tailnet while ssh to the box goes over the LAN, so the test
cannot cut off the person running it — which is the usual way a network test on
a remote machine ends. Every mode auto-clears and a watchdog strips anything
left behind.

**Slow and lossy — 1.5 Mbit, 120 ms ± 30 ms, 2% loss, 91 s.** Connected
throughout, no reconnection events at all. The picture degrades and keeps
moving, which is the requirement's "reduce quality rather than repeatedly
freeze or disconnect".

**Nearly unusable — 300 kbit, 250 ms ± 60 ms, 8% loss.** Held for ~50 s,
recovering once by itself (`reconnecting` → `connected` in one second) until
the person deliberately hung up. Round trip reached 6 s, though part of that
was the test rig rather than the link: netem's default 1000-packet queue
bufferbloats badly at 300 kbit. The later profile uses `limit 60`.

**Link cut entirely for 20 s.** This is the recovery path that had been written
and had never once fired:

```
22:49:05  call: reconnecting
22:49:18  call: connected                    (13 s later)
          route: host/udp -> prflx rtt 44ms
```

The session id is unchanged across it, so the existing call was recovered
rather than replaced, and it re-paired directly with normal latency afterwards.

One instrumentation gap this exposed: `/call/v1/bye` logged every ending as
"the phone hung up", because the phone posted only the session. A call the
recovery timeout gave up on and a call somebody deliberately ended were
therefore indistinguishable — precisely the distinction wanted when reading
back a call that dropped on a bad link. The reason now travels with the
hang-up.

### The End Call button, and the speaker

**End Call** had only ever been exercised through the API, never as a click.
Tested in a browser against a real `MediaStream`, because the guarantee that
matters is not that it navigates away but that it releases the camera:

```
track_before: "live"   track_after: "ended"   released: true
callLocal_cleared: true  remote_cleared: true  self_cleared: true
page: "main"   bye posted: {"session":"…","reason":"the Pi hung up"}
```

**Speaker unavailable** is the one item covered only partly. Muting the sink
and making the assistant speak leaves it running — zero restarts, still
synthesising — but muting is not the device disappearing, and removing the sink
outright was not worth the risk on a machine nobody could hear.

### What is still untested

Only two things, and both need somebody in the room: whether a caller in a
*different* room hears an echo, and the speaker genuinely removed rather than
muted. Everything else in the procedure's phase 6 list has been exercised.

The Brio being unavailable *during* an incoming call is covered, though it was
verified by accident rather than design — see the portal failure in section
27b, where the call ended cleanly with `the Brio could not be opened` and the
camera was reclaimed.

## 28. Instructions for future development

**Do this first, in this order.** These are the procedure's phases 2–11, which
this work has not reached.

1. **Correct the model name.** One line in `config/aipi5.yaml`. Until then the
   startup probe fails and conversation is unavailable.
2. **Phase 2 — hardware.** `./scripts/check_hardware.sh` on the Pi. Read all of
   it; it reports the things that fail silently later.
3. **Phase 3 — reproduce the voice loop.** `AIPI5_NO_LLM=1 python -m aipi5.main`
   makes this behave exactly like AIA. Verify English, Mandarin and Cantonese
   and every existing Kodama command before adding anything.
4. **Phase 5 — the model.** Drop `AIPI5_NO_LLM`. Watch `probe()` in the journal.
5. **Phase 6 — tools, one at a time.** Weather, news, time, camera. Each is a
   plain object: exercise it from a REPL against the real network before
   speaking to it.
6. **Phase 9 — person detection.** Confirm `_best_person` reads the real Hailo
   output; log the raw shape once if it does not. Then tune
   `frames_to_appear` / `frames_to_disappear` / `interval_ms` on the actual Pi
   with a person walking in and out.
7. **Phase 10 — the screensaver.** The logic is tested; what needs the device is
   the timeout suiting the room.
8. **Phase 11 — boot.** Cold boot, reboot, service restart, and a boot with the
   network down.

**Then measure**, and put the numbers in this file. Section 34's list —
`wake_detection_ms` through `total_ms` — is already what `Turn.mark()` records.

**Rules that should hold for anything added later:**

* Keep the AIA checkout unmodified. If a change to the voice path is needed,
  make it in AIA where the measurements are.
* Anything the model may call goes through `ToolBox` and nowhere else. The
  filter on `CommandSpec.confirm` must stay a filter and never become a name
  list.
* New logic that can be a pure function should be, and should be tested here.
  The parts of this project that are hardest to debug on the device are exactly
  the parts that were made testable off it.
* Do not add automatic fallbacks between detection backends. A silent
  degradation is worse than a reported failure.
