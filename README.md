# AIPI5 — a conversational voice assistant for the Raspberry Pi 5

Wake word → speech → intent router → **either** a music command in nine
milliseconds **or** a conversation with GPT — plus weather, local news, bedtime
stories, a camera that can describe the room, local person detection, and a
1280×800 touchscreen that gives way to a clock when nobody is there.

**Status: deployed and running on `aipi5.local`.** 412 tests pass on the Pi as
well as off it. Verified on the device: SenseVoice loads, both Piper voices
speak, the wake model loads, the OpenAI model answers, live weather and local
news reach the speaker, Kodama-Lite starts by command and answers over MPRIS,
and the router matches in 9.7–22.7 ms. The Logitech Brio 101 opens on
`/dev/video0`, the Hailo detector reads it every 500 ms, and “what do you see”
goes camera → vision model → speaker in about six seconds.

Verified in the room, not just in tests:

* **English** — "Stop the music." woke it, transcribed in 187 ms, routed to
  `kodama.pause` at 0.86. 256 ms of work either side of the person speaking.
* **Mandarin** — `现在天气怎么样？` transcribed in 191 ms, answered through the
  weather tool in spoken Chinese.
* **Vision** — asked what it could see, it described the person in front of it,
  and noticed when a sheet of paper was held up to the lens.
* **Person detection** on the AI HAT+ 2 at **28 ms** an inference, driving the
  screensaver through a full engage → clear → return cycle.

Since then, four things the specification left for later have been built and
verified on the device:

* **A video call in both directions.** A phone rings the Pi from a cellular
  network and it answers; the Pi rings the phone through Web Push and waits for
  a tap. Measured over 5G: `host/udp -> prflx rtt 34ms`, no relay.
* **File transfer.** A folder the phone can put photos and video into and take
  them out of, over the same HTTPS and the same token. A 220 MB upload moved
  the service's memory by +0.0 MB.
* **A shutdown that can be stopped.** "Shut down" now counts 3, 2, 1 on the
  screen and a touch anywhere cancels it, because `poweroff` takes the audio
  stack with it and a spoken confirmation is a question this device cannot
  finish asking.
* **A weather dashboard** on the touchscreen: current conditions, an hourly
  strip, seven days, the sun's position through the day, and whether to go
  outside — the last decided by rules rather than by a model, so the page
  renders the instant it opens.

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
   Logitech Brio 101 (USB) → person detection on the AI HAT+ 2 (never uploaded)

   Main page ─┬─→ Talk     conversation, and only conversation
              ├─→ Call     the remote video call — a phone rings, the Pi answers
              ├─→ Camera   live preview + the answer over the picture
              ├─→ News     today's Bay Area stories
              ├─→ Weather  today, in detail
              └─→ Music ──→ Kodama-Lite (a separate app, raised not relaunched)
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
| "what do you see" | one fresh frame from the Brio 101, described by the vision model |
| "tell me a bedtime story about a dragon" | a child-safe story of a length you can ask for |
| anything else | a conversation, in about 60 words, in the language you asked in |

…and six buttons, each of which opens its own page:

| press | what opens | what it says |
|---|---|---|
| **Talk** | the conversation, and nothing else on it | starts listening, as the wake word does |
| **Call** | the remote video call page, full screen | nothing — the call has its own two directions, below |
| **Camera** | a live preview, with the answer drawn over the picture and faded ten seconds after the speaking stops | what it sees |
| **Weather** | the dashboard: now, hourly, seven days, sun, and whether to go out | the sky, the range, and at most one thing worth acting on |
| **Music** | Kodama-Lite itself | whether it opened |
| **Files** | the transfer folder, to send and fetch | nothing — it is a folder, not a turn |

The news **page** was removed: it duplicated on a screen what the assistant
says better out loud, and the button with it. Ask for the news instead.

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
                 python3-opencv    `# the camera, over V4L2` \
                 hailo-all

git clone <this repo> ~/AIPI5 && cd ~/AIPI5
python3 -m venv .venv

# cv2 and hailo_platform are installed as Debian packages, so the venv has to
# be able to see them. Without this the assistant starts *degraded*
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

The main screen is the conversation, a status line, and six buttons. The
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

## The screen: six pages, six cooldowns

The buttons open **pages, not windows**. This is a Chromium kiosk on a
compositor with no title bars and no taskbar, so a second window would be a
page nobody could get back from; the six destinations are views in one
document, exactly one of them visible. It also makes "no duplicate instances"
a property of the design rather than a rule to enforce — a view is either the
current one or it is not, and navigating to the current one does nothing.

**Each button then ignores itself for ten seconds**, independently. A finger on
a capacitive panel produces repeats, and every one of these actions takes
seconds of real work: a capture and a vision request, a feed fetch, a Tauri app
cold-starting. Pressing Camera never disables Weather. The cooldown is enforced
on the server rather than only in the page — a reload cannot bypass it — and
the seconds remaining are published so the button can draw a countdown, because
a button that is merely dead reads as broken, which is exactly what makes
somebody press it again.

