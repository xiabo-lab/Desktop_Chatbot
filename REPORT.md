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
`aia:news`, `aia:music`) and `/api/feed?roles=user,aia` filters them out of the
Talk page. They stay in the 24-hour transcript because they were audible in the
room and that record should not lie; they are kept out of the conversation
because a conversation is a conversation.

**The camera is the exception, because it stopped being a page.** There is no
Camera button on the home screen any more: "What do you see?" sits on the Talk
page beside Listen, and the answer arrives there as conversation — a bubble
holding the photograph, and the spoken reply underneath it. So the camera's
line is recorded as `aia` like any other answer, and passes the filter above
rather than being caught by it.

The picture itself is not in the transcript. `UiState.describe_camera` publishes
`camera_image` — `Capture.taken_at`, a cache-busting token — alongside the
description and its id, and the page draws the bubble live from
`/api/camera/capture`, which serves whatever the camera's `last_capture` is and
takes no name from the browser. A page opened later sees the sentence without
the picture, which is the honest rendering of a still that `Camera._prune` has
since deleted from tmpfs. `camera_image` is absent when the model answered
without taking a picture, and that case draws no bubble: the previous
photograph under the current answer would be a picture of a different moment.
`#camera` still opens the live preview for anybody reaching the device over
`ssh -L`; nothing navigates there.

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

---

## 29. AI Motion games — Fruit Ninja on the AI HAT+ 2

Built 2026-08-14 on branch `feature/ai-motion-games`. Three commits: the pose
service, the game, the tests.

The goal was never one game. It was a camera-and-accelerator layer that Yoga,
Boxing, Workout and Dance can reuse, with Fruit Ninja as its first consumer —
so `aipi5/motion/` knows nothing about fruit and `aipi5/games/` knows nothing
about accelerators.

```
Brio -> CameraLease -> HailoPose -> PoseFrame -> PlayerSelector -> HandFilter
                                        |                             |
                                   (17 joints)                   (two wrists)
                                        |                             |
                              future Yoga / Boxing              Fruit Ninja
```

### 29.1 Environment

| | |
|---|---|
| OS | Debian GNU/Linux 13 (trixie) |
| Kernel | 6.18.39+rpt-rpi-v8 aarch64 |
| Accelerator | **HAILO10H** at PCIe `0001:01:00.0` |
| HailoRT | 5.1.1 (`h10-hailort`, `python3-h10-hailort`) |
| Firmware | 5.1.1 (release, app) |
| Pose model | `/usr/share/hailo-models/yolov8s_pose_h10.hef` (ships with `hailo-models`; no download) |
| Camera | Logitech Brio 101, `usb-046d_Brio_101_2501APQAUK08-video-index0` |
| Capture | 640x360 MJPG @ 30 fps, 2 V4L2 buffers |

### 29.2 The three things that shaped the design

**A Hailo-10H permits exactly one `VDevice` — not one per process, one.**
Measured both ways:

| | |
|---|---|
| second `VDevice`, second process | `HAILO_OUT_OF_PHYSICAL_DEVICES` (74) |
| second `VDevice`, **same** process | the same error |
| one `VDevice`, two `InferModel`s | person 28 ms, pose 32 ms, both fine |

`HailoSchedulingAlgorithm.ROUND_ROBIN` reads like it makes the accelerator
shareable and does not: it timeshares *models configured on one virtual
device*. So `aipi5/core/accelerator.py` owns the single device and refcounts
it, and `vision/person_detection.py` was changed to take it from there instead
of creating its own. That is the only change to existing behaviour in this
work, and it is what lets a pose model load at all while presence detection is
running.

