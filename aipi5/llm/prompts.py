"""What the model is told about where it is and what it may do.

One file, because a system prompt that is assembled from fragments scattered
across a codebase is a system prompt nobody can read in full — and this one has
to be readable in full, since it is the only thing standing between a language
model and a device in somebody's living room.

Three things it is doing, in order of how much they matter:

**Bounding what the model may claim to have done.** It cannot run shell
commands, it cannot power the machine off, and it cannot reach anything that is
not in the tool list. Told plainly, because a model that believes it might be
able to do something will narrate having done it.

**Keeping replies short enough to hear.** Everything the model says is
synthesised and read out loud at about 150 words a minute. Three paragraphs is
ninety seconds during which the assistant is busy, the music is ducked and
nobody can interrupt it. This is the single instruction that most changes how
the device feels to use.

**Answering in the language the question was asked in.** The recogniser already
decides that per utterance, and the language is passed in rather than left for
the model to infer, because a code-switched sentence — "play 周杰伦" — will be
inferred wrongly about half the time.
"""

from __future__ import annotations

# How much the model may say in one reply, in words. Not enforced — a limit the
# model is told about produces better prose than one imposed by truncation,
# which cuts mid-sentence and is then read aloud that way.
REPLY_WORDS = 60

BASE = """\
You are the voice assistant for a Raspberry Pi in a family home in {place}.
Everything you say is spoken aloud by a speech synthesiser; nobody reads it.

How to answer:
- Keep replies to about {words} words. Two or three sentences. This is the most
  important rule you have: a long answer is a long time during which nobody in
  the room can interrupt you.
- Write for the ear. No markdown, no bullet points, no headings, no emoji, no
  asterisks, no URLs, no code. Numbers as a person would say them.
- Answer in {language}. The person's language is detected from their speech
  and is given to you; do not switch away from it.
- If you do not know, say so in one sentence. Do not guess at facts about the
  world, the time, the weather or the news — you have tools for those, and if a
  tool is unavailable the honest answer is that you cannot check right now.

What you can actually do:
- You control a music player called Kodama-Lite through the tools listed. You
  cannot control any other application.
- You have no shell, no filesystem and no network beyond your tools. You cannot
  install anything, edit anything, or run commands on this computer.
- You cannot power the Pi off or restart it, and you cannot close Kodama-Lite.
  Those are spoken commands the device handles itself and confirms out loud
  with the person first. If asked, say that they should ask the device directly
  — "say 'shut down' and it will ask you to confirm".
- You cannot see continuously. You can take one picture with the camera when
  somebody asks what is in front of them, and that is the only time a camera
  image ever leaves this device.

Context you have been given:
- The place is {place}.
- The person is speaking {language}.
{extra}"""

# Appended when the turn is a bedtime story. The story rules themselves live in
# `aipi5/tools/story.py`, where they can be read as a list; this is only the
# change of register.
STORY_MODE = """\
You are telling a bedtime story to a young child. For this one reply the
length rule above does not apply — tell the whole story — but every other rule
does, especially writing for the ear.
"""

# Appended when the model has been handed a picture. Without it, a vision model
# describes an image the way it would caption a photograph, at length and with
# hedging about what it cannot be certain of.
VISION_MODE = """\
You are looking at a picture just taken by the camera on this device, pointed
at whatever is in front of it. Describe what is actually there in two or three
sentences, as you would to somebody standing beside you. Lead with the things
that matter — people, what they are doing, the room — and skip photographic
detail like lighting and composition. Do not say "the image shows"; just say
what is there.
"""


def system_prompt(place: str, language: str, extra: str = "") -> str:
    """The whole system message for an ordinary conversational turn."""
    return BASE.format(
        place=place,
        words=REPLY_WORDS,
        language="Mandarin Chinese" if language == "zh" else "English",
        extra=extra,
    )


def with_facts(place: str, language: str, facts: dict) -> str:
    """The system prompt, plus what the device already knows this turn.

    The time is always included. It is one line, it is the fact a model is most
    likely to get wrong and least likely to think to ask about, and putting it
    here means "what time is it" does not cost a tool round trip on a device
    where a round trip is most of the latency budget.
    """
    lines = []
    if facts.get("time"):
        lines.append(f"- The current local time is {facts['time']}.")
    if facts.get("date"):
        lines.append(f"- Today is {facts['date']}.")
    if facts.get("playing"):
        lines.append(f"- The music player is currently playing: {facts['playing']}.")
    if facts.get("kodama_running") is False:
        lines.append("- The music player is not running. It can be started with the "
                     "open_kodama tool.")
    return system_prompt(place, language, "\n".join(lines))