Verified on the device: ten rapid Camera presses produce one action, and all
five buttons pressed in a row produce five. (Measured before Call was added;
the equivalent test now covers six.)

**A button carries no language.** The four that speak answer in the configured
language, not in whichever one was last spoken to the device — that variable
follows the conversation, and following it here meant News and Weather answered
in Chinese for the rest of a session because somebody had said 关机 an hour
earlier, with nothing on screen to explain it.

**Files navigates without acting**, which is the shape any future button that
opens a folder rather than asking for work should take: no action queued, no
cooldown, nothing for the voice loop to do.

## Call: a phone rings the Pi and it picks up

**Off by default.** It is the one setting here whose failure mode is a camera
and a microphone reachable from the network rather than a feature that does not
work, so two things have to be true before anything listens: `call.enabled` in
the YAML, and at least one paired phone.

```bash
./scripts/pair-phone.sh "Fuwen's iPhone"    # prints a one-time link
./scripts/pair-phone.sh --list
./scripts/pair-phone.sh --revoke "Fuwen's iPhone"
```

**Both ends are browsers and Python never touches the media.** Chromium already
has the encoder, the congestion controller that lowers bitrate instead of
freezing, and — the part that cannot be added afterwards — an echo canceller
that works because one process owns both the capture and the playback stream.
The Brio capsule is inches from the speaker the caller comes out of, so that
last one is the whole audio-quality story. Python does signalling,
authentication, and deciding who owns the Brio.

There are two doors into one hub, and the reason is secure contexts:
`getUserMedia` refuses to run outside one, `http://` on a LAN address is not
one, and `http://127.0.0.1` is. So the Pi's page keeps talking to the existing
loopback server with no TLS anywhere, and only the phone needs a certificate —
which also means `aipi5/ui/server.py` stays loopback-only and unauthenticated,
and the single listener on the network is `aipi5/call/server.py`, where every
route authenticates before it does anything.

**Auto-answer is the absence of a prompt, not a rule.** The token is checked at
the door, so an unknown caller never reaches a state the screen can see and
there is no Accept button to skip. Ringing deliberately does not own the
camera: an authorised caller who has dialled but not been picked up has still
turned nothing on.

Verified on the device: unauthenticated requests get 401 and five failures lock
an address out for five minutes; a ring is answered in about a second with no
touch; the call runs the Brio at **1280×720 MJPEG at 30.000 fps**, which is the
only configuration this camera can actually deliver; `/dev/video0` belongs to
`chromium` during the call and to `python` a second after it ends; and AIA
keeps the TI microphone throughout, so the assistant never goes deaf.

Two things about the Pi that this feature needed and that are worth knowing:

**Chromium cannot open a camera on this Pi without help.** It prefers the
xdg-desktop-portal `Camera` interface, and no portal backend installed here
implements it — so `getUserMedia` does not fail, it never returns.
`scripts/aipi5-ui.sh` passes `--disable-features=PipeWireCamera,
WebRtcPipeWireCamera` to send it to V4L2 instead. Chromium keeps only the last
`--disable-features` flag, so it is merged with `TranslateUI` rather than added
beside it.

**A changed `index.html` needs `systemctl --user restart aipi5`, not
`aipi5-ui`.** The server reads the page once and holds it, so restarting the
browser reloads the same bytes out of the assistant's memory.

**Confirmed working from an iPhone on 2026-08-11**, same network. Two things
that test did not measure and that want the room rather than the log: whether
the echo canceller actually stops the caller hearing themselves with the
speaker at a normal volume, and the latency as a number. Both are worth doing
before a relay is added, since a relay can only make them worse.

### Calling from outside the house

A phone on a cellular network cannot address a Pi behind a home router, and
opening a port is what this feature is supposed to avoid. The answer here is a
**Tailscale tailnet**: both devices join one, and the phone reaches the Pi at a
name that works from anywhere.

```bash
./scripts/setup-tailscale.sh          # check, and print what is left
./scripts/setup-tailscale.sh --apply  # install the client
sudo tailscale up                     # yours to run — it needs your account
sudo tailscale serve --bg --https=443 http://127.0.0.1:8443
```

Two steps stay yours deliberately. `tailscale up` authenticates the machine
against your account, and `tailscale serve` publishes a camera and a microphone
to everyone on your tailnet — a decision about who can see into the room, not a
detail of a deployment. Serve and HTTPS certificates both have to be enabled
for the tailnet first, in the admin console under **DNS**.

Deployed here, and it is **safer than the home-network arrangement it
replaces**:

