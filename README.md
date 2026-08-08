# AIPI5 — a conversational voice assistant for the Raspberry Pi 5

Wake word → speech → intent router → **either** a music command in nine
milliseconds **or** a conversation with GPT — plus weather, local news, bedtime
stories, a camera that can describe the room, local person detection, and a
1280×800 touchscreen that gives way to a clock when nobody is there.

**Status: deployed and running on `aipi5.local`.** 130 tests pass on the Pi as
well as off it. Verified on the device: SenseVoice loads, both Piper voices
speak, the wake model loads, the OpenAI model answers, live weather and local
news reach the speaker, Kodama-Lite starts by command and answers over MPRIS,
and the router matches in 9.7–22.7 ms.

Verified in the room, not just in tests:

* **English** — "Stop the music." woke it, transcribed in 187 ms, routed to
  `kodama.pause` at 0.86. 256 ms of work either side of the person speaking.
* **Mandarin** — `现在天气怎么样？` transcribed in 191 ms, answered through the
  weather tool in spoken Chinese.
* **Vision** — asked what it could see, it described the person in front of it,
  and noticed when a sheet of paper was held up to the lens.
* **Person detection** on the AI HAT+ 2 at **28 ms** an inference, driving the
  screensaver through a full engage → clear → return cycle.

**Not yet done:** Cantonese has not been spoken to it, and twenty-one of the
twenty-two Kodama commands are covered by routing tests but have not been said
out loud. `REPORT.md` §25 has the full list.

## It is AIA with a conversational layer, not a fork of it

This is the single most important thing to understand about the repository, and
it is why it is so small.