**The pose HEF does not post-process on chip**, unlike the detection HEF next
door which returns a finished NMS buffer. It returns nine raw tensors — three
scales, each with 64 channels of DFL box distribution, 1 of person score, 51 of
keypoints. `aipi5/motion/yolov8_pose.py` decodes them. Two things in that
decode are easy to get subtly wrong: anchor centres carry a **half-cell
offset** (`(index + 0.5) * stride`), and **the class score is already a
probability** because the sigmoid is folded into the compiled graph — applying
another one does not look like a bug (every detection survives and ranks the
same) but silently voids the threshold. The keypoint *visibility* column does
still need one. Hailo's own reference (`/usr/include/hailo/tappas/
pose_estimation/yolov8pose_postprocess.cpp`) is LGPL, so it was read for
constants and reimplemented in numpy rather than copied.

**`CAP_PROP_BUFFERSIZE=1` halves the frame rate of a continuous reader.**

| buffers | per read | rate |
|---|---|---|
| 1 | 66.9 ms | 15.0 fps |
| 2 | 33.4 ms | 29.9 fps |
| 3 | 33.4 ms | 30.0 fps |
| 4 | 33.4 ms | 29.9 fps |

With one buffer the driver has nowhere to put the next frame while userspace
holds the only one, so it idles until the buffer is requeued and then waits for
the *following* frame start — one wasted frame period, every frame. Two fixes
it completely and is the shallowest queue that does, which matters because each
extra buffer is another frame of hand-to-screen latency.

This does **not** make `vision/camera.py` wrong. Its single buffer plus
drain-then-block is correct for the person detector, which arrives twice a
second and wants the freshest possible frame. The two readers want opposite
things, which is why `motion/camera_lease.py` opens its own handle after
`Camera.lend()` rather than sharing the assistant's.

It looks exactly like the dim-room auto-exposure halving in section 14, and it
is not: `exposure_dynamic_framerate`, manual exposure at 10 and 20 ms,
resolution and pixel format all made no difference, and **every mode measured
exactly 15.0 fps** — a light-starved sensor gives a ragged number, a buffering
artefact gives exactly half. `v4l2-ctl --stream-mmap` (4 buffers) reached
25.8 fps on the same camera at the same moment, which is what proved the camera
was not the limit.

### 29.3 Performance, measured on the device

| | |
|---|---|
| Camera | **30.0 fps** at 640x360 |
| Hailo inference | **29.1 ms** mean (median 29.9, p95 30.2) |
| Pose rate | **29-30 fps** sustained |
| Capture to decoded pose | **44 ms** mean (median 51, p95 60 over a longer run) |
| Game render | browser `requestAnimationFrame`, 60 fps target |
| Assistant CPU during a game | **~45% of one core** of 400% available; system 89% idle |
| Assistant RSS during a game | 866 MB |
| Frames dropped | ~8% (16 of 204), by design — newest frame wins |

640x360 is not a compromise: it is exactly what a 16:9 frame is scaled to when
letterboxed into the model's 640x640 square, so capturing at it removes the
downscale entirely rather than merely making it cheaper.

The pose loop, the camera thread and the browser's render loop all run at
independent rates. Fruit are extrapolated on the page from the position and
velocity in the last snapshot using the same closed form the Pi integrates, so
the drawn position and the authoritative one agree rather than drifting.

### 29.4 What was verified on the device

* `hailortcli fw-control identify` gives HAILO10H, firmware 5.1.1 — **pass**
* Pose decode against a known image (`bus.jpg`): 4 people, keypoints at the
  right places, letterbox and mirror both correct against ground truth — **pass**
* Full chain with real inference — player selected from 4 people, readiness
  `ready: True`, both wrists live at 0.97/0.99 confidence, mapped to screen
  pixels, slash through a fruit scoring 10 — **pass**
* A live round: fruit spawn, arc, fall, lives 3-2-0, GAME OVER, simulation
  stops — **pass**
* Ten open/close cycles: camera returned every time, accelerator refcount
  2 to 1 every time, threads stable at 22, no fd growth — **pass**
* **Camera unplugged mid-game** (USB unbind): game **paused** with "the camera
  stopped delivering frames", assistant stayed alive; on replug the node moved
  `/dev/video0` to `/dev/video1` and was found by name; Retry reopened the game
  on the new node at 30.2 fps — **pass**. This is the strongest evidence that
  nothing hard-codes `/dev/video0`.
* Audio floor balance across three cycles from a clean restart: 0, 1, 0 — **pass**
* Kodama already paused before a game stayed paused after it — **pass** (this
  is section 38's actual requirement)
* AI Assistant camera function after all the game cycles: captured, described
  aloud, returned to idle — **pass**
* 664 automated tests, on Windows and on the Pi — **pass**

### 29.5 Integration

| | |
|---|---|
| Home | PASS — Game button added, native to the existing row |
| Games page | PASS |
| Fruit Ninja | PASS (mechanically; see 29.6) |
| Camera page | PASS — preview streams after game exit |
| AI Assistant camera | PASS |
| Person detection | PASS — recovered on the `hailo` backend after every game |
| Kodama | PASS |
| Weather | PASS |
| News | PASS |
| Files | PASS |
| Photos / slideshow | PASS |
| Screensaver | PASS — held off during a game, released after |
| Night mode | PASS (schedule intact; not re-tested at 21:01) |
| Video calling | PASS — still listening on Tailscale, iPhone still paired |

### 29.6 Not verified

Stated plainly, because these are the parts that matter most and the parts a
green tick would be worth least on:

1. **Nobody has played it.** The Brio is pointed at a laundry room — the vision
   tool describes "a large wall-mounted Speed Queen commercial laundry
   machine" — so no human has ever been in front of it during this work. The
   pose path is proven with a real photograph through the real accelerator, and
   the slash path is proven against real decoded wrists, but *a person moving
   their hands and seeing a blade follow* has not happened.
2. **Touch has not been tested on the panel.** The UI was driven through an
   `ssh -L` tunnel. Every control was exercised, but with a mouse, not a finger.
3. **Music playing, ducked, restored** is only half tested. The "already
   paused stays paused" half is verified live; the other half rides on
   `AudioPriority`, which is the same path every spoken turn already uses.
4. **Hand-to-screen latency is measured to the pose, not to the glass.** 44 ms
   capture to decoded pose is real. The rest — SSE hop over loopback, one
   `requestAnimationFrame` — is perhaps 20-30 ms more by construction, but it
   has not been instrumented end to end.

### 29.7 Gotchas worth keeping

* **`systemctl --user stop aipi5` also stops the kiosk, and `start` does not
  bring it back.** `aipi5-ui.service` is `PartOf=aipi5.service`, which
  propagates stop and restart but not start. Stopping the assistant to run
  `scripts/probe_pose.py` left the display dark for 80 minutes during this
  work before it was noticed. Use `restart`, or remember to
  `systemctl --user start aipi5-ui` afterwards.
* **`Device.scan()`, not `VDevice.scan()`.** `VDevice` has no `scan` in
  HailoRT 5.x, and reaching for it gives an `AttributeError` that reads exactly
  like a missing accelerator.
* **The start screen cannot use `/api/camera/stream`.** That reads `Camera`,
  which is *lent* for as long as a game is open and correctly answers 503 while
  it is. `/api/game/preview` streams from the game's own capture instead. The
  first version got this wrong and showed a broken image on the one screen
  whose entire job is proving the camera can see you.
* **`_last_tick = 0.0` is a real time.** Truth-testing it made the first tick of
  a session whose clock starts at zero silently do nothing — invisible against
  `time.monotonic()`, immediately fatal in a test.

### 29.8 Licensing

The gameplay owes its shape to the MIT-licensed community project in
`hailo-ai/hailo-rpi5-examples` — the five fruit, ten points each, the launch
speeds as a starting point. The implementation is not taken from it: that one
ties physics to the frame rate and slices by proximity to a single wrist
sample, and both are things this needed to do better.

**No artwork or audio is shipped.** Fruit are drawn as canvas paths with an
emoji garnish on top; every sound is an oscillator and a noise buffer. Nothing
from the commercial game of a similar name is used or imitated. The UI says
"Fruit Ninja"; the code calls it Fruit Slice.

That survived the ten-fruit upgrade in 29.12, which declined several
recommended CC0 asset packs rather than build a static-file route to serve
them. `ASSET_LICENSES.md` is the full record — what is generated, what is
borrowed as an idea rather than a file, and the empty table any future bundled
asset has to be entered into.

### 29.9 What the next game needs

Nothing in `aipi5/motion/`. `PoseFrame` already carries all seventeen COCO
joints with confidences, normalised to the camera and already mirrored, and
`PoseService` already calls a consumer once per frame. A Yoga coach is a new
directory beside `fruit_ninja/`, a `readiness`-style check, and joint-angle
maths over keypoints that are already there — plus one entry in `CATALOGUE`
with `playable: True`.

### 29.10 Starting by voice

Added 2026-08-14, after the first play attempt made the problem obvious: **the
touchscreen is the wrong input for this game.** The player has to stand far
enough back for the camera to see their whole upper body, which is well out of
arm's reach of a panel on a wall, so START was a button only somebody in the
wrong place could press — and pressing it meant walking backwards into shot
before the first fruit arrived.

`aipi5/games/voice.py` adds one command, `start_game`, registered alongside
AIA's own plugins in `main.py`. It differs from the button in exactly two ways,
both because nobody is standing at the screen:

* **The game is opened if it is not already.** A press of START implies the
  start screen is showing; a voice can arrive with the assistant on any page.
* **The player-detected check waits rather than refusing.**
  `GameManager.voice_start` blocks for up to four seconds. Somebody who has
  just finished saying "start game" is by definition in front of the camera,
  but the pose service may have been running for a fraction of a second —
  opening a game costs about 1.5 s of camera and HEF, and the model then needs
  a frame or two. Answering "I cannot see you" to somebody standing in plain
  sight is the one failure this command must not have.

Five outcomes, five different things said out loud: `started`, `resumed`,
`already` (a round in progress is never restarted — that would throw the score
away on an ambiguous phrase), `no-player`, `unavailable`.

**Which phrases are safe is a measurement, not a preference**, because the
router compares by sound. Measured against every phrase AIA already routes,
using the router's own `similarity`, against the 0.78 a whole-utterance match
needs. Three candidates were dropped:

| candidate | worst neighbour | score | |
|---|---|---|---|
| `start the game` | reboot[`restart the pi`] | **0.71** | dropped |
| `play game` | toggle[`play pause`] | **0.74** | dropped |
| `开始` alone | resume[`开始播放`] | **0.67** | dropped |

The first is worth remembering: "start the game" is the most natural English
phrasing there is, and it sounds enough like "restart the pi" to be a coin
toss. `confirm=True` on reboot would have caught it, but a game that sometimes
asks "shall I restart the Raspberry Pi?" is not a game anybody trusts.

What was kept, with the worst neighbour of each under 0.70:

    en   start game · begin game · lets play · let's play
         start fruit ninja · start the fruit game
    zh   开始游戏 · 开始游戏吧 · 玩游戏 · 游戏开始 · 开始水果忍者

`玩游戏` scores 0.00 against everything — there is simply no Kodama command
about games, which is what leaves this much room. `tests/test_routing.py` now
builds the registry the assistant actually builds, so a fourth plugin cannot
pass that suite while the device fails it.

**A voice start has to bring the screen with it**, since the person who started
it is too far away to navigate. `/api/state` carries a two-field `game` object
and the page adopts a game it did not open — the same thing `callFromState`
already does for a call in progress. Guarded against the poll landing inside
the second `openGame` spends waiting for the camera, which would otherwise
throw the player back to the library a moment before their game started.

Verified on the device: all eight English and five Mandarin phrases route to
`start_game` against the Pi's own (older) AIA checkout; `restart the pi`,
`play pause`, `开始播放`, `播放音乐` and `下一首` all still route to what they
did before; `/api/state` tracks `ready` → `playing` → gone; and a game opened
server-side moved the page from Home to the game page with no local call,
while a server-side close returned it to the library. 703 tests pass.

**Still not verified: the spoken utterance itself.** Nobody has said "开始游戏"
to this device — the routing is proven against the real router and the handler
against the real manager, but no audio has gone through the wake word and STT
into this command.

One measurement worth recording from the same session: with the room dark, the
Brio opened its exposure to 66 ms (`exposure_time_absolute: 666`) and the whole
pipeline dropped to **15 fps** — camera 15.0, inference 15.0, while the
accelerator still took only 31.5 ms per frame. That is the dim-room behaviour
of section 14, not the buffer fault of section 29.2: the earlier 15 fps came
with a 25 ms exposure that could have carried 40. Playable, but the 30 fps in
section 29.3 is a daylight number.

One thing that looks like a leak and is not: opening a game from the shell
makes the **kiosk** adopt it, and the kiosk's event stream calls
`status(seen=True)` about thirty times a second, which keeps `_last_seen` fresh
— so the 25 s idle watchdog never fires and the Brio stays lent indefinitely.
That is correct behaviour (a game visibly on screen must not have its camera
taken away) and it is also the clearest proof the navigation works: verified on
the panel with `grim`, which showed the start screen, the live preview and a
correctly disabled START, none of which anything local had asked for. `POST
/api/game/close` when finished testing.

### 29.11 A timed round, a comet blade, and sound that actually plays

Six changes on 2026-08-14, after the first real play session. One of them turned
out to be a bug rather than a feature.

**The game sounds had never played — not once.** Chromium's default autoplay
policy requires a user gesture before an `AudioContext` may produce sound, and
a kiosk page opened by systemd has never had one. `new AudioContext()` starts
`suspended` and `resume()` is refused, so every slice, bomb and game-over tone
was being synthesised and discarded in silence. Measured in a browser without
the flag: `suspended` before a click, `running` immediately after one. It is
worse for a game started by voice, which by design involves nobody touching
anything at all. Fixed with `--autoplay-policy=no-user-gesture-required` in
`scripts/aipi5-ui.sh`, which is the right setting for this machine rather than
a workaround — the policy exists to stop pages a person did not ask for from
making noise, and this is the single local page that *is* the device.

The slice is no longer a beep. A band-passed noise burst swept 5.2 kHz → 900 Hz
for the cut, and a pitched body under it that climbs with the streak, so a good
run audibly goes somewhere. Verified by counting the nodes each sound builds:
slice = 1 buffer + 1 oscillator, bomb = 1 buffer + 2 oscillators, game over =
3 oscillators.

**Three lives became a sixty-second clock, and that changes what the game
rewards.** With lives, an awkward fruit was better ignored than attempted — a
failed swing ended the game a third sooner. On a fixed minute, doing nothing
costs exactly what trying and missing costs, so there is never a reason not to
swing. It also makes every round the same length, which is what makes two
scores comparable. A sliced bomb takes **5 s** off the clock rather than a life;
a dropped fruit costs only its points and the streak; a bomb that reaches the
floor still costs nothing.

The clock runs off the same clamped `dt` as the physics, not off
`now - started_at`, so a pause is never charged and a stalled frame cannot take
ten seconds at once.

**Difficulty now ramps on seconds elapsed, not on score.** The old reasoning —
score means "this person is doing well", time punishes somebody struggling —
belonged to a game with lives. In a fixed minute it is wrong in both
directions: ramping on score gives the best player the busiest screen, leaves a
beginner throwing one fruit at a time for a whole minute, and makes the high
score measure the ramp as much as the player. Now: interval 1.15 s → 0.38 s
over 45 s, pairs from 15 s, triples from 35 s, and nothing new thrown in the
last 1.4 s because a fruit launched at the whistle cannot be reached. Measured
over a whole round, the last third throws more than twice what the first does.

**The hands are 3x with a comet tail.** At a metre and a half a 9 px dot and a
one-frame line are genuinely hard to see, and a blade you cannot see is a blade
you cannot aim. Glow 34 → 46 px, core 9 → 15 px, plus a tapered 260 ms trail
drawn as a run of segments — a single stroked polyline cannot narrow along its
length — with `globalCompositeOperation = "lighter"` so overlaps at the head
glow rather than just stacking opacity. The tail is the browser's, not the Pi's:
it is decoration, collision has already been decided against the segment in the
snapshot, and sixty points per hand per frame is not worth serialising for
something the page already knows.

**Wake word acknowledgement.** `aipi5/core/earcon.py`: two rising notes (A6,
E7), 150 ms, synthesised with numpy and played through `sounddevice` the instant
the wake word fires. Until now the only sign was a line on the screen, which is
no use from across a room, outside the camera's view, or with your hands in the
air a metre and a half back. Non-blocking, because the loop's next move is to
start collecting speech. **Deliberately not drained from the capture buffer
afterwards** — AIA supports saying the wake word and the command in one breath,
so draining would throw away the start of "小艾同学，现在几点" every time, and a
short quiet tone at the head of the audio is something SenseVoice ignores.
`assistant.wake_chime` turns it off.

**"Play again" / "重来"**, since the Play Again button is on the game-over
screen and therefore just as unreachable as START was. Two more measured drops:

| candidate | worst neighbour | score | |
|---|---|---|---|
| `replay` | resume[`play`] | **0.80** | dropped |
| `restart game` | start_game[`start game`] | **0.91** | dropped |

`重来` is 0.50 from reboot[`重启`] — close enough to name, far enough to be
safe, and asserted both ways in the tests. It differs from "start game" in one
respect: `fresh=True`, so a round already in progress *is* restarted. "Again" is
a deliberate word where "start" is ambiguous, and refusing it would leave the
player with no way to start over from where they are standing.

**First real gameplay, and it works.** During a test round somebody played it:
**score 283, 28 fruit sliced, 5 bombs, 26 missed**, with the round ending early
because the five bombs took 25 seconds off the clock. That is the validation
29.6 said was missing — a person moving their hands, a blade following, fruit
being cut, the clock and the bomb penalty both behaving.

It also turned up a real bug: the live scores file had grown a `""` key
alongside `"fruit-ninja"`. A pose frame arriving after `_teardown` had cleared
`active` — it clears under the lock and stops the pose service outside it —
recorded the score under an empty name. Guarded at both ends and the junk key
removed from the device.

Still not verified: the wake chime through an actual spoken wake word (the
waveform plays on demand on the Pi, but no audio has gone through the wake
detector into it), and the slice sound heard from the Pi's speaker during play.

### 29.12 The two-minute round, the wood, the shadow, and the Ultimate

The game as of 29.11 was a black screen with five coloured discs on it. This is
the upgrade to something that looks like an arcade game: a warm dojo wall, the
player's own shadow behind the fruit, ten fruit that each cut and splash and
sound like themselves, and a two-minute round that builds to one guaranteed
boss fruit in the last twenty seconds.

**Nothing about the pose path changed.** Camera to Hailo-10H to seventeen
joints to two filtered wrists to a moving-segment collision test, exactly as
29.2 describes. The one addition to `aipi5/motion/` is eleven joint positions
published alongside the wrists — see the shadow, below — and it costs the pose
loop a dictionary comprehension over eleven keys.

#### The round

`ROUND_SECONDS` is 120, and the round now has a shape rather than a slope:

    120 -> 22 s   NORMAL              fruit, ramping
     22 -> 20 s   ULTIMATE_WARNING    banner and a warning sound
     20 -> 10 s   ULTIMATE_ACTIVE     the dragon fruit; no fruit, no bombs
     10 ->  0 s   NORMAL_FINAL        fruit resumes for a short coda

That is a `Phase` enum in `Session`, not a set of time comparisons, and the
reason is that each transition is an *edge* that must fire exactly once —
`if time_left <= 22` is true for every frame of the next two seconds, so the
warning sound would play sixty times. `_advance_phase` computes what the phase
*should* be from the clock and then compares it to what it was.

**The thresholds are seconds remaining, not seconds elapsed, and that is
load-bearing.** A bomb takes five seconds off the clock, so the two are not
views of one number. A player who sliced three bombs still gets their Ultimate
with twenty seconds showing, which is what the screen promised them.

The spawner's ramp was stretched by about 1.8x rather than doubled — full tilt
at 85 s rather than 45 s. Doubling would have kept the old round's shape and
played it slower; leaving it alone would have hit the ceiling at 45 s and held
it flat for over a minute, which stops reading as a climb. 85 s puts the
busiest normal stretch immediately before the warning.

#### The Ultimate Dragon Fruit

`aipi5/games/fruit_ninja/ultimate.py`, its own module because it shares almost
nothing with a thrown fruit. A thrown fruit is launched, arcs, is cut once and
is gone; this one is placed near the centre, bobs on a slow lissajous, is cut
thirty times and then keeps going. Putting it in `Fruit` would have added an
"unless this one" to every rule in that file.

Scoring is banded — 10 a hit to nine, 15 to nineteen, 20 to twenty-nine, then
30 — with a 500 bonus on the thirtieth. Past thirty it stays in bonus mode until
the ten seconds are up, which is what rewards somebody fast. Failing it costs
nothing at all: no life (there are none), no time, and every point already
earned is kept.

**The hard part was refusing hits that are not real.** A wrist resting inside a
fruit 172 px across is reported thirty times a second, and the naive test hands
the player five hundred points for holding still. It takes three gates, and
each lets through something the others do not:

| gate | value | what it alone would allow |
|---|---|---|
| per-hand cooldown | 110 ms | a still hand scoring 9 times a second |
| movement | 0.035 frame widths | a hand jittering fast in place |
| path crosses the fruit | segment vs moving circle | a swing nowhere near it |

The cooldowns are **per hand and there is no global one**. The intended way to
play is to alternate, and one global cooldown long enough for a single hand
would halve what two hands can do. 110 ms comes from the physical limit rather
than the middle of the suggested range: a sustainable back-and-forth is about
four passes a second per hand (250 ms), and the fastest real interval seen is
around 140 ms.

Both failure modes are asserted directly in `tests/test_ultimate.py`: three
hundred frames of a parked hand and three hundred frames of a jittering one,
each scoring exactly zero.

#### The wood

Procedural, painted once into an offscreen canvas and blitted with one
`drawImage` per frame: a radial base gradient, ~2600 bowed grain strokes, four
panel boards with seams, a faint 64 px lattice, a vignette. Deliberately dark —
everything the player looks at is drawn at full brightness on top of it, and a
handsome bright wood competes with the fruit.

**No asset packs were downloaded**, and `ASSET_LICENSES.md` records why in
full. The short version is that `aipi5/ui/server.py` serves exactly one file
from one fixed path and has no static directory, no MIME table and no
path-traversal defence — adding fifteen megabytes of sprites means building all
three plus a preloader, to draw shapes a canvas draws for nothing.

#### The shadow

The player's silhouette, behind the fruit, at 42% opacity. Built from pose
keypoints, never from the camera image, for two reasons and the second decided
it: section 22 forbids showing the room, and person segmentation would be a
second model on an accelerator that permits exactly one VDevice for the whole
machine — the mask would timeshare with the pose model the blade depends on.

Two implementation notes worth keeping:

- **It is drawn small and scaled up rather than blurred.** A canvas `filter`
  blur on a 1280x800 surface is tens of milliseconds on this hardware and would
  halve the frame rate on its own. Painting into a 320x200 buffer and letting
  `drawImage` scale it back up gives bilinear softening for free.
- **Joints the model is unsure about are omitted, not sent at low confidence.**
  `PersonPose.silhouette` filters; `as_dict` does not. A debug overlay should
  show uncertainty, but a shadow drawn through a guessed elbow does not look
  uncertain, it looks like the player has a broken arm.

Smoothing is on the *page*, at alpha 0.3 — much heavier than the wrists' 0.55,
and it costs nothing that matters. The blade must not lag the hand; a shadow
that lags by 60 ms is a calm shadow.

#### Ten fruit

Apple, banana, orange, lime, watermelon, pearl, grape, strawberry, kiwi, dragon
fruit. Each carries its own juice colour, wetness, sound voice, spawn weight and
launch/spin scaling in `fruit.py`, and each has a canvas routine — crescent,
cluster, cone, striped rind. **Shape, not tint**, because a player a metre and a
half back cannot tell a red circle from a pink one mid-swing but can tell a
crescent from a cluster in peripheral vision.

Sounds are the existing synthesiser with a table: band-passed noise swept
between two frequencies plus a pitched body, per fruit, with per-hit pitch and
level wobble so the fifth apple does not sound like a repeat. A five-voice cap
per 100 ms stops thirty rapid Ultimate hits summing into a crackle.

Sliced halves, juice, splats, sparks and score popups are all simulated on the
page rather than on the Pi — they cannot change a score, so they do not need to
be authoritative, and a hundred and fifty droplets are a hundred and fifty
numbers in the browser instead of a hundred and fifty more objects in every
snapshot thirty times a second. Every list is hard-capped and the oldest is
dropped, and particle counts scale down through 0.6 and 0.3 as the measured
frame rate falls: the effects are the first thing to go, because the one thing
this game cannot trade away is the hand following the blade.

#### What is verified, and what is not

Verified without hardware: the whole phase machine over seeded 120-second
rounds, the 29-versus-30 threshold, per-hand cooldowns and two-handed
alternation, both fake-hit refusals, bomb suppression during the Ultimate,
fruit already in flight surviving the transition, Play Again clearing every
piece of state, and the ten fruit all spawning. `tests/test_ultimate.py` is 60
tests; the suite is 793 and passes.

Verified by rendering the real page in headless Chrome with a synthetic
snapshot: the wood, the shadow following a pose, all ten fruit and the bomb,
splats, halves, popups, the combo readout, the Ultimate with its aura, cracks,
ring and counter, and the clock in `m:ss`.

**Not yet verified, and it needs the device:** section 47's performance numbers.
Pose FPS, camera FPS, render FPS, hand-to-slash latency, CPU, RAM and Hailo
utilisation all have to be measured on the Pi — a frame time from a Windows
laptop says nothing about a Pi 5. The debug overlay now reports render FPS
alongside the pose figures, plus the live phase, the four effect-list depths
and the current particle budget, which is what makes it possible to attribute a
drop to the effects rather than to the accelerator. The four cases to record are
ordinary play, a big splash, a bomb, and a 30-hit Ultimate sequence.

Also unverified on hardware: the new sounds through the Pi's speaker, and
whether 42% is the right shadow opacity in the room's actual lighting.

#### One rule deliberately not followed

The upgrade plan says in section 30 that slicing a bomb costs a life, and its
HUD sketch shows three hearts. **This game has no lives**, and did not get them
back. They were replaced by the clock in 29.11 for a recorded reason: with
lives, an awkward fruit was better ignored than attempted, because a failed
swing ended the game a third sooner; on a clock, doing nothing costs exactly
what missing costs, so there is never a reason not to swing. Section 30 also
opens with "keep existing bomb behavior", which is what was done — a sliced
bomb costs five seconds. Section 28's rule that a failed Ultimate must not cost
a life is satisfied trivially, since it could not.

### 29.13 The upgrade on the device, and where the frame rate actually goes

Deployed 2026-08-15 by `scp` (`~/AIPI5` is not a git repository), CRLF stripped
from `index.html` and `test_fruit_ninja.py`, `systemctl --user restart aipi5`
because a changed page is cached in the assistant's memory and restarting the
kiosk alone reloads the stale bytes. `NRestarts=0` after. The 204 game and
motion tests pass on the Pi as well as on Windows.

Nobody was in the room, so every round below was started with `restart`, which
skips the player-detected check that `start` enforces. That exercises the
clock, the phase machine, the spawner, the renderer and the load — everything
except hands.

#### It runs, and the round has the shape it was supposed to

    PHASE None    -> normal   at 120.0s left
    PHASE normal  -> warning  at  21.8s left
    PHASE warning -> ultimate at  19.7s left
    PHASE ultimate-> final    at   9.0s left
    GAME OVER at 0.0s left

Captured with `grim` off the real panel: the start screen at `2:00` with START
correctly disabled and `BEST 876` carried over from the last session; ordinary
play with grape, kiwi, strawberry and dragon fruit on warm wood; the amber
`ULTIMATE FRUIT INCOMING / GET READY` over the busiest stretch of the round —
nine fruit and two bombs, all of them distinguishable at a glance; and the
Ultimate itself, screen cleared, aura and particle ring, `0 / 30` under it and
the clock still legible at `0:13`.

#### Section 47, measured

| | idle, game open | in play |
|---|---|---|
| camera | 30.1 fps | 30.2 fps |
| pose inference | 30.1 fps | **26.3 mean, 24.5 min** |
| inference time | 29.4 ms | 34-39 ms |
| capture to pose | 43.3 ms | 53.6 ms mean, 72.2 max |
| render | — | **57-58 fps** |
| assistant CPU / RSS | — | 56.5% mean, 63.1% peak / 869 MB |
| kiosk CPU / RSS | — | 50.8% mean, 61.8% peak / 1112 MB |

Hailo-10H, firmware 5.1.1, `yolov8s_pose_h10.hef`, 9 output tensors, camera at
640x360. Two accelerator holders — the game and the assistant's person
detector — sharing the one VDevice, as designed.

Render sat at 57-58 fps against a 60 target for the whole round, in normal play
and through the Ultimate alike, with the particle budget never dropping below
1.0. The wood blit and the ten shape routines are not what costs anything here.

#### The pose rate falls under load, and it is not the new code

The interesting number is 30.1 idle against 26.3 in play, with a minimum of
24.5 — just under section 2's 25-30 band. It is worth being precise about where
that goes, because the obvious answer was wrong twice.

**It tracks fruit on screen**, cleanly:

    fruit on screen    pose fps
       0- 3            27.3
       4- 7            26.1
       8-11            25.2
      12-15            25.1

and during the Ultimate, once the last fruit thrown before it had fallen, the
rate went straight back to 29.4-30.1 with the dragon fruit and its aura and
ring still on screen. So the boss fruit costs nothing; a screenful of ordinary
fruit costs about 5 fps.

**First guess: my own observer.** The first run forked `ps -eo pcpu,rss,args`
twice a second and took eight screenshots. Re-run with a poll every two seconds
and nothing else: 26.3 mean, 24.5 min. Unchanged.

**Second guess: the JSON.** The pose loop and the SSE serialiser are threads in
one Python process, so they share a GIL — and the ten-fruit upgrade added four
fields to every fruit in every snapshot, thirty times a second. Measured on the
device rather than assumed:

    14 fruit: 102.6 us per snapshot, 3788 B   -> 0.31% of a core at 30 Hz

Nowhere near enough. Stripping the four static fields would save 24 µs and half
the bytes, and buy nothing worth the complication of a per-kind lookup table.

**So: is it a regression at all?** The backup taken before the deploy made this
answerable. Old code restored, service restarted, the same script, same room,
nobody in front of the camera:

    old (60 s round)   pose 27.2 mean, 24.6 min   pipeline 53.3 mean, 70.8 max
    new (120 s round)  pose 26.3 mean, 24.5 min   pipeline 53.6 mean, 72.2 max

**The minimum is identical and the pipeline latency is unchanged.** The dip
with fruit count is how this game has always behaved; what the upgrade changed
is how much of the round is spent at the busy end, because the ramp now holds
the floor for longer. The remaining ~1 fps of mean is one run against one run.

That leaves the real cause unattributed, and honestly so: it is not the
renderer (render fps never moved), not serialisation (0.3% of a core), and not
the observer. The likeliest remaining candidate is the SSE thread waking thirty
times a second on a busier snapshot and taking the GIL at the wrong moment
relative to the inference loop — cheap in total CPU but badly timed. Worth
chasing only if the minimum ever goes below about 24, which is where a hand
starts to feel it.

#### Camera and screensaver lifecycle

Closing the game returned `active: ""` with no error; `/api/game/preview` then
correctly answered 503, `/api/state` dropped its `game` object, and
`/api/camera/stream` delivered **48 JPEG frames in eight seconds** — the Brio
was lent, used and given back. The screensaver hold was released and the mode
was back to `night-weather`. The assistant's own vision reads the same `Camera`
object the camera page just proved, so it was not exercised separately: at ten
to one in the morning the end-to-end camera action speaks aloud in the room.

#### What still needs a person

Everything that needs hands, and it is the interesting half:

- slicing at all, and therefore every juice colour, splash, half, popup, combo
  and per-fruit sound in a real round
- the player's shadow, which needs somebody in frame — the joints are published
  and the renderer is proven, but nothing has stood in front of it
- render fps **under effect load**. The overlay read `fx 0d 0h 0s 0p` all round
  because nothing was cut, so the particle budget's step-down at 50 and 38 fps
  has never been exercised on hardware
- the Ultimate actually being hit: thirty valid cuts, two-handed alternation,
  the completion banner and bonus, and whether 110 ms is the right per-hand
  cooldown for a real arm rather than a simulated one
- whether 42% shadow opacity is right in that room's light

### 29.14 Two rounds with a person in them, and the three things they broke

The first two rounds anybody has played of the upgraded game, 2026-08-15.
Everything before this was simulated or rendered against a synthetic snapshot;
these are the numbers and the screenshots from a person standing in front of
the Brio swinging their arms.

    round 1   score 3628 (new best)   66 sliced   40 missed   10 bombs
              ultimate COMPLETE, 88 hits, 2710 points
    round 2   score 2255              46 sliced   49 missed    9 bombs
              ultimate COMPLETE, 78 hits, 1630 points

Both rounds completed the Ultimate comfortably. Nothing crashed, the phase
machine ran clean, the shadow tracked, and the blade followed the hands. Three
things were wrong, and none of them would have shown up without a player.

#### The bomb had become invisible

Ten bombs in round one is fifty seconds off a hundred-and-twenty second round —
nearly half the game — and it was not the player being careless. A screenshot
shows a bomb sitting inside their own silhouette: a near-black sphere, on a
dark shadow, on dark brown wood.

**This is a regression the wood background caused.** The old game was played on
black, where a dark sphere with a thin white highlight was perfectly legible
because the highlight was the only thing that needed to be seen. Moving the
background to warm wood and then putting a large dark figure in front of it
removed both of the cues it had.

The bomb now carries a pulsing red halo *around* it — so the area is brighter
than the wood even where the body is darker — a dashed counter-rotating warning
ring on its rim, and a fuse spark two and a half times the size with a glow of
its own. The body stays black; it should still look like a bomb rather than an
eleventh fruit. Verified against the worst case, which is a bomb lying on the
ninja's black sash: plainly visible.

#### The Ultimate ran away with the score

Section 23 requires that the Ultimate be a major bonus and not dominate the
final score. Round one: **2710 of 3628 points, 75%, off one fruit.**

The plan's +30 for every cut past thirty assumed a player would manage
thirty-five or forty. A real one landed **88** — 8.8 a second across two hands,
which is about 59% of what the 110 ms per-hand cooldown physically permits, so
the cooldown was never the limiter and raising it would only have made the
game worse to play.

So the tail was tapered instead: ten more cuts at the full +30, and after that
**+10, which is what an ordinary apple is worth** — the reasoning being that
this is precisely what the player would be scoring if the dragon fruit were not
on the screen. Completing it is untouched at 970 points, because that part was
never the problem. Re-scored: 88 hits now pays 1730 rather than 2710, and round
two's 78 hits paid 1630 rather than 2440.

The ratio is still high (72% in round two) but the other half of that is the
bombs: forty-five seconds of lost clock is forty-five seconds of normal fruit
not thrown. The bomb fix and the scoring taper pull on the same rope.

#### The ninja, and why it is drawn smaller than life

The player asked for the silhouette to be a ninja rather than a plain shadow,
with a reference illustration. It is built from the same eleven pose joints —
hood, eye slit, headband with two streaming tails, red waist sash with a knot
and tails, wrist cuffs — and none of the reference is used: a costume
convention is not protectable and the drawing of one is, so nothing was traced
or embedded. `ASSET_LICENSES.md` records that.

Drawn one-to-one it was **enormous** — filling two thirds of the play area,
with fruit having to be cut on top of it. That is not a bug in the drawing, it
is arithmetic: a player has to stand close enough that their wrists reach the
edges of the camera frame, because that is what makes the whole screen
reachable, so a faithful silhouette is necessarily screen-sized.

It is now drawn at **0.72 scale, anchored to the bottom of the screen and to
the player's own centre line**. Feet stay on the floor, the head comes down,
and it reads as somebody standing a few paces back — which is what the arcade
game being imitated actually shows. The cost is that the ninja's hands no
longer sit exactly under the blades. That is the right trade: the blades are
bright glowing markers with tails and are what the player tracks, and relative
motion is preserved exactly.

#### Performance with a person in front of the camera

| | empty room | round 1 | round 2 |
|---|---|---|---|
| pose | 26.3 mean / 24.5 min | 21.8 / 19.9 | **26.1 / 25.3** |
| camera | 30.2 | — | 30.2 |
| capture to pose | 53.6 mean / 72.2 max | 45.9 / 88.6 | 58.6 / 81.1 |
| assistant CPU | 56% | — | 76% |
| kiosk CPU | 51% | — | 77% |
| load average | — | — | 3.00 mean, 3.55 max of 4 |
| SoC | — | — | 61.7C mean, 62.8C max |

Round one's 21.8 is the outlier and is not explained; round two ran the *more*
expensive renderer — a 640x400 ninja buffer against a 320x200 grey silhouette,
four times the pixels — and came out at 26.1, inside section 2's 25-30 band.
The likeliest reading is that round one was the first round after a service
restart. Worth watching rather than acting on.

Nothing is thermally limited: 62.8C peak against a throttle point in the
eighties, and a load average of 3.0 on four cores.

### 53. Logitech BRIO 4K replacement — 720p90 motion capture

The Brio 101 was replaced with a **Logitech BRIO 4K** on 2026-08-15. This is
not only a product-name update. The new device exposes four V4L2 nodes:

| node | function |
|---|---|
| `/dev/video0` | colour capture, selected by AIPI5 |
| `/dev/video1` | colour-stream metadata |
| `/dev/video2` | 340×340 infrared capture |
| `/dev/video3` | infrared-stream metadata |

The colour node identifies as `Logitech BRIO`, serial `C26FB3A8`. Its MJPEG
mode table contains **1280×720 at 90 / 60 / 30 fps**; 90 fps was verified by
setting the format through V4L2 and reading the negotiated parameters back.
Unlike the Brio 101, this camera also offers uncompressed YUYV at 720p30, but
90 fps still requires MJPEG.

One camera control matters as much as the format. With
`exposure_dynamic_framerate=1`, the driver still reported 90 fps but the sensor
delivered only about **38 fps** in the room because auto exposure lengthened
the frame interval. With that control disabled, a direct stream warmed through
80–85 fps. A game now disables dynamic frame rate before opening its two-buffer
stream; returning the camera enables it again so ordinary 720p30 presence and
vision captures retain better low-light exposure.

The Hailo pose model does not need to run at 90 fps. `CameraLease` continuously
decodes the camera stream and stores one newest frame; if the approximately
30 fps pose loop has not consumed it, the next camera frame replaces it. The
extra cadence therefore lowers frame age rather than creating a backlog.

Measured with the deployed Fruit Ninja pipeline, a person in view:

| | measured |
|---|---:|
| negotiated camera mode | **1280×720 MJPG @ 90 fps** |
| delivered camera rate | **85.7 fps** |
| pose rate | **25.4 fps** |
| Hailo inference | **29.7 ms** |
| capture to decoded pose | **44.2 ms** |
| AIPI5 process CPU while open | **118% of 400% available** |
| system idle CPU | **67%** |
| SoC temperature | **51.0°C** |
| inference errors | **0** |

Closing the test game restored `1280×720 @ 30`, set
`exposure_dynamic_framerate=1`, and resumed the person detector. Video calls
remain deliberately capped at 720p30: tripling WebRTC encoding and home-uplink
load would not improve local gesture tracking. The Pi-side suite completed
after deployment: **815 tests passed, 1 skipped**.

### 54. Crossed-arms X replaces palm click

The browser's open-palm / closed-fist click was unreliable precisely at the
moment it mattered: closing a hand hides landmarks and the browser recognizer
often lost the hand before it could emit a click. Start and Play Again now use
one whole-body action instead: cross both forearms into an **X at chest
height** and hold it for 650 ms.

Detection is authoritative in the Python game loop and reuses the Hailo pose
snapshot already produced for the ninja. It checks shoulders, elbows and
wrists rather than hand shape. The test rejects ordinary arms, hands clasped at
the centre, and low crossed hands; it is invariant to camera mirroring and
tolerates a short keypoint dropout. After a state change the player must lower
their arms for 250 ms before the next X is armed, so an X held through the last
second cannot immediately start another round.

The page shows hold progress beside the green button and keeps the touch button
as a fallback. It no longer loads or runs the MediaPipe gesture recognizer, so
the browser does not compete with the game for JPEG decoding and hand-model
CPU. The retained files remain documented only for provenance. The expanded
local suite completes with **828 tests passed, 13 skipped**. After deployment,
the Pi completed the same **828 tests with 1 skipped**. A live Ready screen
reported `arms-crossed-x`, a 650 ms hold and no false trigger from an ordinary
stance. Closing that check restored the BRIO to 1280×720 MJPEG at 30 fps with
dynamic exposure enabled.

### 55. Chinese home title and acknowledged game exit

The home page's upper-left title is now **小爱同学**, including the
browser document title. The change is deliberately limited to the UI label;
the separately tuned wake-word spelling and recogniser configuration are not
changed.

The Fruit Ninja Exit Game button previously returned to the games library and
then reopened Fruit Ninja until hardware teardown completed. `show("games")`
correctly started an asynchronous `/api/game/close`, but the next 500 ms state
poll still saw the old session as active and `gameFromState` adopted it as if
voice control had just opened it.

The browser now remembers the specific game the player dismissed and refuses
to adopt that same id until `/api/state` acknowledges that no game is active.
A different id started by voice is still adopted. A rapid manual reopen waits
for the old close request too, so its delayed teardown cannot close the new
session. Failed close requests release the latch rather than hiding a live
game indefinitely. Two regression checks bring the local suite to **830 tests
passed, 13 skipped**. The deployed Pi completed all **830 tests with 1
skipped**. In a browser against the live Pi, the Chinese heading rendered,
Fruit Ninja was opened, started, paused and exited, and all 20 page samples at
250 ms intervals remained on the Games library for five seconds: no rebound.

### 56. Boxing — shared-pose training and opponent fight

Boxing is now playable from the existing four-tile Games page. It deliberately
does not own a camera, model, inference thread, or browser landmark detector.
`GameManager` creates one existing `PoseService`; the service publishes the
same selected person, filtered wrists, skeleton, capture timestamp, camera
rate and pose timing that Fruit Ninja consumes. The only Boxing-specific step
is the inexpensive motion analysis called from that same pose callback.

The implementation is split by responsibility:

| Module | Responsibility |
|---|---|
| `boxing/config.py` | scale-relative motion thresholds, damage, timing and difficulty tables |
| `boxing/motion.py` | punch/hook, guard/block, parry, duck, dodge and lean-back classification |
| `boxing/collision.py` | swept segment collision helpers for between-frame movement |
| `boxing/ai.py` | Observe/Guard/Attack/Recover/Defend/Counter opponent state machine and gradual adaptation |
| `boxing/damage.py` | animated 100 HP state and body-attached progressive injury zones |
| `boxing/game.py` | countdown, 90-second Training, fight rules, scoring, slow motion and result statistics |
| `assets/boxing/boxing.js` | 1280×800 layered Canvas renderer, filtered third-person player rig, animated anime opponent, effects, debug view and synthesised audio |
| `assets/boxing/arena-anime-v2.png` | original generated empty-arena background used beneath all live fighters and effects |
| `assets/boxing/player-red-torso.png` | generated transparent rear-view red-player body layer; pose-driven Canvas arms remain separate |
| `assets/boxing/opponent-blue-torso.png` | generated transparent front-view blue-opponent body layer; AI-driven Canvas arms remain separate |

The established 650 ms crossed-arms X starts both modes and replays the chosen
mode. The existing release latch remains authoritative, so holding the X
through the end of a round cannot immediately restart it. Perfect Parry sets
the game simulation scale to 0.1 for a bounded counter window; pose timestamps
and gesture classification remain real-time.

Development capability was audited before implementation. The available
in-app browser supplied Playwright-style DOM control, console capture,
1280×800 viewport control and screenshots. Python `unittest`, Node syntax
checking, the existing camera/accelerator debug payload, Git tooling,
deployment scripts and hardware checks were already present. Image generation
supplied one original, empty 2.5D anime arena background. The four user
screenshots informed only the rear-player / centered-opponent / ring / audience
composition; no pixels, characters, HUD, logos or sounds were copied. Fighters
remain lightweight code-driven Canvas rigs, so pose response, attacks, guard,
dodge, injuries and knockout reactions are animated at runtime. No package or
runtime dependency was added.

One project-specific tool was added: `scripts/preview-boxing.py`. It serves the
real production page with deterministic synthetic pose/combat snapshots so the
mode picker, Training, Fight, Perfect Parry, results and motion-debug overlay
can be exercised without Hailo hardware. It uses only the Python standard
library, is never imported by the application, and cannot contend for a
camera. `tests/test_boxing.py` separately feeds synthetic `PersonPose`
sequences directly into the classifier.

Validation on the development machine:

* **857 tests passed, 13 skipped** with `python -m unittest discover -s tests`.
* `node --check aipi5/ui/web/assets/boxing/boxing.js` passed.
* The production page was screenshot-inspected for mode selection, Fight,
  animated attack, Perfect Parry / slow motion, results/replay and debug.
* The layout audit found no visible element outside the viewport; the browser
  console contained no warning or error.
* The debug view reported camera, pose and game FPS, pipeline latency, dropped
  frames, both hand coordinates/velocities, guard, current action, AI state,
  attack impact and parry window.

The upgrade was deployed to the online Pi through the configured `aipi5` SSH
host. Only the UI, Boxing asset, preview/test files and documentation were
copied; the existing voice/backend process was not restarted. The kiosk UI was
restarted once and both user services remained active. A file-for-file backup
is available at
`/home/fuwenxu/AIPI5/.deploy-backups/boxing-anime-20260815-2348`.

On-device validation passed **857 tests with 1 skip**. During a sustained live
Boxing ready-state run, the Hailo pose service held 19.3–19.9 inference FPS,
the camera held 80.0–83.9 FPS, pipeline latency was 53.2–68.0 ms, and the
reported error count stayed at zero. The tunneled production page's debug
overlay reported 57 Canvas FPS. The AIPI5 process used about 985–993 MB RSS,
swap remained unused, and the Pi held 50.5 °C. The Hailo device remained
visible at PCIe address `0001:01:00.0`. No person was in the camera view, so
the safety gate correctly kept START disabled; attack, hit, parry, knockout
and replay visuals were instead exercised with the deterministic preview
harness. After the run, Boxing was closed, the pose model was released, and
the Logitech camera was successfully reclaimed by the assistant.

### 57. Boxing reference anatomy, three lanes and per-hit damage

The user supplied a full-body design sheet for the rear red player and front
blue opponent. The existing generated transparent body layers remain the
anchors, but their former constant-width Canvas arm strokes have been replaced
with an articulated sports-anime rig. Each arm now has a shaped deltoid,
tapered upper arm, elbow joint, tapered forearm, muscle cuts, wrist wrap and an
angular glove. Player shoulder, elbow and wrist positions still come directly
from the shared live pose; opponent limbs use the same anatomy with AI-driven
guard, jab, cross, hook, body-blow, dodge and lean geometry.

Both fighters now move as complete bodies among left, middle and right lanes.
Player lane selection comes from the scale-normalised torso offset, while the
opponent changes lane during attack and dodge decisions. Separate easing keeps
their movement independent and prevents lane changes from snapping. Impact,
block, parry and knockout effects use each fighter's current screen position
instead of the former fixed centre point.

The injury tracker previously ignored normal player punches: a hit deals one
or two HP for balance, but an injury was recorded only when a single hit dealt
at least four. Every successful hit now advances its body-attached zone. Stage
one is an irregular bruise; stage two adds a darker bruise and visible
swelling; stage three increases the swelling and adds two short, bright,
stylised blood marks plus a small rounded drop. Health tuning is unchanged.

The deterministic preview gained independent player/opponent lane controls and
isolated damage stages. Browser screenshots verified opposite lanes and all
three damage appearances with the production renderer. Node syntax checking
passed, the focused Boxing/UI suite passed 40 tests, and the complete local
suite passed **863 tests with 13 skipped**.

The verified files were deployed to the Pi with a pre-deployment copy at
`/home/fuwenxu/AIPI5/.deploy-backups/boxing-reference-20260816-01`. Both user
services restarted active. The device passed 80 focused tests and the complete
**863-test suite with 1 skip**. A short live Fight run reached the playing
state, drove the opponent into lane `-1` during a jab, held 85.0–93.5 camera
FPS, and reported zero motion errors. The game was then closed; V4L2 confirmed
that the assistant reclaimed the Logitech camera at 1280×720 MJPEG, 30 fps,
with dynamic exposure frame rate restored to `1`.

### 58. Boxing four-location baked damage matrix

Damage is now restricted to the four requested locations: left eye, right
eye, left shoulder and right shoulder. Every location has four states (normal,
first punch, second punch and third punch), and the opponent matrix contains
the Cartesian product of all four locations: `4^4 = 256` complete front-view
body images. The rear player cannot display eye injuries, so its meaningful
matrix contains the two visible shoulder locations: `4^2 = 16`. This produces
**272 complete body images** without storing 240 rear-view duplicates whose
only differences would be invisible.

The approved front/rear fighter matrices and a generated eye/shoulder damage
source sheet are retained in `artwork/boxing-damage/`. The deterministic
`scripts/build_boxing_damage_matrix.py` generator crops the clean generated
fighters to their registered runtime body layers, bakes irregular bruising,
swelling highlights/shadows and two restrained third-stage blood strokes into
the body pixels, clips every effect to the fighter alpha silhouette, and emits
WebP assets plus a key-order manifest. The shipped set is 18.48 MiB.

The Python injury state rejects all locations outside those four, advances one
stage per successful impact and caps at stage three. Head punches select an
eye and body punches select a shoulder. Since the player is seen from behind,
opponent impacts are mapped to its visible shoulders. Each API snapshot now
contains an explicit four-location `damage_matrix` and base-4 `damage_key`.

The Canvas injury-painting routine was removed. The browser selects the full
baked body for the current key, keeps only 12 recent matrix images in a lazy
LRU-style cache, preserves the clean body as the loading fallback and
crossfades completed body images over 180 ms. Arms, gloves, lane movement and
live pose reactions remain separately animated. The preview harness accepts
`demo-damage-NNNN` and `demo-player-damage-NN` for exact visual states.

Local validation passed JavaScript syntax checking, **44 focused tests**, and
the complete **867-test suite with 13 skips**. A 1280x800 production-renderer
browser check loaded combined opponent key `1232` and player key `13`; both
fighters showed the baked injuries and the browser console stayed clean.

The complete change was deployed to the Pi after a targeted backup at
`/home/fuwenxu/AIPI5/.deploy-backups/boxing-damage-matrix-20260816-01`.
Representative manifest/body SHA-256 values matched across machines, both
services restarted active, WebP and JSON routes returned their correct MIME
types, and the service virtual environment passed **867 tests with 1 skip**.
In the live ready-state check, the Brio held 89.9 camera FPS, Hailo inference
held 19.9 FPS and motion errors remained zero. No player was visible, so the
safety gate correctly refused START. After closing Boxing, the assistant
process reclaimed `/dev/video0`.

### 59. Boxing full-body pose and damage matrix

The approved sports-anime player/opponent artwork now replaces the live
fighter construction. OpenAI image generation produced 21 paired production
sheets from the user-supplied design reference and the three approved previews.
One malformed storyboard-style reaction sheet was rejected and regenerated as
one clean player/opponent pair. The production set covers idle, high/body
guards, left/right blocks, head/body straights and hooks, left/right dodges,
duck, lean-back, left/right parries, head/body reactions and knockouts.

Each sheet uses a flat green background that was removed with the ImageGen
`remove_chroma_key.py` soft-matte helper. The deterministic
`scripts/build_boxing_pose_matrix.py` builder splits the registered halves,
keeps punch reach at a consistent scale, derives pose-specific injury anchors,
and bakes the approved four-stage damage artwork into every complete fighter.
The final manifest contains **21 rear-player poses × 16 visible shoulder
states = 336 images** and **20 front-opponent poses × 256 eye/shoulder states
= 5,120 images**: **41 clean bases and 5,456 final WebPs**. The production pose
directory is 201,331,217 bytes including bases and manifest.

`boxing.js` maps every classified player action and AI state to its matching
base: straights and hooks retain head/body targets, guards and blocks keep their
side, parries keep the moving hand, defensive movement selects dodge/duck/lean,
and damage/KO events select their reaction images. One-shot player actions are
latched briefly so 90 fps camera input produces a readable frame, complete
images crossfade over 140 ms, and the existing left/middle/right lane easing is
preserved. The older procedural fighter is now only a first-load/training-dummy
fallback; normal fight rendering selects complete matrix cells and paints no
injury overlay.

At 1280×800, browser validation showed separated left/right lanes, a complete
blue attack pose, the semi-transparent rear guard pose, combined eye/shoulder
damage and no console warnings or errors. Focused Boxing/UI validation passed
46 tests. The complete local suite passed **869 tests with 13 skips**.

Deployment used the targeted backup
`/home/fuwenxu/AIPI5/.deploy-backups/boxing-pose-matrix-20260816-01`. The
259,115,008-byte transfer archive matched SHA-256 on both machines before
extraction. The live manifest and representative WebP routes returned the
expected JSON and `image/webp`; both user services restarted active. The Pi
virtual environment passed **869 tests with 1 skip**. A live Boxing ready-state
check measured 91.3 camera fps, 20.1 Hailo pose fps and zero motion errors. No
player was visible, so the safety gate remained false. Closing the game released
the motion session and the assistant process reclaimed `/dev/video0`.

### 60. Yoga Coach — one rig for the coach and the scorer

Yoga Coach is the third consumer of `aipi5/motion/`, added 2026-08-17. Like
Boxing it is a new directory under `aipi5/games/` plus one `CATALOGUE` entry
flipped to `playable: True`, and it changes nothing in `motion/`. The split
holds: `motion` still produces poses and knows nothing about games.

    aipi5/games/yoga/rig.py       fourteen bone directions, forwards
    aipi5/games/yoga/poses.py     32 frontal-plane poses, as bone tables
    aipi5/games/yoga/scoring.py   one frame of a body against one pose
    aipi5/games/yoga/lesson.py    three authored twenty-minute classes
    aipi5/games/yoga/game.py      the session: two clocks, a hold, a score
    aipi5/ui/web/assets/yoga/yoga.js   the coach, drawn from the same rig

#### The decision the whole game rests on

A pose is stored **once**, as fourteen absolute bone directions in image
coordinates. `rig.forward_kinematics` runs that table forwards into joint
positions; `rig.measure` turns joint positions into the twelve angles and seven
ratios a pose is judged by. The coach is drawn from the first function and the
player is scored against the second, and the player's own landmarks go through
the *same* `measure`. There is no second table of target numbers to keep in
step with the artwork, because there was never a first.

Three things fall out of it that were not designed separately:

- **Transitions are free and correct.** The page interpolates two angle tables,
  so a limb swings. Interpolating drawings would give a cross-fade;
  interpolating joint *positions* would give limbs that stretch and shrink.
- **Left and right cannot disagree.** The coach is drawn from behind, which is
  Boxing's third-person view, and the pose stream is already mirrored — so
  anatomical left is at the smaller x for both, with no conversion anywhere.
  "Raise your left arm" is one sentence for both bodies.
- **The wrong side is detectable for almost nothing.** Mirroring a metric set is
  a swap and a sign flip, so every frame is scored twice: against the pose and
  against its mirror. A beautiful Warrior II on the wrong side scores badly
  against one and well against the other, which is a signal rather than a
  nuisance.

`tests/test_yoga.py` checks the JS copy of the rig constants against the Python
one, and a one-off cross-check ran both forward-kinematics implementations over
all 32 poses: worst joint disagreement **9.0e-16** rig units.

#### What a frontal camera can and cannot see

Every pose is in the frontal plane, and that is a measurement constraint before
it is an aesthetic one. Shoulder width is the scale every ratio is divided by,
and a body turned side-on has no shoulder width. So: no floor poses (the Brio
is on a desk and a person lying down is out of frame), no profile poses, no deep
twists. Sun Salutation contributes only its standing half and the closing rest
is Mountain Breath rather than Savasana.

Two poses were changed by this during the build and both are worth recording:

- **Chair Pose bends its knees towards the lens**, so from the front there is
  almost no knee angle to measure. It is scored on `stack` — how far the hips
  sit above the feet, in shoulder widths — and explicitly *not* on the knees.
- **A standing backbend with the arms overhead is indistinguishable from Upward
  Salute** from the front: the spine simply gets shorter. It was rebuilt with
  the hands low on the back and the elbows folded, which is its own shape.

#### Calibration, and why the tolerances can afford to be tight

Angles need no calibration — an elbow at 90 degrees is 90 degrees on anybody.
Ratios do: a long-legged player standing perfectly upright measures `stack` 2.3
where a short-legged one measures 1.9, and judged against the coach's 2.07 flat,
one of them is told to sink lower in every pose forever.

So the coach's Mountain Pose is compared against the *player's*, once, while
they stand on the ready screen waiting to begin — which is the best calibration
sample this game will ever get, and the reason that screen waits for a player at
all. The two ratios that fall out (leg length, arm length, relative to the
coach) scale every ratio target afterwards. Samples are only taken from a body
already close to Mountain, so a calibration is never learned mid-Warrior.

Measured: a simulated player with **18% longer legs** scores 100 on the whole
Advanced lesson with calibration and is marked down without it.

#### Scoring

Per pose, 0-100 from three parts: **quality** 0.50 (mean accuracy across the
hold), **coverage** 0.30 (how much of the asked-for hold was credited),
**stability** 0.20 (Welford over the accuracy series, plus a count of breaks).
Coverage carries nearly a third deliberately: one frame of a beautiful Triangle
with no hold behind it tops out at fifty. Bands are the specification's own —
90 Excellent, 80 Great, 70 Good, below that Keep Practicing.

Tolerance bands are *the width of the population*, not measurement error, and a
metric inside its band is simply correct with no gradient at all. Difficulty
scales every band (beginner 1.35, intermediate 1.05, advanced 0.88) and sets the
accuracy needed to credit hold time (0.58 / 0.64 / 0.70).

Two weighting rules stop a pose from being diluted by the sixteen metrics it is
not about:

- a metric a pose leaves exactly where standing leaves it keeps 45% of its
  weight, rising to all of it once the pose moves it by a full band — automatic,
  so a pose need not list every joint it is *not* about;
- a pose's `emphasis` joints carry a fixed **62% share** of the total between
  them, however many there are. A share rather than a multiplier because a Neck
  Release is entirely about one joint out of seventeen, and doubling that joint
  still leaves a body standing perfectly still scoring in the high nineties.

Measured, on the most generous difficulty: a player who simply stands there
scores **27-48** against every real standing and balance pose, and **44 overall**
across a full twenty-minute Beginner class.

#### Two clocks

`time_left` is twenty minutes of wall time and is the promise the game made;
`hold_left` is what the current pose is still owed and only moves while the
player is in it. Folding them together breaks whichever promise was folded into
the other. Every step also carries a wall deadline (`hold x 1.6 + 5 s`), so a
player who cannot find Half Moon spends forty seconds being told what to fix and
then moves on. A player who never finds anything simply gets fewer poses, and
the class still stops at twenty minutes.

The per-pose results card is **not a phase**, and that is the difference between
a class that runs to twenty minutes and one that runs to twenty-one and a half:
forty poses times a two-second card is ninety seconds. It is drawn over the
start of the next pose's transition, where the coach is already moving.

Scheduled lengths: Beginner 43 steps / 19.75 min, Intermediate 40 / 19.88,
Advanced 37 / 19.90, all inside the 20-minute clock with the countdown.

#### The live camera during play, and the rule it bends

Section 22 kept the player's room off the screen during play, and that still
holds for Fruit Ninja and Boxing. Yoga is the deliberate exception: a player two
metres from the panel being told to straighten their back cannot see their own
back, so the feed *is* the correction. `/api/game/preview` gained bounded `fps`
and `w` parameters — the page asks for 12 fps at 640 px and the server clamps to
15 and 720, because a stale tab asking for 200 would take a core away from the
inference this game is built on.

#### Performance, measured on the device 2026-08-17

All three games were measured the same way, mid-round, with the kiosk showing
them and an empty room:

| | pose fps | inference | capture-to-pose | camera |
|---|---|---|---|---|
| Fruit Ninja | 17.1-18.6 | 38-49 ms | 56-74 ms | 80-88 |
| Boxing | 18.3-19.8 | 35-62 ms | 53-97 ms | 78-92 |
| **Yoga Coach** | **15.8-19.0** | 36-54 ms | 52-82 ms | 65-85 |

**Yoga is not a regression: it performs identically to the two games already
shipping.** The ~18 fps figure is a property of the device and the deployed
capture configuration, not of this game — section 59 already recorded 20.1 pose
fps for Boxing on 2026-08-16, against the 26-30 measured in section 29.13 when
the pipeline captured 640x360. Load average was 5.4-6.4 of four cores and the
SoC ran 72.5-74.7 C, under the soft limit; `get_throttled` reported 0x50000,
which is a historic under-voltage and a historic soft-temperature event with no
current throttling. Assistant RSS 924 MB of 8 GB.

Chasing the shared pipeline back to 26-30 fps is worth doing and is deliberately
**not** part of this change: it would touch the capture path both existing games
depend on. Yoga does not need 30 Hz — a held pose is the one thing in this
project that does not.

#### Verified on the device

Catalogue lists Yoga as playable; open, level select, start, transition, hold,
per-pose score card, pose advance, Yoga Complete with all six summary fields,
and the crossed-arms prompt. The coach animates between poses. Framing advice
("Step back so the camera can see your feet") appears when the legs are not
visible — yoga is the only game here that needs ankles, so the check lives in
the session rather than redefining readiness for every game. Closing releases
the camera; Fruit Ninja and Boxing both open and run afterwards with no motion
error. Local suite: **965 tests, 13 skips, all passing.**

#### Not verified

Everything that needs a body. Whether the tolerances feel fair to a real person,
whether the corrections arrive fast enough to act on, whether the coach is
legible from two metres, whether the crossed-arms X is comfortable to make at
yoga distance (further back than Fruit Ninja distance), and whether twenty
minutes is the right length. All of it needs somebody standing in front of the
camera, not another measurement.


### 61. Cutting the hand-to-screen delay by 40%, and widening the blade

Three requests: take the ninja figure off the play field, make the blade three
times wider so fruit is easier to cut, and find the motion-tracking latency and
reduce it — with the camera resolution and frame rate explicitly on the table
and image quality explicitly expendable.

The third one is the interesting one, because **the camera turned out not to be
where the time was going.** The mode that was already configured is the mode it
still runs in; everything gained came from three things the pipeline was doing
to itself.

#### The ninja

`drawPlayerShadow` and its rig, 450 lines, removed along with the three call
sites that fed it, the generated `ninja-head.png` and its licence row. Two
comments elsewhere had been written against it — the wood's lattice, "so the
eye has something to judge depth against when the shadow moves over it", and
the bomb's halo, "visible against the shadow, which is the case that actually
failed" — and both were reworded rather than left describing a figure that is
no longer there. The bomb keeps its halo: it was redrawn because it was
invisible against *dark wood*, and the wood has not moved.

`tests/test_ui_assets.py` had a test pinning the ninja's fixed anime
proportions. It is now a test that every part of the ninja is absent, which is
the property that can regress — a half-removal that stops drawing the figure
but keeps smoothing its pose every frame would otherwise pass.

#### The blade, and the one place it was not widened

The drawn trail is `BLADE_WIDTH_SCALE = 3` on both strokes, so the head goes
from 18 px across to 54.

The collision test was a **line with no thickness**: `slash_hits_fruit` took
the fruit's own radius and nothing else, so a swipe whose bright centre passed
a hair to one side of a grape scored nothing while the streak was drawn
straight over it. That reads as dropped tracking, not as a near miss. So
`collision.BLADE_HALF_WIDTH = 27` — half the drawn width — is added to the
target radius, which makes the tested shape the swept capsule the player can
see. On a 32 px grape that is nearly double the reach; on the Ultimate's 86 px
dragon fruit it is a third more.

**A bomb gets none of it.** Widening the blade is meant to stop a good swing
scoring nothing; letting the same widening set off a bomb the player steered
around would take with one hand what it gives with the other and turn "the
blade got thicker" into "the round got shorter". Generous towards a reward and
exact towards a hazard is what reads as fair rather than as inconsistent, and a
bomb is the one object on screen that is aimed *away* from, so its edge is
already being judged by eye. `game._blade_reach` is the one line that says so.

The debug overlay's slash segments are drawn at the real 54 px with a round cap,
because `closest_distance` measures against a segment with hemispherical ends —
the drawn shape and the tested shape are now the same shape.

#### Where the delay actually was

Measured with `scripts/bench_motion.py`, which is new and breaks one frame into
its stages — the read, how old the frame was when the pose thread picked it up,
the letterbox, the accelerator, and the tensor decode — because a single
end-to-end number cannot tell you that a mode which halved the frame age also
doubled the JPEG decode.

Three faults, in the order they were found:

**1. A "free" slice that cost a full-resolution copy — 8.3 ms.** The capture
thread published `frame[:, :, ::-1]` to convert BGR to RGB. That is a view, so
it looks free, and it is not: a reversed axis has a negative stride, OpenCV
cannot work on one, and both consumers silently copied the whole 1280x720 frame
before they could touch it. The letterbox, three ways, on a real frame:

    reverse the axis, then resize (was)          8.89 ms
    cvtColor at full size, then resize           1.55 ms
    resize into a kept buffer, swap after (now)  0.57 ms

The lease now publishes exactly what the driver gave it, and the channel swap
happens *after* the downscale, on a quarter of the pixels. The preview needed no
conversion at all after that — BGR is what `imencode` writes — so a full-frame
`cvtColor` there disappeared too.

**2. 3.9 MB of allocation and memset per frame.** `infer` was calling
`create_bindings()` and `np.zeros` for all nine output tensors on every frame,
zeroing 3.9 MB that the accelerator then overwrote completely. Allocated once
at load now, along with the 640x640 input square whose grey border is the same
grey on every frame of a session.

**3. The big one: 8.5 ms of dequantisation nobody wanted.** The outputs were
requested as FLOAT32, so the runtime converted all nine tensors on the way out.
Measured directly, one HEF against itself:

    run() with FLOAT32 outputs   28.04 ms   3.90 MB out
    run() with native outputs    19.50 ms   1.40 MB out

**8.5 ms, thirty per cent of the whole inference**, to produce 8,400 numbers the
decoder looks at and 3.8 MB it discards untouched — because `decode` already
thresholds before it decodes anything. So the model now takes the accelerator's
own UINT8 and UINT16, and `yolov8_pose.decode` converts the few hundred rows
that clear the score threshold, using the `qp_scale`/`qp_zp` the runtime
publishes per tensor. A HEF with per-channel quantisation or an unfamiliar
format falls back to asking the runtime, which is slower and correct.

That decoder had **no tests at all**, which was survivable while it was a
straight port and stopped being so the moment it started doing its own
arithmetic on integers — a wrong zero point moves every joint a little, which
looks like tracking that is merely loose. `tests/test_pose_decode.py` covers the
half-cell anchor offset, the sigmoid that is folded into the graph and the one
that is not, and above all decodes the same person twice, once from floats and
once from the integers those floats came from, requiring the two to agree within
a model pixel — with a companion test that a deliberately wrong zero point
*fails* that comparison, so the equality is not passing for the wrong reason.

#### The camera sweep, and why nothing changed

Every mode worth trying, with the assistant stopped and a person in shot, as
capture-to-decoded-pose:

    1280x720@90  MJPG   27.8 ms   46.3 pose fps   28% cpu   <- unchanged
    640x480@120  MJPG   26.5 ms   46.8 pose fps   21% cpu
    1280x720@60  MJPG   30.1 ms   47.1 pose fps   22% cpu
    960x540@30   MJPG   21.5 ms   28.2 pose fps   10% cpu
    640x360@30   MJPG   21.6 ms   28.2 pose fps    5% cpu
    1280x720@30  NV12   30.9 ms   28.3 pose fps   16% cpu
    1280x720@30  YUYV   31.0 ms   28.3 pose fps   13% cpu

The camera advertises exactly two modes above 30 fps — 1280x720 MJPG at 90 and
640x480 MJPG at 120 — so this is not a grid. NV12 and YUYV are there to answer
"what if we drop the JPEG decode", and they do drop it, and it does not help:
both cap at 30 fps, and what a compressed mode buys is frames.

**The 30 fps rows look like the winners and are not.** Their low figure is the
pose loop *waiting* for a camera slower than itself, so the frame is fresh and
then sits on screen for a whole 35 ms until the next one. Mean displayed
staleness is latency plus half the update period, and on that measure all seven
rows are within 2 ms of each other; what the fast modes additionally buy is
halving the gap between pose samples, which is what collision segments and blade
smoothness are made of. Repeated inside the running system with the browser
drawing at 60 fps, 30 fps capture measured *worse* end to end — **46.2 ms
against 36.4 ms** — because a pose loop that is now faster than 30 fps spends
the difference waiting.

640x480@120 is 1.3 ms better than the mode in use and is **not** taken here.
The camera's 4:3 modes are a horizontal crop rather than a rescale — confirmed
by capturing the same scene at both and measuring two door handles, 75% of the
16:9 field — and the field of view is how far a player has to move their arms
to reach the edge of the screen. 1.3 ms is not worth a quarter of the play
area.

**Section 62 takes it anyway, and is right to**: measured inside the running
system rather than on an idle one, those two modes are 17 ms apart rather than
1.3, because the 720p capture thread is competing with the browser for four
cores. That section also has the two faults that had to be fixed first.

#### Before and after

The same A/B the deploy notes ask for: the pre-change `aipi5/motion/` restored
from the backup on the device, measured, then the new code put back and measured
again. Inside the running assistant, with the kiosk drawing the game, a round
live and the same scene:

    capture -> decoded pose   62.0 / 58.1 ms  ->  36.4 / 35.6 ms    -24 ms, -41%
    pose rate                 17.5 / 18.7 fps ->  33.0 / 32.5 fps        +77%
    hailo inference           43.4 / 39.4 ms  ->  27.7 / 23.6 ms    -16 ms, -38%

With the assistant stopped, so the accelerator is not shared and the CPU is not
contended, the same mode measures **45.4 ms -> 27.8 ms** and **25.6 -> 46.3
pose fps**, broken down as age 6.2, letterbox 0.9, accelerator 19.7, decode 0.7.
The floor is now the accelerator: 19.7 ms of a 27.8 ms total, and
`/usr/share/hailo-models/` has no smaller pose HEF for HAILO10H than the
`yolov8s` in use — `yolov8m_pose_h10` is the only other one and is larger.

#### What is still worth chasing

**The machine, not the pipeline.** Under heavy play — fruit on screen, a person
swinging, the browser compositing splashes at 60 fps — the load average passes
4 on four cores and capture-to-pose swings between 22 ms and 63 ms depending on
what the browser is doing at that instant. That is contention, not the camera:
the same instant shows the accelerator taking 33 ms for an inference that costs
19.7 ms on an idle machine. The next real gain is on the drawing side.

`motion.target_fps` is in the configuration and has never been read by anything.
It now has an obvious job — capping the *consumer* rate so the game simulation,
the snapshot JSON and the SSE stream do not all run at 46 Hz when 30 would
do — but capping was not done, because it trades blade smoothness for headroom
and there is no measurement yet saying which way that goes.

#### Verified on the device

The figure is gone from the play field and the trails are visibly three times
wider, in a screenshot of a live round. Fruit cut, score climbing, streak
counter running. The Yoga live feed still shows the room in the right colours
after the preview stopped converting them, checked on a frame pulled from the
stream. Suite on the Pi: **978 tests, 6 skips, all passing**; on the development
machine, 978 with 22 skips — the extra skips are the new decoder tests, which
need numpy and only run on the device.


### 62. 640x480 at 120, and what a faster pipeline broke in Boxing

Section 61 measured 640x480@120 as the fastest mode the camera has and declined
it, because the field-of-view crop was not worth 1.3 ms. Asked for it anyway,
and asked for the same work to reach Boxing.

Taking it turned out to be right for a reason the first sweep did not show, and
two things had to be fixed before it was safe.

#### It is worth much more in Boxing than the first sweep suggested

The section 61 sweep ran with the assistant stopped, so the pose loop had the
machine to itself and the two fast modes were 1.3 ms apart. Inside the running
system, with the kiosk drawing, they are not close at all:

    boxing, capture -> decoded pose      1280x720@90   640x480@120
    ------------------------------------------------------------
    capture -> decoded pose                  48.1 ms       31.0 ms
    pose rate                                34.5 fps      37.8 fps
    hailo inference                          31.4 ms       27.0 ms
    cpu idle                                    29%           46%

**17 ms, 36%, between two modes that measured 1.3 ms apart on an idle
machine.** The difference is CPU: Boxing's renderer is the heaviest thing this
device draws — a full-body fighter, baked damage WebPs, an arena — and it is
competing for four cores with a capture thread decoding 1280x720 MJPEG eighty
times a second. Dropping that to 640x480 hands the difference back to the
browser, and inference stops being slowed by contention.

There is no 1280x720@120: the camera advertises 90, 60, 30 and below at that
size. A request for 120 there negotiates down to 90, measured — 38.6 ms, which
is 720p@90 with a longer name.

#### Before and after, for Boxing

The section 61 A/B repeated with Boxing running, pre-change `aipi5/motion/`
restored from the device backup and then put back:

                             before          after (640x480@120)
    capture -> decoded pose   70.8 ms   ->   31.0 ms    -39.8 ms, -56%
    pose rate                 17.5 fps  ->   37.8 fps        +116%
    hailo inference           47.6 ms   ->   27.0 ms    -20.6 ms, -43%

Split between the two causes, both measured on the device: the old code at the
new camera mode is 54.7 ms, so of the 39.8 ms, **16 ms is the camera mode and
24 ms is the pipeline work** from section 61.

#### The crop rescales one axis and not the other

The camera's 4:3 modes are a horizontal *crop* of the 16:9 field — 75% of the
width, all of the height. For Fruit Ninja that is exactly the accepted trade:
positions map straight to the screen, so a player moves their arms three
quarters as far to reach the edge.

For Boxing and Yoga it is not a trade, it is a **fault**, and it took some
staring to see. Both measure *shapes* — signed angles between bones, and
distances divided by a shoulder span — out of coordinates normalised to the
frame. Normalised coordinates carry the frame's aspect ratio inside them:
`tan θ' = tan θ × aspect`, so a limb at a true 45 degrees reads as 60.6 degrees
through a 16:9 camera and 53.1 degrees through a 4:3 one. **7.5 degrees of
systematic error on every diagonal limb**, which is most of a yoga tolerance
and would have marked Triangle Pose wrong for somebody doing it correctly.
Boxing's duck, measured as a vertical drop over a horizontal span, would have
needed a third more movement to register.