```
iPhone (any network) ──tailnet──▶ aipi5.<tailnet>.ts.net:443
                                  tailscale serve  (real Let's Encrypt cert)
                                        │ proxies to
                                        ▼
                                  127.0.0.1:8443   the call server
```

With Tailscale terminating TLS, `host: 127.0.0.1` and `tls: false` are correct:
the call server stops accepting connections from the network altogether — `ss`
shows it on loopback only, where it used to be on `0.0.0.0` — and the only way
in is through the tailnet. The certificate warning goes with it, which is worth
more than the convenience, since the habit it was building was clicking through
certificate warnings. AIPI5 **refuses to start** with `tls: false` on any
non-loopback address, because a bearer token over plaintext is a bearer token
anyone on the path can read.

**Echo cancellation is deliberately not installed.** A PipeWire canceller was
tried and its output measured as digital silence — the caller heard nothing at
all, while every other signal said the call was healthy. It is removed, and
`scripts/setup-echo-cancel.sh` carries a warning saying why. Echo has never
been confirmed as a problem in normal use: it was heard only with the phone in
the same room as the Pi, which is an acoustic loop no canceller can win. The
call now reports `level=` and `energy=` per call, so a silent microphone is a
line in the journal rather than something a caller discovers.

Putting a connection-pooling proxy in front exposed three bugs that were
already present and invisible without one — an unread POST body corrupting the
*next* request on the same connection, a timed-out call never giving the camera
back, and the rate-limiter counting the proxy instead of the caller. All three
are fixed and covered by tests; `REPORT.md` §27c has the detail, and the first
is worth reading if you ever add a route here.

**STUN and TURN are configured and empty, and measurement says they can stay
that way.** A call from an iPhone on 5G, Pi behind the home router:

```
call route: host/udp -> prflx rtt 34ms           (the Pi)
call route: phone prflx/udp -> host rtt 37ms     (the phone)
tailscale:  active; direct <phone's cellular address>   (underneath)
```

No `relay` at either layer — WireGuard punched straight through to the phone's
cellular address, and WebRTC paired the two tailnet addresses peer to peer on
top. Media crosses the Internet with nothing in the middle, at conversational
latency. `scripts/setup-turn.sh` stays for a less cooperative carrier; nothing
needs standing up today.

Don't guess at this — both pages report the selected candidate pair after every
connect, always, even when they cannot find one:

```bash
systemctl --user status aipi5 -n 50 | grep 'call route'
```

`host` is direct, `srflx` went via STUN, `relay` is being carried by a server.
If TURN is ever needed, credentials are minted per call from a shared secret
and expire within the hour, so nothing reusable reaches the phone.

`REPORT.md` §27a has the Brio's measured formats, §27b the call architecture
and §27c phase 3.

## Files: a folder the phone can reach

The same HTTPS the call uses, the same bearer token, and one folder —
`~/Downloads/AIPI5`, outside this repository. Photos, video and documents go
both ways: the phone uploads through Safari with real progress, the Pi's own
screen lists what arrived and can open pictures, video and sound over the list.

Three properties, each of which is a test:

* **Nothing is ever buffered whole.** `cgi` was removed in Python 3.13 and
  `email.parser` wants the entire body, so `aipi5/files/multipart.py` is a
  streaming reader that holds back the tail of its buffer — a boundary split
  across two reads cannot corrupt a file, and that is tested at every offset.
  A 220 MB upload at 40 MB/s moved the service's memory by +0.0 MB.
* **An upload never wears its final name until it is whole.** It arrives in
  `.name.<pid>.uploading` and is `os.replace`d into position, so a name in the
  listing is a complete file. A killed connection leaves neither.
* **The folder is the boundary, enforced by resolution.** A name is reduced to
  a bare filename, joined, then *resolved* — symlinks and all — and must still
  be underneath the root. Checked against the running device in nine spellings
  on three routes: `../../etc/passwd`, `%2e%2e`, `..%2f`, `....//`, a raw `../`
  in the request line. None of them reached anything.

Getting a file *onto* an iPhone took three attempts and the first two trap the
person in the app; `phone.html` records all three, because the next person will
reach for the same two dead ends first.

## Shutdown: three seconds and a touch

Every other destructive command here is answered out loud. Shutdown cannot be,
and not as a matter of taste: `poweroff` takes the audio stack down with it, so
the one command this device cannot narrate is the last one it runs.

So it is answered by presence. The screen shows **3, 2, 1** and a touch
anywhere cancels. Two rules hold it together, both failing towards a device
that stays on: it **fails closed** — if nothing says the countdown is on a
screen within two seconds, the shutdown does not happen — and the clock starts
when the screen says it is drawing it, so the poll does not eat the first
second of somebody's three.

`aipi5/core/shutdown.py`, and it reads the command's *name* rather than AIA's
`confirm` flag: the two AIA checkouts in this house disagree about that flag
and the behaviour must not.

