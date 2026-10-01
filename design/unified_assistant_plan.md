# AIPI5 unified assistant implementation plan

This document is an implementation handoff for Codex or Claude Code. It is
based on the repository at commit `1d47584` on branch
`feature/open-youtube-and-the-kiosk-agent-console`.

## 1. Product decision

Combine **Talk** and **Agent** into one user-facing **Assistant** page and one
assistant identity. Do **not** combine the two Linux processes or remove the
privileged helper boundary.

The target experience is:

1. The user says the wake word, `小爱同学`.
2. AIA captures and transcribes the utterance locally, as it does now.
3. Existing exact, high-confidence commands can still take the fast path.
4. Everything else goes to the OpenAI model with typed tools. The model chooses
   the tool from ordinary language; the user does not memorize a command list.
5. Short device actions finish in the voice process and are spoken immediately.
6. Long investigations or maintenance tasks are delegated to the existing
   sandboxed agent service and stream progress into the same Assistant page.
7. The page shows conversation, tool progress, pictures, approvals, reminders,
   and long-running work in one chronological surface.

The OpenAI model is the **planner and intent selector**. It is not a shell and
must not receive arbitrary filesystem, process, URL, or command execution.
Every real-world capability remains a small validated adapter.

## 2. What exists today

### Voice/Talk path

`aipi5/main.py` owns AIA's microphone, wake detector, VAD, SenseVoice STT,
Piper TTS, phrase router, and the short OpenAI tool loop. It is a user service
because it needs the session audio and MPRIS buses.

The wake detector already accepts both `小爱同学` and `小艾同学` as acoustic
variants in the adjacent AIA checkout. AIPI5's visible title uses `小爱同学`,
while several hints and documents still use `小艾同学`.

When the fast router declines an utterance, `Assistant.answer()` sends it to
`OpenAIClient.respond()`. The model can currently call only:

- `get_weather`
- `get_local_news`
- `get_current_time`
- `describe_camera_image`
- safe, non-confirming Kodama commands while Kodama is already running

The current OpenAI client uses `/v1/chat/completions`, manually runs the tool
loop, and negotiates token and `reasoning_effort` parameters.

### Agent path

`aipi5-agent.service` runs separately under `aipi5-agent`, behind a Unix
socket. `aipi5-agent-helper.service` owns the narrow root boundary. The agent
can inspect health, read bounded logs/config/source, drive an isolated browser,
create reminders, remember household facts, and propose approved transactional
changes. Its default ceilings are 24 model steps, 40 tool calls, 15 minutes,
and 400,000 tokens.

This separation is load-bearing:

- restarting `aipi5.service` must not destroy the active agent run;
- the voice process must not gain root/helper privileges;
- the helper's root-owned allowlists must not move into editable YAML;
- code/config changes must retain approval, test, health-check, and rollback;
- the agent must not be able to edit its own runtime, installer, or systemd
  boundary.

### UI split

`aipi5/ui/web/index.html` contains two views:

- `page-talk`: voice transcript, Listen, and What do you see?
- `page-agent`: separate event log, dictation box, progress, stop, and approval

Talk reads `/api/feed`; Agent long-polls `/api/agent/poll`. The Agent compose
box sends `agent.ask`, while voice goes through the independent Talk path.

### Requested capabilities: current state

| Capability | Current state | Missing connection |
|---|---|---|
| Wake on `小爱同学` | Already accepted as an AIA wake variant | Standardize visible wording and add an end-to-end regression |
| Open YouTube | Exact English/Mandarin fast-path command exists | Offer a safe model tool for natural variations without weakening URL policy |
| Call my phone | Touchscreen outgoing call and Web Push exist | A validated voice/model adapter and confirmation policy |
| Remind me | Agent service has persistent reminders and delivery | Deterministic voice-facing RPC; do not launch a 24-step maintenance run |
| Add a birthday | Local atomic `BirthdayStore` and calendar UI exist | Typed model tools for add/list/update/delete and ambiguity handling |
| Change volume | Master `VolumeControl.set()` and AIA commands exist | Typed model tool for natural variations and read-back |
| Take a webcam picture | Camera captures a temporary still for vision | Persist a copy into the Files folder and publish it in the transcript |
| Bitcoin price | No implementation | A current-information tool/provider; never answer from model memory |
| Local news | RSS service and model tool exist | Keep it; do not replace a narrow reliable feed with browser automation |
| Play music | AIA/Kodama commands and launcher exist | Let the model interpret natural requests, while keeping explicit launch policy |
| Device diagnosis/change | Sandboxed Agent already exists | Delegate and render in the unified page |

## 3. Target architecture