`PersonPose.shaped()` is the fix: x is re-expressed in a fixed reference aspect
before anything measures a shape, so every constant in `boxing/config.py`,
`yoga/poses.py` and `motion/gestures.py` keeps the meaning it was measured
with, whatever the camera is set to. The aspect comes from the letterbox — the
frame that actually arrived, not the one that was configured, because the
driver is free to answer a request for one size with another and does.

The split is the interesting part and is worth stating as a rule: **a position
wants the camera's own normalised space and a shape does not.** Fruit Ninja
never calls `shaped()`, and should not — the screen is deliberately stretched
to match camera space, and correcting it would be undoing that on purpose.

`tests/test_pose_shape.py` photographs one body two ways and requires all three
consumers to measure it the same, with a companion test that the uncorrected
version really does move — otherwise the equality would be passing for the
wrong reason.

#### The faster pipeline was quietly making Boxing harder to play

The second fault, and the one that matters most for "make it land". Every punch
threshold in `boxing/config.py` is a **distance**, and a distance only means
something over a stated time. They were measured against the previous *frame*
at a pose rate of about thirty, so `punch_travel` of 0.065 shoulder widths was
implicitly "in 33 ms". Section 61 took the pose rate to forty-five. The frames
got closer together, the distance between two of them shrank by a third, and
the same punch stopped clearing the same threshold.

