# AIPI5 — engineering report

The report section 38 of the implementation procedure asks for, written against
what has actually been done on the device rather than against what the code
intends.

**Deployed and running on `aipi5.local`.** All ten startup checks pass; 144
tests pass on the Pi as well as off it. Verified in the room: a spoken command,
live weather and local news through the speaker, a camera description of the
actual room, person detection on the accelerator, and the screensaver
completing a full engage / clear / return cycle. What has *not* been done is in
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

Camera Module 3 ─→ person detection (AI HAT+ 2, new) ─→ presence ─→ screensaver
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
aipi5/vision/camera.py            one Picamera2, two streams
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
tests/ (10 modules, 144 tests)
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

## 14. Camera implementation

`aipi5/vision/camera.py`. **One `Picamera2` with two streams** — `main` at
1280×720 for the still, `lores` at 640×480 for the detector — because the camera
allows one owner and two consumers want frames. libcamera produces both from the
same sensor read.

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
9. **The venv could not see system packages.** `picamera2` and
   `hailo_platform` are Debian packages with no usable wheel, so a venv built
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

**Design limits, unchanged and deliberate:**

16. The `interval_ms` sleep can drift when an inference takes longer than the
    interval; the remainder is what is slept, so it degrades to inference time.
17. The button queue is depth 2 — tapping while the assistant speaks drops
    presses with a debug line.
18. Story length is a target, not a contract. Nothing truncates, because
    truncation is read aloud as a sentence stopping mid-word.
19. No Cantonese Piper voice. Inherited from AIA: Cantonese is recognised as
    Cantonese and answered in Mandarin.
20. The UI accepts input, unlike AIA's. Mitigated by a fixed action tuple with
    nothing destructive in it.

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
python -m unittest discover -s tests -t .      # 119 tests, anywhere
curl -s localhost:8092/api/system | python -m json.tool   # live settings
ssh -L 8092:127.0.0.1:8092 fuwenxu@aipi5.local           # the screen, remotely
```

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