```text
microphone / Assistant page / phone console
                    |
                    v
             Assistant coordinator
                    |
       +------------+-------------+
       |                          |
exact safe command          OpenAI Responses API
(AIA fast router)           model chooses typed tool
       |                          |
       |             +------------+-------------------+
       |             |            |                   |
       v             v            v                   v
 local action   current facts  persistent data   delegate_agent_task
 Kodama/volume  weather/news/  reminder/calendar       |
 games/browser  BTC/web search camera photo             v
       |             |            |             aipi5-agent.service
       +-------------+------------+                   |
                    |                                 v
                    +--------> unified events <--- approvals/helper
                                   |
                                   v
                            Assistant page + TTS
```

The coordinator is not another agent loop. It owns presentation, cancellation,
and the decision to delegate long work. Tool selection remains with the model.

## 4. Safety and interaction policy

Define policy in code beside each tool, not only in the prompt.

### No confirmation

Read-only operations and easily reversible local state:

- weather, news, current time, Bitcoin price;
- list reminders and birthdays;
- describe the current camera frame;
- set master volume, including mute/unmute;
- control an already-running Kodama player;
- open a known application/site explicitly named by the user.

### Spoken confirmation or on-screen approval

- ring a phone;
- delete/cancel a birthday or reminder when the target is ambiguous;
- open an arbitrary URL or a model-selected website not in the known-app map;
- send or publish anything to another person;
- expose camera/microphone to a newly opened page.

### Existing Agent approval card

Continue to use it for:

- `set_config`;
- `patch_file`;
- `restart_service`;
- any future privileged or difficult-to-reverse action.

Silence, a timeout, an unclear answer, or a stale approval token always means
**no**. Never allow the model's prose to count as approval.

## 5. Implementation phases

Make these separate commits. Keep the test suite green after every phase.

### Phase 0 — lock the baseline

1. Run the complete off-device suite:

   ```bash
   python -m unittest discover -s tests -t .
   ```

2. Record the exact pass/skip count in the commit message.
3. Do not read or commit `openai API.txt` or any deployed credential.
4. Preserve the current untracked Fruit Ninja skill/plan; it is unrelated
   user work.
5. Add a short architecture test that asserts the voice service still reaches
   the agent only through `AgentProxy`, never `HelperClient`.

Acceptance: no implementation change and the baseline is reproducible.

### Phase 1 — one Assistant page, same backends

Do the UI merge before changing model/runtime behavior.

Files:

- `aipi5/ui/web/index.html`
- `aipi5/ui/server.py`
- `tests/test_agent_page.py`
- `tests/test_kiosk_agent.py`
- `tests/test_camera_in_talk.py`
- `tests/test_ui_assets.py`
- README screenshots/documentation

Steps:

1. Rename the visible Talk heading to **ASSISTANT**. Keep `#talk` as a route
   alias for old bookmarks and tests for one release.
2. Remove the Agent button from the home grid and remove `page-agent` after its
   controls have moved.
3. Move these Agent controls below the Talk transcript:
   - compose textarea;
   - Speak/dictation button;
   - Send button;
   - Stop button;
   - current agent state;
   - approval card.
4. Render agent events into the existing conversation feed with compact types:
   - user request;
   - assistant answer;
   - collapsible/quiet tool progress;
   - run summary;
   - error;
   - approval.
5. Continue escaping all agent-originated text with `textContent`. Do not use
   `innerHTML` for logs, page text, tool output, or model output.
6. Keep `/api/feed` and `/api/agent/poll` initially. The browser may poll both
   and merge rows by timestamp. Do not redesign storage in the same commit.
7. Start/stop the agent long poll while the Assistant page is visible. Do not
   leave one long poll behind each page visit.
8. Keep camera captures in the same feed and preserve `drainFeed()` ordering.
9. Standardize all user-facing wake hints to `小爱同学`, while retaining both
   AIA acoustic variants.
10. Update the home-grid count and screenshots. The grid returns from nine
    destinations to eight.

Acceptance:

- only one Assistant destination exists;
- voice messages and agent run events appear on the same page;
- dictation still touches the microphone only in `main.py`;
- approvals work from the panel;
- changing pages does not duplicate polls or transcript rows;
- `#talk` still opens the unified page.

### Phase 2 — introduce one coordinator contract

Add:

- `aipi5/assistant/__init__.py`
- `aipi5/assistant/events.py`
- `aipi5/assistant/coordinator.py`
- `tests/test_assistant_events.py`
- `tests/test_assistant_coordinator.py`

Do not move every existing class into this package. It is an orchestration
layer, not a repository rename.

