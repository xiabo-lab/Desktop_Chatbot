"""Turning one captured frame into a sentence somebody wants to hear.

Thin on purpose. The camera took the picture, the client sends the picture, and
what is left is the part that belongs to neither: which instruction goes with
it, how long a description is worth waiting for, and what to say when it does
not arrive.

The one piece of policy here is that **the picture is always fresh and always
one**. `Camera.capture_still` takes a new frame every time it is called and
this never caches a description across captures, because "what do you see"
asked twice in a room where something has changed has two different right
answers — section 19 says so and it is the whole reason a cached image would be
worse than no image.
"""

from __future__ import annotations

import logging

from aipi5.llm import prompts

log = logging.getLogger(__name__)


class VisionDescriber:
    """Sends a capture to the vision model and hands back what it said."""

    def __init__(self, client):
        self.client = client
        self._last: str | None = None

    @property
    def available(self) -> bool:
        return self.client is not None and self.client.available

    @property
    def last_description(self) -> str | None:
        """What was said about the most recent picture, for the screen.

        Section 19 asks for the description to appear on the display where
        appropriate. Held here rather than pushed, so the UI can show it
        without this module knowing a display exists.
        """
        return self._last

    def describe(self, capture, question: str = "") -> str | None:
        """Describe one capture. None on any failure, with the reason logged.

        Returns None rather than an apology string so the caller decides what
        to say — the tool path wraps it in a JSON error the model turns into a
        sentence, while a direct spoken command says its own.
        """
        if not self.available:
            log.info("no vision model available")
            return None

        data_url = capture.as_data_url()
        if data_url is None:
            return None

        # Roughly, and only for the log: base64 is four characters per three
        # bytes, so this is the JPEG size and not the payload size. Worth
        # having because a camera that quietly starts producing 4 MB frames
        # shows up here as a slow turn long before anybody thinks to look at
        # the camera.
        log.info("describing a %.0f KB capture", len(data_url) * 3 / 4 / 1024)

        reply = self.client.describe_image(
            data_url, prompts.VISION_MODE, question)
        if not reply:
            log.warning("vision request produced nothing: %s", reply.error)
            return None

        log.info("vision answered in %.0f ms: %r", reply.ms, reply.text[:80])
        self._last = reply.text
        return reply.text
