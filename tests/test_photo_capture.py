"""Taking a picture and keeping it, which is not what the camera already did.

Two tools that look alike and are not:

    describe_camera_image   capture, describe, let tmpfs reclaim it
    take_photo              capture, copy into the Files folder, keep it

The first is the input to a vision request; the second's whole content is that
the picture should still be there tomorrow. Confusing them is the failure this
file is mostly about — that, and the two things that can genuinely go wrong on
the device: another process holding the camera, and a name built out of
something somebody said.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aipi5.llm.tools import ToolBox
from aipi5.photos.capture import PhotoCapture, PhotoError, safe_label

# A one-pixel JPEG. Enough to be read, copied and counted.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    + "08" * 64
    + "ffc9000b080001000101011100ffcc000600101005ffda0008010100003f00d2cf20"
    + "ffd9")


class FakeCamera:
    """`Camera`'s four members that matter here, and its two failure modes."""

    def __init__(self, tmp: Path, lent="", started=True, writes=True):
        self.tmp = tmp
        self._lent = lent
        self._started = started
        self.writes = writes
        self.captures = 0
        self.taken_at = 1_756_800_000.0

    @property
    def lent(self):
        return bool(self._lent)

    def describe(self):
        return {"lent_to": self._lent or None, "running": self._started}

    def available(self):
        return self._started

    def capture_still(self):
        self.captures += 1
        if not self.writes:
            return None
        self.taken_at += 1.0
        path = self.tmp / f"{int(self.taken_at)}.jpg"
        path.write_bytes(JPEG)
        return SimpleNamespace(path=path, taken_at=self.taken_at,
                               width=1280, height=720)


class Stored:
    def __init__(self, name):
        self.name = name


class FakeFiles:
    """`FileStore`'s `save` and `ready`, over a directory."""

    def __init__(self, root: Path, ready=True, error=None):
        self.root = root
        self.ready = ready
        self.error = error
        self.saved: list[str] = []

    def save(self, name, chunks, expected=0):
        if self.error is not None:
            raise self.error
        # Never overwrites, the way the real one does not.
        final, n = name, 1
        while (self.root / final).exists():
            stem, _, ext = name.rpartition(".")
            final = f"{stem} ({n}).{ext}"
            n += 1
        (self.root / final).write_bytes(b"".join(chunks))
        self.saved.append(final)
        return Stored(final)


class CaptureCase(unittest.TestCase):

    def setUp(self):
        self.shm = tempfile.TemporaryDirectory()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.shm.cleanup)
        self.addCleanup(self.folder.cleanup)
        self.camera = FakeCamera(Path(self.shm.name))
        self.files = FakeFiles(Path(self.folder.name))
        self.photos = PhotoCapture(camera=self.camera, files=self.files)


class TestTheName(unittest.TestCase):
    """The filename is built from the clock. A label is a bounded suffix.

    Text that reached the model reaching a path is the thing this has to make
    impossible rather than unlikely, and the way to do that is for the path to
    be a timestamp with a suffix rather than something validated after the
    fact.
    """

    def test_a_plain_label_survives(self):
        self.assertEqual("kitchen", safe_label("kitchen"))
        self.assertEqual("the cat", safe_label("  the   cat  "))

    def test_mandarin_survives(self):
        """This house speaks it, and 厨房 is a perfectly good label."""
        self.assertEqual("厨房", safe_label("厨房"))

    def test_nothing_that_means_something_to_a_path_survives(self):
        for nasty in ("../../etc/passwd", "/etc/shadow", "a/b", "..",
                      ".hidden", "a\\b", "a\x00b", "a\nb"):
            with self.subTest(label=nasty):
                cleaned = safe_label(nasty)
                for forbidden in ("/", "\\", "..", "\x00", "\n"):
                    self.assertNotIn(forbidden, cleaned)
                self.assertFalse(cleaned.startswith("."))

    def test_nothing_that_means_something_to_a_shell_survives(self):
        for nasty in ("$(reboot)", "`id`", "a; rm -rf ~", "a && b", "a | b",
                      "--version"):
            with self.subTest(label=nasty):
                cleaned = safe_label(nasty)
                for forbidden in ("$", "`", ";", "|", "&", "("):
                    self.assertNotIn(forbidden, cleaned)
                self.assertFalse(cleaned.startswith("-"))

    def test_a_label_that_is_all_punctuation_simply_is_not_there(self):
        self.assertEqual("", safe_label("!!!///"))

    def test_it_is_bounded(self):
        self.assertLessEqual(len(safe_label("x" * 500)), 40)


class TestTakingOne(CaptureCase):

    def test_a_picture_is_kept_and_the_name_comes_from_the_clock(self):
        saved = self.photos.take()
        self.assertTrue(saved["filename"].startswith("photo-"))
        self.assertTrue(saved["filename"].endswith(".jpg"))
        self.assertEqual([saved["filename"]], self.files.saved)
        self.assertTrue((Path(self.folder.name) / saved["filename"]).exists())

    def test_a_label_lands_in_the_filename(self):
        self.assertIn("-kitchen.", self.photos.take("kitchen")["filename"])

    def test_a_label_that_sanitises_to_nothing_leaves_a_clean_name(self):
        saved = self.photos.take("../../etc/passwd")
        self.assertNotIn("..", saved["filename"])
        self.assertNotIn("/", saved["filename"])

    def test_the_dimensions_and_the_time_come_back(self):
        saved = self.photos.take()
        self.assertEqual((1280, 720), (saved["width"], saved["height"]))
        self.assertGreater(saved["bytes"], 0)
        self.assertIn("-", saved["taken"])

    def test_the_stored_name_is_reported_and_not_the_requested_one(self):
        """`FileStore.save` never overwrites, so two photographs in the same
        second get different names. Echoing the requested one would tell
        somebody a file exists under a name it does not have."""
        self.camera.taken_at = 1_756_800_000.0
        first = self.photos.take("cat")
        self.camera.taken_at -= 1.0            # same second again
        second = self.photos.take("cat")
        self.assertNotEqual(first["filename"], second["filename"])
        self.assertIn("(1)", second["filename"])

    def test_a_token_comes_back_and_it_is_not_a_path(self):
        """`/api/camera/capture?t=` takes a token. A path in this field would
        be a filesystem path published to every reader on loopback."""
        saved = self.photos.take()
        self.assertNotIn("/", saved["token"])
        self.assertNotIn("\\", saved["token"])


