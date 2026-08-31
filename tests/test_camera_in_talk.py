"""The camera answers in the conversation, and brings the picture with it.

Three things had to hold at once for that to be true, and each of them is one
class here: the state has to carry the photograph alongside the sentence, the
server has to be able to serve that one file and only that one, and the page
has to have stopped offering a Camera destination it no longer has.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from aipi5.ui.server import ASSET_ROOT, _Handler
from aipi5.ui.state import UiState
from aipi5.vision.camera import Capture

PAGE = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")


class TestTheDescriptionCarriesItsPicture(unittest.TestCase):
    """The sentence and the still are one event, so they move together."""

    def test_the_picture_arrives_with_the_description(self):
        ui = UiState()
        ui.describe_camera("A desk with two monitors.", 1712345678.5)
        snapshot = ui.snapshot()
        self.assertEqual(snapshot["camera_description_id"], 1)
        self.assertEqual(snapshot["camera_image"], 1712345678.5)

    def test_an_answer_with_no_capture_carries_no_picture(self):
        # The button's fallback: the model answered without calling the vision
        # tool. There is a sentence and there is no photograph of the moment
        # it was said, and the page must not draw the previous one under it.
        ui = UiState()
        ui.describe_camera("A desk.", 1712345678.5)
        ui.describe_camera("I did not look just now.")
        self.assertIsNone(ui.snapshot()["camera_image"])
        self.assertEqual(ui.snapshot()["camera_description_id"], 2)

    def test_clearing_drops_the_picture_too(self):
        ui = UiState()
        ui.describe_camera("A desk.", 1712345678.5)
        ui.describe_camera(None, 1712345678.5)
        self.assertIsNone(ui.snapshot()["camera_description"])
        self.assertIsNone(ui.snapshot()["camera_image"])


class _Camera:
    """Only the part of `Camera` the route reads."""

    def __init__(self, last_capture=None):
        self.last_capture = last_capture


class _Served:
    """What the handler sent, without a socket under it."""

    def __init__(self):
        self.body = None
        self.kind = ""
        self.code = 0
        self.json = None


def _serve(camera) -> _Served:
    """Run `/api/camera/capture` against a handler with nothing behind it."""
    handler = _Handler.__new__(_Handler)
    handler.ui = type("Ui", (), {"camera": camera})()
    served = _Served()

    def send(code, body, content_type):
        served.code, served.body, served.kind = code, body, content_type

    def as_json(payload, code=200):
        served.code, served.json = code, payload

    handler._send = send
    handler._json = as_json
    handler._camera_capture()
    return served


class TestServingTheCapture(unittest.TestCase):

    def test_the_last_capture_is_served_as_a_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "20260826-101500.jpg"
            path.write_bytes(b"\xff\xd8\xff not really a jpeg")
            served = _serve(_Camera(Capture(path=path, taken_at=time.time(),
                                            width=1280, height=720)))
        self.assertEqual(served.code, 200)
        self.assertEqual(served.kind, "image/jpeg")
        self.assertEqual(served.body, b"\xff\xd8\xff not really a jpeg")

    def test_nothing_captured_yet_is_a_404_and_not_a_crash(self):
        served = _serve(_Camera(None))
        self.assertEqual(served.code, 404)

    def test_a_disabled_camera_is_a_404_and_not_an_attribute_error(self):
        served = _serve(None)
        self.assertEqual(served.code, 404)

    def test_a_pruned_capture_is_a_404_rather_than_a_500(self):
        # `Camera._prune` keeps ten stills and /dev/shm is cleared on reboot,
        # so the file behind a description outlives it only for a while. The
        # page drops the picture on this and keeps the sentence.
        with tempfile.TemporaryDirectory() as folder:
            gone = Path(folder) / "deleted.jpg"
            served = _serve(_Camera(Capture(path=gone, taken_at=time.time(),
                                            width=1280, height=720)))
        self.assertEqual(served.code, 404)

    def test_the_route_takes_no_name_from_the_browser(self):
        """The reason this route is safe: there is nothing to traverse with.

        A URL that named a file under /dev/shm would be the shape of every
        directory traversal ever written. The camera decides which file this
        is; the query string is only a cache-buster.
        """
        source = Path(sys.modules[_Handler.__module__].__file__)
        body = source.read_text(encoding="utf-8").split(
            "def _camera_capture")[1].split("\n    def ")[0]
        self.assertNotIn("self.path", body)
        self.assertNotIn("params", body)


class TestTheAnswerReachesTheConversation(unittest.TestCase):
    """The button's reply is filtered into the Talk feed, not out of it.

    Read out of the source rather than run, because `aipi5/main.py` imports the
    whole assistant — AIA's wake word, sounddevice, OpenCV — and no test in
    this suite can import it on a machine that is not the Pi. What is being
    checked is one line of policy with nowhere else to live: `/api/feed` keeps
    `user` and `aia` and drops every `aia:page` role, so an answer recorded
    under a role of its own is an answer the conversation never shows.
    """

    MAIN = (ASSET_ROOT.parents[3] / "aipi5" / "main.py").read_text(encoding="utf-8")

    #: What ends one branch of `handle_button`'s chain: a dedent back to the
    #: next `elif`. Split on it so a role assigned three branches later cannot
    #: be read as this one's.
    NEXT = chr(10) + "    elif "

    def _branch(self, action: str) -> str:
        # `action == "..."`, not `elif action == "..."`: the first arm of the
        # chain is an `if` and reading it has to work the same way.
        return self.MAIN.split(f'action == "{action}":')[1].split(self.NEXT)[0]

    def test_the_camera_answers_as_the_assistant(self):
        branch = self._branch("camera")
        self.assertIn('role = "aia"' + chr(10), branch)
        self.assertNotIn('role = "aia:camera"', branch)

    def test_the_pages_that_speak_about_themselves_still_do(self):
        # The guard on the change above: `aia:page` roles are not a mistake
        # being cleaned up, they are how a page summary stays out of the
        # conversation while remaining in the 24-hour record.
        self.assertIn('role = "aia:weather"', self._branch("weather"))
        self.assertIn('role = "aia:music"', self._branch("kodama"))


class TestTheCameraLivesOnTheTalkPage(unittest.TestCase):
    """No destination of its own on the home screen, and an answer in the feed."""

    def test_the_home_screen_no_longer_offers_a_camera_button(self):
        home = PAGE.split('<div id="page-main"')[1].split('<div id="page-talk"')[0]
        self.assertNotIn('data-action="camera"', home)
        # The rest of the home screen is untouched, which is what makes the
        # absence above a removal rather than a broken split.
        for kept in ("talk", "call", "weather", "kodama"):
            self.assertIn(f'data-action="{kept}"', home)

    def test_the_talk_page_asks_the_camera_instead(self):
        talk = PAGE.split('<div id="page-talk"')[1].split('<div id="page-call"')[0]
        self.assertIn('data-action="camera"', talk)
        self.assertIn('id="talk-listen"', talk)

    def test_pressing_the_camera_opens_the_conversation(self):
        # The one line that decides where the answer is looked for.
        self.assertIn('camera: "talk"', PAGE)
        self.assertNotIn('camera: "camera"', PAGE)

    def test_the_picture_is_drawn_into_both_feeds(self):
        # The same rule `addMessage` follows: one line, two feed elements, so
        # navigating between the main screen and Talk never loses it.
        body = PAGE.split("function addCapture")[1].split("\nfunction ")[0]
        self.assertIn('el("main-feed")', body)
        self.assertIn('el("talk-feed")', body)
        self.assertIn("/api/camera/capture?t=", body)
        # A pruned still must remove its bubble rather than leave a broken
        # image in the middle of the conversation.
        self.assertIn("image.onerror", body)

    def test_the_transcript_is_drained_before_the_picture_is_drawn(self):
        """The picture follows the sentence, every time and not most times.

        The description and the reply are published a millisecond apart and
        reach the page down two polls running at 500 and 1000 ms, so without
        this the order was decided by whichever timer fired first — observed
        both ways on the device. `addCapture` waits for the transcript, and
        `drainFeed` coalesces so that waiting for it cannot append a row twice.
        """
        body = PAGE.split("function addCapture")[1].split("\nfunction ")[0]
        self.assertIn("await drainFeed()", body)
        drain = PAGE.split("function drainFeed")[1].split("\nasync function ")[0]
        self.assertIn("feedInFlight", drain)


if __name__ == "__main__":
    unittest.main()