## The assistant's voice outranks the music

When the assistant speaks, Kodama is **paused** and then resumed where it
stopped. Pausing over MPRIS rather than muting is the whole point — a muted
song keeps playing and loses the seconds it was silent for. AIA's `Ducker`
already did this around a spoken turn; what is new is that the buttons and
pages go through it too, and that it is re-entrant.

The re-entrancy is not decoration. `Ducker.duck()` starts by forgetting what it
previously paused, so two overlapping ducks — a button pressed mid-turn, a page
speaking while the voice loop holds the floor — leave the music paused with
nothing left to resume, and nothing in the log to say when. `AudioPriority`
counts holders and only the outermost touches the bus.

Measured on the device: playing at 106 s, paused at 108.7 when the Weather page
spoke, playing again nine seconds later at 108.9.

## Weather: a dashboard, and rules rather than a model

The provider is unchanged — Open-Meteo, no key, no account — but it is asked
for more: hourly readings, seven days, sunrise and sunset, pressure, and this
hour's chance of rain. Two payload shapes, deliberately. `as_dict()` goes into
the state the page polls twice a second and into the model's context on every
weather question; `as_page_dict()` carries the hourly strip and is fetched only
by the page that draws it. Thirty-six hours of readings in the first would be a
few hundred tokens and a few kilobytes a second for one card.

The four judgements — UV, outdoor, umbrella, clothing — and the "should I go
outside" line are **rules** in `aipi5/tools/advice.py`. Not because a model
would answer badly, but because the page must render the moment it opens on a
device that may be on a hotspot, and because the same weather should always
give the same advice. Rain already falling outranks any probability; a missing
UV reading is not a low one.

The rest is a screen: glass cards on a sky that follows the conditions, a very
large current temperature, °F/°C converted locally and remembered, and the last
good reading kept on screen with *"Offline — showing cached weather"* when the
network goes. It fits 1280×800 with no scrolling — measured, not assumed, which
is how a card that was 291 px in a 258 px row was caught being drawn over the
row beneath it.

## The camera

A **Logitech Brio 101 on USB**, which replaced the CSI Camera Module 3 this
project was originally written for. One `cv2.VideoCapture` on a V4L2 node,
opened once at startup and shared under a lock by the two things that want
frames — the person detector twice a second, and the vision question when
somebody asks. Opening it twice would fail with a device-busy error at exactly
the moment somebody spoke to it.

Two consequences of the change are visible in `config/aipi5.yaml`:

`device: auto` finds the camera **by name**, not by node. `/dev/video0` is not
a stable identity — the Brio claims more than one node, one of which opens
cleanly and never yields an image, and the order depends on what else was
plugged in at boot. This is the same reasoning AIA applies to matching the
microphone by name rather than by card number. Name an explicit node to
override the search; a named device is never second-guessed.

Frames are **drained before one is decoded**. V4L2 is a queue and hands back
the oldest buffer the driver filled, so on a 30 fps stream polled twice a
second the naive read is always stale and the staleness compounds. Grabbing
without retrieving discards buffers without paying for a JPEG decode, so
`Camera._read` walks to the end of the queue and decodes only there. A person
who arrived four seconds ago is a screensaver that lifts after they have left.

The Camera Module's two-stream trick — a full-size still and a small detection
frame produced from one sensor read — is gone, because UVC gives one stream at
one size. It cost nothing: the detector resizes to its model's input as its
first step, so the second stream was only ever pixels it threw away.

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
python -m unittest discover -s tests -t .    # 412 tests, no hardware needed
```

No microphone, no camera, no accelerator, no network, no API key. That
constraint is what decided which parts of this project are pure functions — the
presence debounce, the screensaver timing, the feed parsing, the weather cache,
the conversation trimming and the tool gate are all exactly the parts where a
mistake is silent on the device.

Four of them have already earned their keep. `test_the_mandarin_phrases_keep_
their_measured_margin` disproved a claim in a code comment that was wrong by
0.19; `test_respects_the_limit` found that headlines carrying one
distinguishing word are collapsed as duplicates; the multipart tests caught a
boundary split across two reads; and the file tests caught the suite itself
writing into the developer's real `~/.config/aipi5` and reading it back in the
next test.

## Layout

```
aipi5/
  core/       aia_bridge (finds AIA), config (YAML), presence, preflight,
              shutdown (the countdown that can be cancelled)
  llm/        the OpenAI client, bounded conversation, tool schemas, prompts
  tools/      weather, news, clock, bedtime stories, advice (the weather rules)
  vision/     camera (one V4L2 handle, shared), description, person detection
  kodama/     the one command AIA does not have: open the player
  call/       signalling, device tokens, TLS, push, the phone's page
  files/      the transfer folder, a streaming multipart reader, download tickets
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
