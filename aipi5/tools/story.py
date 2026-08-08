"""Bedtime stories: how long, about what, and what they may not contain.

This is the one thing the assistant does that is purely generated — there is no
feed to read and no sensor to sample, so what would otherwise be a tool
implementation is instead a set of constraints on a request. Keeping them here
rather than inline in the prompt file means the length arithmetic and the
subject parsing can be tested, and that the child-safety rules are in one
readable list rather than a paragraph of prose somebody has to re-read to
audit.

**Length is derived from speech, not from tokens.** A story is measured in how
long it takes to hear, because that is what "a short story" means at bedtime.
Piper reads English at roughly 150 words a minute and Mandarin at roughly 240
characters a minute, both measured on this device's voices; those two rates are
what turn "four minutes" into a word budget the model can aim at.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Measured against this project's Piper voices at their default speed. Rough on
# purpose — the model treats a word count as a target, not a contract, and
# precision here would be false.
WORDS_PER_MINUTE_EN = 150
CHARS_PER_MINUTE_ZH = 240

# The rules the story is generated under. A list rather than a paragraph so
# that adding or removing one is a visible, reviewable change — this is the
# part of the assistant a parent would want to read.
SAFETY_RULES = (
    "The listener is a young child at bedtime.",
    "Nothing frightening: no violence, no injury, no death, no peril the child "
    "should worry about, and no villain who is genuinely menacing.",
    "Nothing sad at the end. Every story finishes calm, warm and resolved.",
    "No romance, no crude humour, no commercial brands, no real people.",
    "Gentle pacing and simple sentences. It is going to be read aloud by a "
    "synthesised voice, so avoid unusual spellings, sound effects, asterisks, "
    "emoji, headings and anything else that is punctuation rather than words.",
    "Keep it in the language the request was made in.",
)

# How a length is asked for out loud. Ordered longest-phrase-first so that
# "a very short story" does not match on "short" and lose the "very".
_LENGTHS = (
    ("really long", 3.0), ("very long", 3.0), ("nice long", 2.0),
    ("really short", 0.5), ("very short", 0.5), ("quick", 0.5),
    ("short", 0.6), ("long", 2.0), ("tiny", 0.4),
    ("很长", 3.0), ("长一点", 2.0), ("长", 2.0),
    ("很短", 0.5), ("短一点", 0.6), ("短", 0.6),
)

# Words that introduce a subject rather than being one. Stripped so that
# "tell me a story about a dragon who bakes" yields "a dragon who bakes" and
# not the whole sentence, which the model would then dutifully repeat back.
#
# The length adjective is part of the lead-in and not part of the subject, so
# it is matched *inside* this pattern rather than trimmed off afterwards. That
# was the first attempt and it did not work: "tell me a short story about a
# dragon" has the adjective in the middle, where a pattern that expects
# "a story" finds nothing to strip and hands the model the entire sentence
# back as the subject.
_LENGTH_WORDS = (r"(?:really\s+|very\s+|nice\s+)?"
                 r"(?:short|long|quick|tiny|little)\s+")
_LEAD_IN = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+|could you\s+|will you\s+)?"
    r"(?:tell|read|say)?\s*(?:me\s+)?(?:a|an|another|one more)?\s*"
    # The length group is wrapped before `?` is applied to it. Interpolating
    # it bare and appending `?` attaches the quantifier to the trailing `\s+`
    # instead — making it lazy rather than making the group optional — so the
    # adjective becomes mandatory and a plain "tell me a story" stops matching
    # at all.
    rf"(?:\d+\s*(?:minute|minutes|min)\s+)?(?:{_LENGTH_WORDS})?"
    r"(?:bed\s?time\s+)?(?:story|tale)\s*(?:about|of|on|with)?\s*",
    re.IGNORECASE,
)

# Mandarin puts the topic *before* the noun — 讲一个关于小兔子的睡前故事 — so
# there is no "story" token to anchor the end of a lead-in on, and a pattern
# that insists on one matches nothing at all. Only the opening verb phrase is
# stripped, and what is left ("关于小兔子的睡前故事") is the subject as spoken.
_LEAD_IN_ZH = re.compile(
    r"^\s*(?:请)?(?:你)?(?:给我|帮我)?(?:讲|说)(?:一个|一則|一则|个|一)?\s*")

# ...which leaves the bare request — 讲个故事 — with "故事" as its subject. It
# is not one: it is the noun with nothing in front of it, and passing it on
# would ask the model for a story about the concept of stories.
_BARE_ZH = ("故事", "童话", "睡前故事", "一个故事", "睡前的故事")


@dataclass(frozen=True)
class StoryRequest:
    """A story, as the model will be asked for it."""

    subject: str
    minutes: float
    language: str

    @property
    def budget(self) -> str:
        """The length, phrased as the model should aim at it."""
        if self.language == "zh":
            return f"about {int(self.minutes * CHARS_PER_MINUTE_ZH)} Chinese characters"
        return f"about {int(self.minutes * WORDS_PER_MINUTE_EN)} words"

    @property
    def about(self) -> str:
        return self.subject or ("anything gentle and suitable for bedtime")


def parse(text: str, language: str = "en", default_minutes: float = 4.0,
          max_minutes: float = 10.0) -> StoryRequest:
    """Read a spoken story request into a subject and a length.

    Tolerant by design. This is fed a speech transcript, so it must never fail
    on an unexpected shape — a request it cannot parse becomes a story of the
    default length about nothing in particular, which is a perfectly good
    bedtime story and much better than an error.
    """
    said = (text or "").strip()

    minutes = default_minutes
    lowered = said.lower()
    for phrase, factor in _LENGTHS:
        if phrase in lowered:
            minutes = default_minutes * factor
            break

    # An explicit number of minutes beats any adjective: somebody who says
    # "a ten minute story" has been more specific than "a long story".
    explicit = re.search(r"(\d+)\s*(?:minute|minutes|min|分钟)", lowered)
    if explicit:
        minutes = float(explicit.group(1))

    minutes = max(0.5, min(minutes, max_minutes))

    if language == "zh":
        subject = _LEAD_IN_ZH.sub("", said).strip(" ,.。，、!?！？")
        if subject in _BARE_ZH:
            subject = ""
    else:
        subject = _LEAD_IN.sub("", said).strip(" ,.。，、!?！？")

    return StoryRequest(subject=subject, minutes=minutes, language=language)


def instructions(request: StoryRequest) -> str:
    """The whole of what the model is told, for one story.

    Returned as text rather than sent, so the caller decides how to use it and
    so a test can read it. Its content is the safety rules verbatim plus the
    length and subject — no cleverness, because this is the prompt a parent
    would want to be able to read in full.
    """
    rules = "\n".join(f"- {rule}" for rule in SAFETY_RULES)
    return (
        f"Tell a bedtime story about: {request.about}.\n"
        f"Length: {request.budget}.\n"
        f"Language: {'Mandarin Chinese' if request.language == 'zh' else 'English'}.\n"
        f"\nRules:\n{rules}\n"
        f"\nReply with the story itself and nothing else — no title, no "
        f"preamble, no 'here is your story'."
    )