Measured, by sweeping how slowly a punch can be thrown and still be seen:

    pose rate     slowest punch still detected
                  per-frame (was)   33 ms window (now)
    26 fps             610 ms            610 ms
    30 fps             610 ms            610 ms
    45 fps             420 ms            610 ms
    60 fps             310 ms            610 ms

**A third of the range of punches the game recognises, lost to a latency
improvement.** That is the worst shape a regression can have, because it does
not present as "the frame rate changed", it presents as the tracking having got
worse — and the punches it drops first are the slow deliberate ones, thrown by
exactly the person this device is in the house for.

So a wrist is now compared against where it was `PUNCH_WINDOW_S` ago —
interpolated between the two samples that bracket 33 ms, whatever the frame
rate is — rather than against the previous frame. `travel`, `speed`,
`radial_gain` and `extension_gain` all become rate-independent together, and a
faster pipeline buys what it should: the decision arrives sooner, on fresher
data, rather than a different decision.

The neutral-stance blend had the same bug in miniature and got the same fix: a
flat 3.5% per frame chased the player 1.7x faster once the rate went up, which
eats the very offset a dodge is measured against. It is a 0.94 s time constant
now — what 3.5% per frame came to at thirty frames a second.

`tests/test_boxing.py` throws one 450 ms punch, described once as a function of
wall-clock time, sampled at 30, 45 and 60 fps. It was checked against the code
it replaces and **fails there at 45 and 60**, which is the only way to know a
test of this kind is worth having. Its companion refuses a 1.5 s reach at all
three rates, so "rate-independent" cannot quietly become "always yes".