class TestWhenItCannot(CaptureCase):

    def test_a_borrowed_camera_says_who_has_it(self):
        """"The camera is being used by a video call" is something a person can
        act on. "The camera did not take a picture" is not."""
        self.camera._lent = "a video call"
        with self.assertRaises(PhotoError) as raised:
            self.photos.take()
        self.assertIn("a video call", str(raised.exception))
        self.assertEqual(0, self.camera.captures)

    def test_the_owner_is_asked_before_the_capture_and_not_after(self):
        """Reopening a device another process is reading does not fail cleanly
        on this hardware — it produces a camera that stops working until
        something is restarted."""
        self.camera._lent = "a game"
        with self.assertRaises(PhotoError):
            self.photos.take()
        self.assertEqual(0, self.camera.captures)

    def test_a_camera_that_is_not_running_says_so(self):
        self.camera._started = False
        with self.assertRaises(PhotoError) as raised:
            self.photos.take()
        self.assertIn("not available", str(raised.exception))

    def test_a_capture_that_produced_nothing_says_so(self):
        self.camera.writes = False
        with self.assertRaises(PhotoError) as raised:
            self.photos.take()
        self.assertIn("did not take a picture", str(raised.exception))

    def test_nowhere_to_put_it_is_refused_before_the_camera_is_touched(self):
        self.files.ready = False
        with self.assertRaises(PhotoError):
            self.photos.take()
        self.assertEqual(0, self.camera.captures)

    def test_a_save_that_failed_is_not_reported_as_a_photograph(self):
        self.files.error = OSError("the disk filled up")
        with self.assertRaises(PhotoError) as raised:
            self.photos.take()
        self.assertIn("could not be saved", str(raised.exception))

    def test_with_no_camera_at_all_it_is_simply_unavailable(self):
        self.assertFalse(PhotoCapture(files=self.files).available())
        self.assertFalse(PhotoCapture(camera=self.camera).available())


class TestTheTool(CaptureCase):

    def setUp(self):
        super().setUp()
        self.published: list[dict] = []
        self.box = ToolBox(photos=self.photos,
                           on_capture=self.published.append)

    def call(self, **args):
        return json.loads(self.box.call("take_photo", json.dumps(args)))

    def test_it_answers_with_the_filename(self):
        answer = self.call()
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["filename"].startswith("photo-"))

    def test_the_photograph_reaches_the_transcript(self):
        self.call(label="kitchen")
        self.assertEqual(1, len(self.published))
        self.assertIn("filename", self.published[0])

    def test_it_is_published_after_the_save_and_not_before(self):
        """What appears on the screen has to be a picture that is on disk."""
        self.files.error = OSError("the disk filled up")
        self.assertFalse(self.call()["ok"])
        self.assertEqual([], self.published)

    def test_a_screen_that_throws_does_not_lose_the_photograph(self):
        box = ToolBox(photos=self.photos,
                      on_capture=lambda saved: 1 / 0)
        answer = json.loads(box.call("take_photo", "{}"))
        self.assertTrue(answer["ok"])
        self.assertEqual(1, len(self.files.saved))

    def test_a_null_label_is_not_the_word_none(self):
        """Strict mode sends an explicit null. `str(None)` is "None", which
        would be in the filename of every photograph nobody labelled."""
        self.assertNotIn("None", self.call(label=None)["filename"])

    def test_a_busy_camera_is_a_sentence_and_not_an_exception(self):
        self.camera._lent = "a video call"
        answer = self.call()
        self.assertFalse(answer["ok"])
        self.assertIn("a video call", answer["error"])

    def test_it_is_not_offered_without_a_camera_and_somewhere_to_put_it(self):
        self.assertEqual([], [t for t in ToolBox().schemas()
                              if t.get("name") == "take_photo"])
        self.assertEqual(
            [], [t for t in ToolBox(photos=PhotoCapture()).schemas()
                 if t.get("name") == "take_photo"])

    def test_the_two_camera_tools_are_told_apart_in_the_schema(self):
        """A model given both will use whichever it read last unless the
        difference is stated where it can see it."""
        schema = next(t for t in self.box.schemas() if t["name"] == "take_photo")
        self.assertIn("keep", schema["description"])
        self.assertIn("describe_camera_image", schema["description"])
        self.assertIn("does not keep it", schema["description"])

    def test_they_are_separate_handlers(self):
        box = ToolBox()
        self.assertIn("take_photo", box._handlers)
        self.assertIn("describe_camera_image", box._handlers)
        self.assertIsNot(box._handlers["take_photo"],
                         box._handlers["describe_camera_image"])


if __name__ == "__main__":
    unittest.main()