1. Define a small `AssistantEvent` record:
   - monotonically unique id;
   - timestamp;
   - `kind` (`user`, `assistant`, `tool`, `approval`, `capture`, `done`,
     `error`);
   - source (`voice`, `touch`, `text`, `agent`, `system`);
   - public text;
   - optional run/tool/capture metadata.
2. Define one event sink interface used by `main.py`, the Agent proxy adapter,
   and the UI server. Never put raw tool results or hidden model reasoning in a
   public event.
3. Make the coordinator expose:
   - `submit_text(text, source, language)`;
   - `submit_voice(text, language)`;
   - `cancel(run_id)`;
   - `answer_approval(token, allow)`;
   - `snapshot()`.
4. Keep exact AIA router matches in `main.py`; on a decline call the
   coordinator. This preserves offline/9 ms behavior without asking users to
   memorize exact phrases.
5. For typed text, call the same coordinator instead of always sending
   `agent.ask`. The coordinator/model decides whether it is a short action or
   a delegated long task.
6. Add `/api/assistant/events`, `/api/assistant/ask`,
   `/api/assistant/cancel`, and `/api/assistant/approval` as the unified UI
   contract. Keep the old Agent endpoints as compatibility wrappers until the
   phone page is migrated.

Acceptance: the same natural-language request has the same tool/policy whether
it came from wake voice, touchscreen dictation, keyboard text, or phone text.

### Phase 3 — migrate the short tool loop to the Responses API

Files:

- `aipi5/llm/client.py`
- `aipi5/llm/conversation.py`
- `aipi5/llm/tools.py`
- `tests/test_client_negotiation.py` (replace endpoint-specific tests)
- new `tests/test_responses_client.py`

Preserve the public methods used elsewhere (`probe`, `respond`, `step`,
`describe_image`) during the migration so the Agent loop and voice loop do not
change together.

1. Implement a Responses API adapter behind `OpenAIClient`.
2. Send function tools with strict JSON schemas and `tool_choice: "auto"`.
3. Parse every output item; do not assume the first output is assistant text.
4. Return every function result with the matching call id.
5. Preserve a bounded tool loop and a final no-tools request that must produce
   prose.
6. Use `previous_response_id` or a Conversation object for the short assistant
   context, but keep AIPI5's idle expiration and maximum-turn behavior.
7. Continue keeping camera base64 out of future turns; store only the textual
   description in conversation history.
8. Keep SDK retries disabled and retain AIPI5's whole-attempt timeout/retry
   policy.
9. Add the built-in `web_search` tool only for current information that has no
   narrow local provider. Do not make weather/news depend on it.
10. Leave the chained AIA voice pipeline in place. Realtime speech is a later,
    optional phase.

Acceptance:

- ordinary conversation, a custom function call, multiple tool calls, a tool
  error, and a built-in web search are covered with fake Responses objects;
- startup probe exercises the same endpoint/tool shape production uses;
- no Chat Completions-specific negotiation remains after the migration;
- the existing Agent loop still completes and respects all ceilings.

### Phase 4 — add narrow everyday capability adapters

The model should see capabilities, not implementation objects. Extend
`aipi5/llm/tools.py` (or add small adapters imported by it) with the following
strict tools.

#### 4.1 Master volume

Tool: `set_master_volume(percent: integer 0..100)` and
`get_master_volume()`.

- Inject the existing `VolumeControl` into `ToolBox` from `main.py`.
- Call `VolumeControl.set()`; never shell out from the tool.
- Return the actual configured level from `describe()`.
- Add tests for 0, 100, clamping/refusal policy, persistence failure, and
  unavailable PipeWire.

#### 4.2 Birthdays/local calendar

Tools:

- `list_birthdays()`
- `save_birthday(name, calendar, month, day, year?, leap?, note?)`
- `delete_birthday(id)`

- Inject the existing `BirthdayStore`.
- Reuse `BirthdayStore.save()` validation and atomic write.
- The model must ask a follow-up if month/day, solar/lunar calendar, or the
  intended person is ambiguous.
- Do not claim this writes Google Calendar. It writes the project's local
  family calendar. Add Google Calendar later as a separately authenticated
  integration if that is the actual requirement.
- Deleting requires explicit identification and confirmation.

#### 4.3 Persistent reminders

Do not duplicate `Schedule` inside the voice service.

1. Add deterministic Agent socket messages, separate from `agent.ask`:
   - `assistant.reminder.create`;
   - `assistant.reminder.list`;
   - `assistant.reminder.cancel`.
2. Add matching `AgentProxy` methods.
3. In `AgentService`, validate and call the existing `Schedule` directly;
   bypass the maintenance model loop.