#### What did not transfer from Fruit Ninja, and why

The 3x blade does not have a Boxing analogue and was not invented one.
`slash_hits_fruit` was widened because a *drawn* streak 54 px across was being
tested as a line with no thickness. Boxing has no such gap: there is no spatial
hitbox at all — `_player_attacks` lands a punch the moment the analyzer
classifies one, unless the AI dodges or blocks it — and no thin drawn blade,
because the player is a whole fighter. Its equivalent of "the swing connected
and nothing happened" is a punch that fails to classify, which is precisely
what the window fix addresses. Tripling `punch_travel` would have made it
*stricter*; tripling `parry_radius` to two and a half shoulder widths would
have made any moving hand a parry.

Removing the ninja has no analogue either. The figure in Boxing is the game.

#### Verified on the device

Boxing in training mode with a person in front of the camera: fighter tracking,
punches registering, score climbing, combo counter running, 39.1 pose fps.
Fruit Ninja on the same camera mode: both blades tracked, fruit spawning, no
figure. Suite: **991 tests, all passing**, on the Pi and on the development
machine.

One thing to watch, since the field of view is now narrower: a player has to
stand slightly further back than before for their whole upper body to be in
shot, and Yoga — which needs ankles — was already the game that asks for the
most distance. Its framing advice ("Step back so the camera can see your feet")
is unchanged and still the thing that says so, but nobody has stood through a
class on this camera mode yet.