[AIA](https://github.com/xiabo-lab/AIA) already solved the hard half. Its wake
word, microphone capture, VAD endpointing, SenseVoice recognition, Piper
synthesis, phrase router and twenty-odd Kodama-Lite commands were measured on
this exact Pi, this exact microphone and this exact room, and the numbers in
its config are load-bearing: mixer gain 8/30 because 12 dB of headroom is what
the endpointer needs; `min_speech_ms` at 500 because every missed capture held
300–660 ms of voiced audio while a real phrase holds 1140–1290; `save_lyrics`
at a raised floor of 0.90 because SenseVoice heard "search lyrics" as
"Se lyrics." and that scores 0.889 against saving.

So AIPI5 **imports** all of it. `aipi5/core/aia_bridge.py` finds an AIA checkout
and puts it on `sys.path`; the voice path in `aipi5/main.py` is AIA's objects,
constructed and driven. Copying would have meant those measurements immediately
beginning to drift from the file they were measured into, and the way that
drift presents is an assistant that mishears one command in four with no diff to
point at.

What is new is the fork AIA left for later. In AIA, an utterance the router
declines is repeated back — "You said: …" — and its README says plainly that
the LLM is not built. Here that branch goes to OpenAI with tools.

```
Microphone → Wake word (小艾同学, AIA's Vosk matcher)
                  ↓
            VAD endpointing                          } all of this
                  ↓                                  } is AIA's,
       STT (SenseVoiceSmall INT8, zh/en/yue)         } unchanged
                  ↓
         ┌─── Intent router ───┐
         │                     │
   fast path (~9 ms)      slow path — NEW
   phrase match against   OpenAI + tools
   plugin manifests            │
         │              ┌──────┴───────┬────────┬─────────┐
         │            weather        news    camera    Kodama
         │                                  (vision)  (interpreted)
         └──────── Piper TTS (AIA's) ───────┴────────┴─────────┘
                  ↓
              Speaker
                  ↓
   1280×800 touchscreen ──→ screensaver when the room is empty
        ↑
   Camera Module 3 → person detection on the AI HAT+ 2 (local, never uploaded)
```

## What it can do

Everything AIA could, unchanged and verified by test — play, pause, next,
previous, stop, what's playing, search, song search, volume, shuffle, repeat,
like, show/search/save lyrics, karaoke, go home, play local, play liked, close
Kodama, shut down, reboot, network status — in English and Mandarin, decided per
utterance, with Cantonese understood and answered in Mandarin.

Plus:

| say | what happens |
|---|---|
| "open the music player" / "打开音乐播放器" | starts Kodama-Lite through its systemd unit and waits for it to reach the bus |
| "what's the weather" | live Open-Meteo for San Jose 95127, cached ten minutes |
| "what's the local news" | San Jose / Santa Clara feeds, interleaved and de-duplicated, summarised to 3–5 stories |
| "what time is it" | the device's clock — never the model's guess |
| "what do you see" | one fresh frame from Camera Module 3, described by the vision model |
| "tell me a bedtime story about a dragon" | a child-safe story of a length you can ask for |
| anything else | a conversation, in about 60 words, in the language you asked in |

## Two things worth knowing before you deploy

**The model is `gpt-5.6-luna`, not the `GPT-5.6-Terra` the specification
names.** GPT-5.6 ships in three tiers and the specification picked the middle
one; this deployment runs the cheapest, because of what this assistant's turn
actually looks like. Per million tokens, input/output:

| tier | id | price | ~cost here |
|---|---|---|---|
| Sol | `gpt-5.6-sol` | $5.00 / $30.00 | ~$16/mo |
| Terra | `gpt-5.6-terra` | $2.00 / $12.00 | ~$6.50/mo |
| **Luna** | **`gpt-5.6-luna`** | **$0.20 / $1.20** | **~$0.65/mo** |

At roughly thirty turns a day. A turn here is a ~1,250-token cached prefix (the
system prompt and the tool schemas), a one-sentence question, five tools with
enum-constrained arguments, and a reply capped at about sixty words because
everything said is read aloud. Luna supports vision and tool calling, which are
the only capabilities beyond plain text this project needs.

Latency is the better argument. The model is on the *slow* path by design —
every known command is matched in ~9 ms and never reaches it — so the API round
trip is the whole of what a person waits for when they ask an open question, and
Luna is the tier tuned for that.

Move to `gpt-5.6-terra` if answers come back thin, particularly for bedtime
stories, which are the one thing here judged on prose rather than on being
correct and short. One line of YAML, about six dollars a month.

Whatever the name, a model the API rejects is a degraded mode and not a boot
failure — the client sends one real completion at startup and reports the
outcome by name, while every Kodama command, the weather, the clock, the camera
and the screensaver keep working.

**AIA and AIPI5 cannot both run.** The microphone allows exactly one reader.
`systemd/aipi5.service` declares `Conflicts=aia.service` and the installer
disables AIA, because AIPI5 *is* AIA's voice loop with a layer on top rather
than a second assistant.

## Installing

On the Pi, with an AIA checkout already working at `~/AI_Assit`:

```bash
# System libraries. Every one of these fails silently or confusingly if it is
# missing, which is why they are first — all four were found the hard way
# during the first real deploy.
sudo apt install pipewire-alsa    `# without it every reply is synthesised and never heard` \
                 libportaudio2    `# sounddevice is a binding, not the library` \
                 playerctl        `# MPRIS transport control` \
                 python3-picamera2 hailo-all

git clone <this repo> ~/AIPI5 && cd ~/AIPI5
python3 -m venv .venv

# picamera2 and hailo_platform are Debian packages with no usable wheel, so the
# venv has to be able to see them. Without this the assistant starts *degraded*
# rather than failing — no camera, no person detection, no screensaver — which
# is the kind of missing that takes a day to notice.
sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' .venv/pyvenv.cfg

.venv/bin/pip install -r ~/AI_Assit/requirements.txt   # the voice path
.venv/bin/pip install -r requirements.txt              # PyYAML and openai

./scripts/get_person_model.sh    # finds the HEF matching the fitted accelerator
./scripts/check_hardware.sh      # verify before installing — read all of it
./scripts/install-service.sh
```

`check_hardware.sh` reports the things that fail *silently* later: a missing
`pipewire-alsa`, a microphone whose gain is near full scale (every utterance
runs to the 10 s cap), a Hailo device `hailortcli` cannot reach.

AIA has to be there first — this project imports it. If it is not:

```bash
git clone https://github.com/xiabo-lab/AIA.git ~/AI_Assit && cd ~/AI_Assit
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./scripts/get_sensevoice.sh && ./scripts/get_wake_model.sh
# Piper and its two voices — bench_m0.sh fetches them, or directly:
curl -fL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz | tar -xz -C vendor
```

The OpenAI key is read from `OPENAI_API_KEY` first and from a key file beside
the project second. Both are in `.gitignore`, and a test asserts that every
filename the loader can read is — see `test_every_key_filename_is_gitignored`.

`install-service.sh` also puts an **AI Assistant** launcher on the desktop and
in the applications menu. It starts the *service* rather than the browser —
launching the script directly would put a second Chromium on the display behind
systemd's back, sharing the profile with the managed one. So it is really a
re-start button: the way back after `systemctl --user stop aipi5-ui`, which is
the only exit from a full-screen window on a display with no keyboard.

One thing the installer does not touch, because it is a system preference
rather than an application setting: **output volume**. The Pi's HDMI sink can
sit at 50% with ALSA already at 100%, which sounds like a quiet assistant and
is not one. Check it with:

```bash
wpctl get-volume @DEFAULT_AUDIO_SINK@
wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0    # WirePlumber persists this
```

## Running it by hand

```bash
systemctl --user stop aipi5          # the microphone allows one reader
.venv/bin/python -m aipi5.main
```

| variable | effect |
|---|---|
| `AIA_HOME` | where the AIA checkout is (default `~/AI_Assit`) |
| `AIA_NO_WAKE=1` | no wake word; any speech is a command |
| `AIA_DEBUG=1` | debug logging |
| `AIPI5_NO_UI=1` | no web UI — the voice loop does not depend on it |
| `AIPI5_NO_LLM=1` | behave exactly like AIA: decline instead of answering |

The screen is at **http://127.0.0.1:8092/**, loopback only, and
`scripts/aipi5-ui.sh` opens it full-screen in Chromium. Forward it to a laptop
with `ssh -L 8092:127.0.0.1:8092 aipi5.local`.

## The screen

1280×800, designed for this panel and no other — section 22 of the
specification is explicit that the old 1920×440 geometry must not be inherited,
and AIA's layer-shell strip is not used here.

The main screen is the conversation, a status line, and five buttons. The
buttons are why this UI is not read-only the way AIA's is: each posts a name
from a fixed tuple into a queue and the voice loop decides what it means.
Nothing in that tuple is destructive — shutting down, restarting and closing the
player stay spoken commands that are confirmed out loud, because a button
cannot hold that conversation.

The screensaver is the full display: a clock that redraws every second from the
browser's own clock, corrected against the Pi's, and the current San Jose
weather refreshed every ten minutes. It comes up 60 seconds after the room
empties and goes away the moment somebody returns — no touch required, which is
section 26. Speaking to the device from outside the camera's view takes it down
too.

## Person detection

Local, on the AI HAT+ 2, and nothing it sees leaves the device. At two frames a
second, uploading would be about 172,000 requests a day of a room somebody
lives in — unacceptable on privacy grounds before it is unacceptable on cost.

Three backends behind one interface: `hailo` (YOLOv8n HEF through HailoRT),
`cpu` (SSD-MobileNet through onnxruntime, about a third of a core), and
`disabled`. **There is deliberately no automatic fallback from `hailo` to
`cpu`** — falling back would turn "the accelerator is not working" into "the
assistant is mysteriously using a core it did not used to", which is the kind of
regression discovered months later from a thermal graph.

The debounce is asymmetric: two consecutive frames to arrive, eight to leave.
Walking up to the device should feel immediate; walking out of shot for a moment
must not take the screen away. Both numbers are configuration and both should be
tuned on the actual Pi.

## What the model may and may not do

`aipi5/llm/tools.py` is the security boundary and is worth reading in full — it
is one screen. The model never executes anything: it emits a tool name and a
JSON blob, the name is looked up in a fixed dictionary, the arguments are
validated, and a Python function is called. There is no path from model output
to a shell, a filesystem path, a URL, or an argument interpolated into a
command line.

The Kodama commands it may reach are filtered on AIA's `confirm` flag rather
than on a deny list, so a destructive command added to AIA tomorrow is excluded
the moment it is declared. `tests/test_tool_safety.py` asserts the mechanism as
well as today's outcome.

## Tests

```bash
python -m unittest discover -s tests -t .    # 119 tests, no hardware needed
```

No microphone, no camera, no accelerator, no network, no API key. That
constraint is what decided which parts of this project are pure functions — the
presence debounce, the screensaver timing, the feed parsing, the weather cache,
the conversation trimming and the tool gate are all exactly the parts where a
mistake is silent on the device.

Two of them have already earned their keep. `test_the_mandarin_phrases_keep_
their_measured_margin` disproved a claim in a code comment that was wrong by
0.19; `test_respects_the_limit` found that headlines carrying one
distinguishing word are collapsed as duplicates.

## Layout

```
aipi5/
  core/       aia_bridge (finds AIA), config (YAML), presence, preflight
  llm/        the OpenAI client, bounded conversation, tool schemas, prompts
  tools/      weather, news, clock, bedtime stories
  vision/     camera (shared by both readers), description, person detection
  kodama/     the one command AIA does not have: open the player
  ui/         the 1280×800 page, its server, and the shared state
config/       aipi5.yaml — the settings a person changes
scripts/      hardware check, model fetch, service install, the kiosk browser
systemd/      the user units
tests/        the off-device testable part
REPORT.md     the engineering report section 38 asks for
```

Nothing here duplicates AIA. If you are looking for the wake word, the
recogniser, the synthesiser, the router or the music commands, they are in
`~/AI_Assit` and this project imports them.