4. The everyday tool converts the model's absolute ISO local timestamp with
   existing `parse_when()` and forwards it.
5. Speak back the exact resolved date/time and delivery channel.
6. Preserve reboot survival, retry, and Web Push delivery in Housekeeping.

#### 4.4 Call my phone

Extract the body of `/api/call/out` into a reusable `CallController.call_out()`
method. Both the HTTP route and the new tool must call this method.

Tool: `call_phone(device?: enum of paired phones)`.

- Only offer it when calling is enabled and at least one push subscription is
  available.
- Never accept a phone number, URL, or push endpoint from model output.
- If more than one paired phone exists and no device is named, ask which one.
- Require spoken/on-screen confirmation before ringing.
- The result must distinguish: signalling started, push sent, phone answered,
  timed out, and notification failed.
- Keep the existing camera/audio ownership transition in `on_call_change()`.

#### 4.5 Take and save a picture

Add `aipi5/photos/capture.py` with a `PhotoCapture` adapter injected with
`Camera` and `FileStore`.

Tool: `take_photo(label?: string)`.

1. Call `Camera.capture_still()` once.
2. Save a persistent copy through `FileStore.save()` with a generated safe
   timestamp name; model/user text may influence only a sanitized label, never
   a path.
3. Return the stored filename, dimensions, and capture timestamp.
4. Publish a capture event so the picture appears in the Assistant transcript.
5. Refuse cleanly while games, calls, hand-browser control, or the screensaver
   camera handoff owns the camera.
6. Keep `describe_camera_image` separate: "take a picture" saves; "what do you
   see" captures temporarily and describes.

#### 4.6 Current Bitcoin price/current facts

Preferred order:

1. Add `get_asset_price(asset, currency)` backed by a small provider module
   with timeout, timestamp, currency validation, and a short cache.
2. If a reliable provider is not configured/reachable, use Responses API web
   search as fallback and say that the result is from web search.
3. Never use model knowledge for a price.
4. Return price, currency, provider/source, and `as_of` time.
5. Test stale cache, bad JSON, timeout, unsupported symbols, and provider
   failure without network.

#### 4.7 Browser/sites and Kodama

- Keep exact Open YouTube and Open Kodama fast-path commands.
- Add a typed `open_known_app(app)` tool with enum values produced from code,
  not editable YAML. The handler calls `BrowserLauncher`/`KodamaLauncher`.
- Only call it when the current user utterance explicitly asks to open/play;
  state this in the schema and enforce an `explicit_user_action` flag from the
  coordinator rather than trusting prompt compliance alone.
- Do not add arbitrary shell launch or an unconstrained URL tool to the
  everyday toolbox.
- The long Agent browser can still open an arbitrary public HTTP(S) URL under
  its existing helper URL policy and hand over sign-in/captcha/payment pages.

Acceptance for Phase 4: each example request works through model-selected typed
tools even when phrased differently from the AIA command list.

### Phase 5 — delegate long work without creating a second identity

Add one internal tool such as:

`delegate_agent_task(task, reason)`

This is offered only for device diagnosis, browser research/automation, or
maintenance that cannot finish inside the short tool loop.

1. The handler sends `agent.ask` through `AgentProxy` and returns the run id.
2. The coordinator emits `delegated` and continues streaming Agent mailbox
   events into the same transcript.
3. The voice reply is short: "I started checking that. Progress is on the
   screen." Do not read every tool event aloud.
4. Speak the final result only when it is short and the user is still in an
   active voice session; otherwise show it and optionally notify the paired
   phone.
5. Route cancel and approvals to the active delegated run.
6. Keep one active maintenance run at a time, matching the current service.

Acceptance: "why did the screen go blank last night?" delegates; "set volume
to 30" does not.

### Phase 6 — memory and conversation unification

The current voice conversation, 24-hour audible history, Agent mailbox, and
Agent notes serve different privacy/lifetime purposes. Do not replace them
with one unbounded transcript.

1. Keep the 24-hour audible room log local and role-filtered.
2. Keep short model context bounded by idle timeout and turn count.
3. Keep durable household preferences only through explicit `remember` and
   `forget` actions.
4. Add references between stores (event/run ids) rather than copying full logs.
5. Make the unified UI event stream a presentation index, not a new permanent
   surveillance log.

### Phase 7 — optional Realtime voice experiment

Do this only after Phases 1–6 are stable on the Pi.

- Keep the local wake detector. It is already measured and avoids sending
  continuous room audio to a cloud service.
- After wake, optionally open a short Realtime session for lower-latency,
  interruptible speech-to-speech and realtime tool use.