### 63. Boxing in first person

"Change the boxing game to 1st person view, regenerate pictures of action if
needed." The view was over the player's shoulder: a rear-view fighter driven by
their pose, with the opponent in the middle distance beyond him.

The move is not a camera position. It is a change in **which things exist**.

#### The player stops existing

`drawPlayer` and everything under it is gone — the rig, the smoothing, the
joint constraints, the procedural torso and head, the ground shadow, the
twenty-one-pose player selection. Nothing of the player is drawn but two
gloves. `player-red-torso.png`, the sixteen baked player damage bodies and the
336-image player pose matrix are all still on disk and **not one of them is
requested any more**; they are kept rather than deleted because between them
they are the whole of the third-person view and nothing needs them gone.

#### Their head becomes the camera

Leaning, ducking and dodging used to slide a figure across a still arena.
They now move the arena, because that is what moving your head does.
`headCamera` turns `motion.posture` into a translation, a zoom and a roll, and
`applyCamera` applies it at **two depths**: the arena at 0.34 and the opponent
at 1. That difference is the parallax, and it is what makes two flat images
read as a room with a man standing in it — the same movement of the head shifts
something across the room and something at arm's length by very different
amounts. Leaning back shrinks the shoulder span, which is the only depth cue
this camera has and a surprisingly convincing one.

The gloves are drawn **outside** the camera, deliberately. They are on the ends
of the arms of the head the camera *is*; moving them with it would move them
twice, and a duck would drop the player's own hands out of their own view.

A punch that lands on the player has nothing on screen to flinch, so the
picture takes it: a red wash strongest at the edges and thin in the middle —
the opponent must stay visible through it, because the punch after the one that
hurt is the one you have to see coming — plus a harder shake, and the sparks
where the glove arrived rather than at a position along the bottom of the
frame. The rope that used to be drawn across the bottom of the picture is gone;
it sat where the player's own gloves now are, and a rope in front of your own
hands puts you outside the ring looking in.

#### The pictures: cut, not generated

The request allowed for regenerating artwork. None was needed, because the
picture wanted already existed inside artwork this project has generated and
licensed: **sheet 06, the right straight to the head**, whose rear-view fighter
has one arm fully extended away from the camera. That is exactly the view a
player has of their own punching arm. Nothing on that half of the sheet touches
it, so a crop and an alpha trim get a clean cutout with no matting by hand.

`scripts/build_boxing_first_person.py` cuts it, and cuts it **in two at the
wrist**, because the pieces move differently: the glove sits at the tracked
hand and is scaled by how far away it is, while the forearm is a bridge to
wherever the glove went. One right arm, mirrored for the left, so the two hands
cannot drift out of agreement. Stored at 2x so the good resampling happens once
rather than every frame.

Three things had to be measured rather than assumed, and each is in the
manifest the builder writes: where the wrist is in each piece, and the sprite's
own axis — the arm is drawn at about six degrees off horizontal, and rotating
about the bounding box instead of the axis hangs the glove off the side of its
own wrist.

#### Three faults found by looking at it

**The glove flew off the top of the screen on every jab.** One number moved the
glove up and down with the player's hand, and it was set for a hand at guard.
A hand at the end of a straight arm is twice as far from the eye, so the same
movement covers half the angle; a single figure for both threw the glove into
the ceiling the moment the arm extended. Two constants now, one per distance.

**The forearm looked like a plank.** It was drawn from a fixed point off the
bottom corner all the way to the glove, which on a straight punch is six
hundred pixels of a three-hundred-and-sixty pixel drawing — every muscle in it
smeared into a streak, on the shot the player throws most. The arm now keeps
its own proportions and its far end simply falls where it falls, with a squared
alpha ramp **baked into the sprite** so it fades out instead of ending on a
hard stretched edge. Baked rather than done per frame with a scratch canvas and
a gradient, because it is the same ramp on every frame of every round.

**Both arms were the wrong arm, and upside down — which is one fault, not
two.** Reported from the screen in those words, and they are the same thing:
the sprite is a right arm reaching to the right, so its top edge is the outside
of the limb; pointing it up and inward, which is where a guard is, turns it
past vertical and puts its top edge underneath. An arm reflected along its own
length **is the other arm**, so a single missing roll shows up as the pair
swapped *and* as each one flipped. One negative in the y scale fixes both, and
the glove takes the same roll — which is why it now adds its axis tilt where
the forearm subtracts it. `tests/test_ui_assets.py` pins the two transforms,
because nothing else in the suite can see this and the screen is the only place
it is visible.

#### Verified on the device

The renderer was driven with fabricated snapshots through the real page over an
SSH tunnel — guard, a right straight, and a duck-and-slip — because the fault
in each of the three above is a thing you can only see. Then live on the kiosk:
a fight started by a real person, HUD and countdown running, their right hand
extended into the opponent and their left up in guard, both gloves correctly
oriented, at 41.2 pose fps and 33.3 ms capture-to-pose. Suite: **993 tests**,
passing on the Pi and on the development machine.

One consequence of the camera mode in section 62 showed up here first: the
player was refused a start with "move back a little so your shoulders are
visible". The 4:3 field is a quarter narrower, so the distance a player has to
stand at has genuinely changed, and Boxing is where they notice.

### 64. An image generator, so "regenerate the artwork" is a real answer

Twice in two sessions the answer to "regenerate pictures of action if needed"
was that there was no image generator to hand — the boxing first-person view
was built by cutting up a sheet that already existed, which worked, and would
not have if the sheet had not existed. `.claude/skills/image-gen/` closes that.

`scripts/imagegen.py` has three subcommands: `models` prints what the key can
actually reach, `generate` makes a picture from a prompt, `edit` makes one from
a prompt and one or more reference images — which is the important one, because
new artwork that does not match the sheets already here is worse than none.

Three decisions worth keeping:

**Standard library only.** The `openai` package is installed on the Pi and is
not installed on the development machine, and an artwork tool that runs on one
of the two is a tool that gets used once. `urllib` is on both; the multipart
body for `/images/edits` is thirty lines.

**No second credential path.** The key comes from
`aipi5.core.config.credentials()` — the same environment-then-gitignored-file
resolver everything else uses, with the same rule that it is never printed.
A tool that read `OPENAI_API_KEY` itself would be a second place to audit and a
second place to fix when the key rotates.

**The skill carries the project's asset rules, not just the API's.** Where
sources live against where served files live; that `ui/server.py` serves a
fixed extension list and anything else 404s however correctly it is placed;
that PNG is 1235 kB against WebP's 86 kB for the same 1024-square with alpha;
that derivatives belong in a build script rather than in hand edits; and that
every generated file needs an `ASSET_LICENSES.md` row, because that file is the
record that no third-party pixels are in this repository and it is only true
while it is complete.

Both paths were run end to end before being called finished — a transparent
glove generated and trimmed, then the same glove recoloured through `edit`
against itself as the style reference, then the same again as WebP to confirm
the alpha survives the format. Two faults were found by doing it rather than by
reading it: a shadowed variable in the multipart builder that rebound the
`path` parameter and posted the whole request to `/v1<the image's filename>`,
which the API answers with a 404 and an empty body; and an unquoted
`description:` in the skill's own frontmatter, which contained a colon-space,
which YAML reads as a nested mapping — the skill would have failed to load and
said nothing about why.

### 65. The English voice reading Chinese, and a browser that opens by voice

Reported from the room: the assistant does not answer in Chinese, and where a
Chinese word appears it says the words "Chinese letter" once per character.
Both halves came out of one turn in the journal.

```
07:29:17 aia.stt.sensevoice   stt <Transcript zh/yue 195ms '打开 youtu 。'>
07:29:20 aipi5                llm 2135 ms: '我不能打开其他应用。请按屏幕上的 Music 按钮…'
07:29:23 aia.tts.piper        tts[en] 3244 ms to audio: '我不能打开其他应用。…'
```

Three things are visible there. The recogniser heard Cantonese and was right.
The model answered in Chinese and was right. And the reply was then read by the
**English** voice, which is the fault.

**Why the wrong voice.** `reply_language` asked `detect_script`, which counts
characters: `打开 youtu 。` is two Han against five Latin, so it said English —
over a recogniser that had listened to the audio and said `yue`. The counting
is not fair inside one sentence, because one Han character is a word and one
Latin character is a letter, and no threshold repairs it: "Play 周杰伦" is the
same shape with the languages the other way round and the opposite right
answer. So the counting is now *declined* rather than tuned. `mixed_script` in
`aia/stt/base.py` reports a code-switched transcript, and `reply_language`
hands those straight back to the recogniser's verdict; `detect_script` still
decides where the text is all Han or all Latin, which is the case the function
was written for — a confirmation that named the language of its question and
got an answer in the other one.