- Put it behind a feature flag and retain the AIA chained path as fallback.
- Measure first-audio latency, barge-in, Mandarin/English switching, music
  ducking, camera/call ownership, cost, and behavior during network loss.
- Do not run Realtime continuously while waiting for the wake word.

## 6. Prompt/tool instructions

Update the everyday system prompt so it says, concisely:

- use tools for device state and current facts;
- never claim an action succeeded until its tool says it did;
- ask one short clarification when required arguments are ambiguous;
- do not ask for confirmation in prose when a tool presents a confirmation;
- do not expose internal tool names or maintenance reasoning;
- use the language of the current user utterance;
- prefer narrow local tools over browser/web search;
- delegate multi-step diagnosis/automation rather than attempting it in the
  three-round voice loop.

Prompt rules improve behavior; handler validation and service boundaries are
the actual security controls.

## 7. End-to-end acceptance phrases

Test at least English and Mandarin variants. Record transcript, selected tool,
tool result, spoken response, and final side effect.

1. `小爱同学，打开 YouTube。`
2. `小爱同学，call my phone.`
3. `小爱同学，remind me tomorrow at 9 AM to call Mom.`
4. `小爱同学，把妈妈的生日加到日历，农历八月十五。`
5. `小爱同学，把音量调到百分之三十。`
6. `小爱同学，take a picture and save it.`
7. `小爱同学，what is the Bitcoin price in dollars right now?`
8. `小爱同学，what is the local news?`
9. `小爱同学，play some quiet music.`
10. `小爱同学，why did the screen restart last night?`
11. Ambiguous: `add Alex's birthday on the fifth.` The assistant must clarify.
12. Unsafe/unclear: `open that site and enter my password.` The assistant must
    hand over or refuse; it must not invent credentials.
13. Offline: repeat volume, calendar, camera, and known music commands with the
    OpenAI endpoint unreachable. Exact local commands must still work.
14. False wake: play ordinary speech containing a near-homophone and verify no
    destructive/persistent action occurs without confirmation.

## 8. Test plan

Add unit tests for every handler and policy edge. No unit test may require a
camera, microphone, accelerator, network, API key, real browser, real Kodama,
or real systemd.

Required groups:

- schema strictness and invented-tool refusal;
- tool availability filtering;
- explicit-action enforcement for launches/calls;
- confirmation token binding, expiry, denial, and double tap;
- reminder timestamp and timezone resolution;
- birthday solar/lunar/leap validation and ambiguity;
- camera ownership conflicts and persistent save;
- phone selection and notification failure;
- current-price cache/failure/as-of timestamp;
- Responses tool-call pairing and bounded final response;
- UI XSS, duplicate events, cursor resume, page enter/leave cleanup;
- microphone single-owner invariant;
- Agent/helper privilege boundary;
- full suite cannot start Kodama or Chromium.

On-device verification after every deploy:

1. `systemctl --user status aipi5 aipi5-ui aipi5-agent`
2. Confirm only one microphone reader.
3. Say the exact fast-path commands before testing model-selected variations.
4. Test camera → call → camera, camera → game → camera, and browser hand
   control → close → camera ownership cycles.
5. Verify music ducks for speech and resumes unless the command intentionally
   stopped it.
6. Reboot and verify reminders, birthdays, volume, Agent socket activation,
   and the unified page.
7. Review the journal for secret/model output leakage and false success claims.

## 9. Rollout order

Enable tools one at a time behind code/config feature switches:

1. unified page only;
2. volume and birthday tools;
3. reminder RPC;
4. saved camera photo;
5. current asset price/web search;
6. voice-triggered outgoing call;
7. known-app model tool;
8. long-task delegation;
9. optional Realtime experiment.

For each tool, deploy, test on the actual Pi, then enable the next. Do not ship
all persistent/external side effects in one release.

## 10. Definition of done

The change is complete when:

- the home screen has one Assistant destination and no separate Agent page;
- wake voice, typed text, dictation, and phone text share one coordinator and
  policy;
- users can phrase requests naturally without reading a command/config list;
- exact local commands still work when OpenAI is unavailable;
- all requested examples either work or ask the minimum necessary
  clarification/confirmation;
- long maintenance work still runs in the sandboxed Agent service;
- no model output reaches shell, arbitrary local paths, root operations,
  unpaired phones, or private URLs without a validated adapter;
- current facts carry sources/timestamps and are never guessed from model
  memory;
- the full off-device suite passes and the hardware ownership matrix passes on
  the Pi;
- README, REPORT, screenshots, and deployment instructions describe the new
  single-assistant experience accurately.