**Why it sounded like that.** espeak-ng has no pronunciation for an ideograph
and names it instead. Measured on the device, the same text through both
voices:

| text | en_US-lessac | zh_CN-huayan |
|---|---|---|
| 请按屏幕上的 Music 按钮 | 5.97 s | 2.34 s |
| Playing 周杰伦 | 2.72 s | 1.34 s |
| Opening YouTube. | 1.31 s | 1.34 s |
| a whole English sentence | 4.18 s | 3.25 s |

The first two rows are the babble. The last two are the reason the fix is
asymmetric: the Mandarin voice reads Latin as words, so it is safe to give it
anything, while the English voice cannot be given Han at all. `Speaker.say` now
plans an utterance by script — Han runs to the Chinese voice, everything else
to the voice that was asked for — and concatenates. A Chinese reply plans to a
single segment and is still one synthesis at one voice's prosody; only an
English reply carrying Han is split, which is exactly where splitting is worth
its ~250 ms. The log line reports the voices used rather than the language
requested, `tts[en+zh]`, because `tts[en]` on a Chinese reply is how this was
found and the next one should be as easy to see.

After, on the device:

```
tts[zh]    '我不能打开其他应用。请按屏幕上的 Music 按钮。'   4.54 s of audio
tts[en+zh] 'Playing 周杰伦'                              1.71 s of audio
tts[en]    'Opening YouTube.'
tts[zh]    '正在打开 YouTube。'
```

**And the thing that was actually being asked for.** The turn above was
somebody saying "open YouTube", and the honest part of the model's refusal was
that it could not. `aipi5/browser/launcher.py` adds it as a spoken command —
not a tool. `aipi5/llm/tools.py` already argues that starting an application in
somebody's living room has to be a person deciding rather than a model
inferring, which is why there is no `open_kodama` tool; a browser is the same
kind of act, so it is declared the same way and the model is told to tell
people what to say instead of refusing flatly.

Four decisions:

**No `{site}` slot.** One command per entry in a table in source. A slot would
put the address bar behind a mis-heard word.

**A decorated window.** Not `--app`, not `--start-fullscreen`. The agent's
browser carries the scar in its own docstring — the owner opened YouTube, met
a consent wall, and had nothing to press — and this project has built a window
with no way out of it twice. 1280×770 at 0,0 so the title bar fits on an 800 px
panel.

**Its own profile.** Sharing `~/.cache/aipi5-ui` would not open a second
window: Chromium hands the command line to the instance already running, which
would have put YouTube in a tab *inside the kiosk*, over the assistant's face,
with no tab strip to get back from. That same forwarding is how the window is
raised on a second ask, and what it does is open a tab — verified, and the
reply says so rather than claiming only to have raised something.

**Phrases measured, not chosen.** The transcript to plan for is the one the
device produced: `打开 youtu 。`, the recogniser not reaching the end of an
English word inside a Mandarin sentence. Folded to pinyin that is `dakaiyoutu`
against the phrase's `dakaiyoutube` — 0.91, over a 0.78 floor — and 打开优兔
and 打开有兔 score the same, because sound is what is compared. 打开油管 is a
phrase of its own rather than a near-miss: `dakaiyouguan` scores 0.75, under
the floor. Measured against every phrase of every other command, the nearest
neighbour either of them has is the launcher sitting beside them — 打开YouTube
against 打开音乐 at 0.70, and 打开油管 against 打开播放器 at 0.64. Eight and
fourteen hundredths of headroom, pinned in `tests/test_browser.py` rather than
asserted here, because this project has had a phrase comment quote a score that
was wrong by 0.19 and nothing but a test found it. The music player keeps every
phrase it had.

### 66. One browser: the voice command, the phone, and the hand

Reported the same day §65 shipped: "open YouTube" opened YouTube, and the hand
tracker did not come on. The window was there, the page loaded, a finger
worked — and waving at it did nothing.

**Hand control is not a property of a window. It is a property of a pipe.**
`aipi5/agent/pilot.py` reads wrists off the accelerator and posts gestures to
the agent runtime, which calls the root helper, which drives Chromium over a
CDP pipe *the helper holds*. `Housekeeping._agent_hands` only lends the camera
to the pilot at all while the agent's `browser_state` says a page is open. A
Chromium started by `subprocess.Popen` from the assistant satisfies none of
that: nothing holds a pipe to it, so it is invisible to the pilot, to the
camera hand-off, and to the kiosk's own "a hand is driving" notice.

So the launcher no longer starts a browser when there is an agent to ask. It
sends the URL to `AgentProxy.open_page` → `agent.open` → `AgentService.
open_page` → the helper's `browser_open` — the same operation the phone's agent
calls, into the same window. One browser: the one asked for out loud, the one
the phone drives, and the one a hand drives.

Four things that made this more than a redirect:

**It is not an `agent.ask`.** A run would put a language model between "open
YouTube" and a URL that was chosen in this repository's source. `open_page`
takes no model, no approval and no run — it is a sibling of `gesture()`, which
is the other message the *assistant* originates rather than forwards. The
boundary is unchanged and checked: `open_page` names `browser_open` as a
literal, and the address still goes through the helper's `_check_url`, which
refuses `file:`, `javascript:`, localhost and RFC1918 whoever is asking.

**It had to stop blocking the turn.** Measured on the device, `browser_open` is
**5.3 s cold and 3.0 s warm** — most of it the 2.5 s settle and the
accessibility-tree read that exist so a *model* can read the page, and which a
spoken command has no use for. The voice loop's whole budget is 2.5 s. So the
hand-off goes on a thread and the sentence is spoken at once: measured 0.00 s
to the reply against 3.35 s to the page, with the window appearing about a
second in. "Opening YouTube." is a statement about what is starting.

**A failure after the reply has nowhere to go**, so it goes three places: the
journal at ERROR with the reason in it, `last_error` on the settings page, and
a fallback to a browser of our own rather than leaving somebody who asked for
YouTube with nothing. The fallback is the worse window on purpose-built terms —
no hand, no ad blocking — so taking it is logged as a warning that says which
two things will not work, instead of leaving it to be discovered by waving.

**The four-second cache had to be dropped, not waited out.** Hand control
follows `browser.open` in the agent's snapshot, and `_browser_state` caches it
for four seconds so a 1 Hz poll is not a socket round trip forever. Four
seconds of a hand that does nothing is exactly the interval in which somebody
decides the feature is broken, so `open_page` clears it.

Verified on the device, closing the browser and opening it again through the
launcher:

```
browser closed            agent_browser=False  hand_control=False
"a spoken command asked for YouTube; handing it to the agent's browser"
reply spoken              0.00 s
page landed               3.35 s
"YouTube is open in the agent's browser ('YouTube'); a hand can drive it"
three seconds later       agent_browser=True   hand_control=True
```

and in the journal beneath it: `camera lent to hand control`, `capturing
640x480 MJPG at 120.0 fps`, `hand control running on the accelerator`,
`[screensaver] held off by hand control`. A screenshot of the panel shows the
kiosk's own strip under the window reading "Hand control has the camera —
pictures, calls and games come back when the browser closes", and the browser's
status bar showing a link URL under a pointer nobody touched.

**The bug this produced, and it was in the test.** `FakeHelper` took its
canned answer as `answer or <a success>`. `OpResult.__bool__` is its `ok` flag,
so a *refusal* handed to the fixture is falsey and was silently replaced by a
success — the test for "a refused URL is reported" was asserting on an open
page. Found because the refusal's log line never fired. `is None`, not `or`,
for any object that defines truthiness.

### 67. Kodama-Lite opening on its own, and it was the test suite

Reported again, and the previous fix was still in place: no `open_kodama`
tool, the launcher reached only by the Music button or by asking out loud, and
every launch logging who asked for it. The unit was `disabled` and `inactive`
and its journal was empty. And a `kodama-lite` process was running, twenty-two
minutes old, reparented to init.

**Started as a bare process, outside systemd**, which is only one line in this
project: `raise_window`, which runs `/usr/bin/kodama-lite` directly. Its
docstring explains at length why that is safe — Kodama-Lite is built with
`tauri-plugin-single-instance`, so a second launch hands its argv to the copy
already running and exits — and every word of it is true *while the player is
running*. With nothing to forward to, the same line is a cold start of an app
that resumes its previous queue and begins playing into the room.

`open()` did guard it. The guard asked the `player` object it was handed, and

```python
class StubPlayer(Plugin):        # tests/test_routing.py
    def available(self) -> bool:
        return True
```

is a test fixture that says the player is running. Four tests in
`TestTheLauncherItself` call `open()` on a launcher built with it, so four
passing tests started the music player — every time the suite ran on the Pi.
Including the run the *agent* makes: `_suite_passes()` is the gate before any
source change, so asking the agent to fix a typo started the music. That is
the "randomly".

Reproduced to be sure, with nothing else running:

```
$ ps -eo pid,cmd | grep -c "[k]odama-lite"
0
$ .venv/bin/python -m unittest tests.test_routing.TestTheLauncherItself\
      .test_already_open_is_not_a_restart
Ran 1 test in 0.005s — OK
$ ps -eo etime,cmd | grep "[k]odama-lite"
      00:04 /usr/bin/kodama-lite
```

**The fix is to stop asking an object.** `_binary_is_running` reads `/proc`
and matches argv[0] exactly, and `raise_window` returns without launching
anything when it finds nothing. A stub can report whatever it likes about the
session bus; it cannot invent a process. Not `pgrep -f`, which matches the
command line of the shell that ran it — the same trap `pkill -f` sets, and one
this project has already lost an ssh session to. On Windows there is no
`/proc`, so it returns False and the laptop and the Pi take the same branch
instead of one of them starting a music player.

Second lock, in the tests: `setUp` patches `subprocess.Popen` for the whole
class, so the next test somebody adds there is covered without their having to
know any of this. Verified after: the full suite on the device, before and
after, with no Kodama process either side.

### 68. The agent console on the touchscreen

Asked for, after the browser work: the console had been the phone's alone
(`aipi5/call/web/phone.html`), and that was a decision rather than an accident
— a chat surface on a panel with no keyboard is a chat surface nobody can use.

**What changed the decision is dictation.** `squeekboard` is installed and
running on this device, but a page that autofocuses a text field raised
nothing — measured, on the compositor and the Chromium this device actually
has — and even if it had, there is no pinyin IME on it, so half of what gets
said in this house could not have been typed. The device has a bilingual
recogniser three feet from the person's face, so the microphone button is the
input and the box is what you correct by saying it again.

`aipi5/ui/dictation.py` is a rendezvous, not a queue, and that is the whole
design. `UiState` is a queue because a button press is fire-and-forget; this
has an answer only the caller wants. It waits **without touching the
microphone**: ALSA allows one open, a second reader is not a race but an
assistant that stops hearing, so the HTTP thread leaves a request, the voice
loop notices it on the next frame, and fills the answer in. Both sides can
give up alone — a request carries a deadline, the caller clears its own slot,
and the loop refuses one that has already expired rather than taking the
microphone to answer nobody.

`dictate()` in `main.py` is a turn's first half and none of its second: no
router, no model, nothing spoken. Routing it would mean the assistant
answering a question that was put to the agent.

Three things this turned up:

**It runs before the wake detector is fed.** The capture eats the frames the
detector would have read, and a detector part-way through a phrase then acts
on the half it kept — the same reason the call branch skips `detect` rather
than discarding its answer.

**No `begin_turn`.** The elapsed time here is somebody talking, which is not
latency and is not bounded by anything this code does. Measured before it was
taken out: a capture with nobody speaking logged `turn 4201ms to audio [OVER
by 1701ms]`. A journal full of verdicts that mean nothing is a journal people
stop reading, which is the failure `Turn.judged_ms` exists to prevent. Two
honest numbers are logged instead — how long somebody spoke, and how long the
recogniser took.

**`hidden` loses to `display: flex`, silently.** The attribute's `display:
none` is a user-agent rule and this page's own `button { display: flex }`
beats it, so Stop sat in the header of a console with nothing running. Found
by screenshotting the panel rather than by reading the page. One line —
`button[hidden] { display: none; }` — and every hidden button on this page,
now and later, depends on it.

**Approvals are answerable here**, which is a decision and not an oversight:
anyone who can touch the panel can now approve a settings or a source change.
It is consistent with what a touch already means on this screen, which reaches
the settings page and a shutdown countdown, and the alternative was a run
started at the screen that stops halfway until somebody finds a phone.

What the page may send is three messages — `agent.ask`, `agent.stop`,
`agent.answer` — checked against a frozenset in `aipi5/ui/server.py`.
`agent.gesture` is absent because it has its own route with its own bounds,
and `agent.open` because it is the *assistant's* message about a page somebody
asked for out loud; reachable from a text box it would be a URL bar with a
language model in it.

Verified on the panel: nine buttons still fit the home row, the console drew
the real transcript out of the agent's mailbox (three runs, their tool calls
and their `2 steps · 1 checks · 5s` summaries), and `POST
/api/agent/dictate` came back in 4.1 s with "I didn't catch that." after
nobody spoke, logging `dictation: no speech in the capture window (4056 ms)`
and no budget verdict.
