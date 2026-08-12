"""Which video node the camera picks, decided without a camera.

The Brio 101 replaced the Camera Module 3 and brought the one problem CSI never
had: `/dev/video0` is not an identity. A USB camera claims more than one node,
one of them metadata that opens cleanly and never yields an image, and the
order they enumerate in depends on what else was plugged in at boot. Getting
that ranking wrong is not a crash — it is an assistant that reports a working
camera and describes nothing, which is exactly the failure that is worth a test
rather than an afternoon of unplugging things.

Everything here works against a fake `/dev` and a fake `/sys`, so it runs on
the development machine with no camera, no OpenCV and no Linux.
"""

from __future__ import annotations

import base64
import time
import unittest
from pathlib import Path
from unittest import mock

from aipi5.core.config import CameraConfig
from aipi5.vision import camera as camera_mod


class TestDeviceSelection(unittest.TestCase):

    def nodes(self, layout: dict[str, tuple[str, str, int]], tmp: Path):
        """Build a fake /dev and /sys from {node: (name, driver, index)}.

        `driver` is a real symlink rather than a file, because that is what the
        kernel puts there and `_driver` resolves it.
        """
        dev, sysfs = tmp / "dev", tmp / "sys"
        dev.mkdir(parents=True, exist_ok=True)
        for node, (product, driver, index) in layout.items():
            (dev / node).write_bytes(b"")
            entry = sysfs / node
            (entry / "device").mkdir(parents=True, exist_ok=True)
            (entry / "name").write_text(product, encoding="utf-8")
            (entry / "index").write_text(str(index), encoding="utf-8")
            target = tmp / "drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            link = entry / "device" / "driver"
            if not link.exists():
                try:
                    link.symlink_to(target, target_is_directory=True)
                except OSError:  # pragma: no cover — Windows without symlinks
                    self.skipTest("this user cannot create symlinks")
        return mock.patch.multiple(camera_mod, DEV=dev, SYSFS=sysfs)

    def candidates(self, layout, **cfg) -> list[str]:
        with self.nodes(layout, self.tmp):
            return [Path(c).name for c in
                    camera_mod._candidates(CameraConfig(**cfg))]

    def setUp(self):
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    # What `ls /sys/class/video4linux` actually shows on this Pi, shortened:
    # the webcam's two nodes and a sample of the eighteen the ISP and the HEVC
    # decoder always occupy. The camera is not video0 and its nodes are not
    # adjacent to the ones anybody would guess.
    LAYOUT = {
        "video0": ("Brio 101", "uvcvideo", 0),
        "video1": ("Brio 101", "uvcvideo", 1),
        "video19": ("rpi-hevc-dec", "rpi-hevc-dec", 0),
        "video20": ("pispbe-input", "pispbe", 0),
        "video9": ("pispbe-config", "pispbe", 0),
    }

    def test_the_webcam_comes_first(self):
        self.assertEqual(self.candidates(self.LAYOUT)[0], "video0")

    def test_the_capture_node_beats_the_metadata_node(self):
        # Both are the Brio and both open cleanly. Only index 0 ever produces
        # an image, and trying index 1 first costs a warm-up before failing.
        self.assertEqual(self.candidates(self.LAYOUT)[:2], ["video0", "video1"])

    def test_the_isp_is_never_tried(self):
        # Eighteen of this Pi's twenty nodes are the ISP and the HEVC decoder.
        # They cannot produce a camera frame and each costs about two seconds
        # to refuse, which is why they are dropped rather than ranked last:
        # with them in, a camera that was merely busy measured at 81 s of
        # startup during which the assistant could not hear.
        self.assertEqual(self.candidates(self.LAYOUT), ["video0", "video1"])

    def test_a_renamed_camera_is_still_found(self):
        # A hint that no longer matches anything — a renamed product, a kernel
        # that reports it differently — must not cost the camera, because the
        # driver is the thing that actually says what the node is.
        self.assertEqual(self.candidates(self.LAYOUT, name_hint="Kinect"),
                         ["video0", "video1"])

    def test_a_camera_on_another_driver_is_reachable_by_name(self):
        # The escape hatch for hardware that is not UVC — a CSI camera, a
        # virtual device — without having to name a node that moves.
        layout = {"video0": ("pispbe-input", "pispbe", 0),
                  "video8": ("My CSI Camera", "rp1-cfe", 0)}
        self.assertEqual(self.candidates(layout, name_hint="CSI"), ["video8"])

    def test_no_camera_at_all_is_an_empty_list_not_a_long_search(self):
        # An unplugged cable must be an immediate degraded start, not a boot
        # spent opening the ISP one node at a time.
        layout = {"video20": ("pispbe-input", "pispbe", 0),
                  "video19": ("rpi-hevc-dec", "rpi-hevc-dec", 0)}
        self.assertEqual(self.candidates(layout), [])

    def test_the_name_decides_between_two_webcams(self):
        layout = {
            "video0": ("HD Pro Webcam C920", "uvcvideo", 0),
            "video2": ("Brio 101", "uvcvideo", 0),
        }
        self.assertEqual(self.candidates(layout)[0], "video2")

    def test_a_named_device_is_never_second_guessed(self):
        # Configuring a node and silently getting a different camera is the
        # failure discovered from a description of the wrong room.
        self.assertEqual(self.candidates(self.LAYOUT, device="/dev/video1"),
                         ["video1"])

    def test_a_bare_index_is_passed_through(self):
        self.assertEqual(self.candidates(self.LAYOUT, device="2"), ["2"])


class TestDrainDepth(unittest.TestCase):
    """How many grabs it takes to reach a frame captured *now*.

    One more than the queue is deep: the queued buffers were all filled just
    after the previous read — the driver drops frames once its queue is full
    and nobody is reading — so reaching a live frame means emptying the queue
    and then blocking for one. Getting this wrong is not visible as a bug. It
    is a description of a room as it was half a second ago, and 140 ms added
    to every "what do you see" on the camera this ships with.
    """

    def test_a_single_buffer_needs_two_grabs(self):
        # What the Brio's driver actually honours.
        self.assertEqual(camera_mod.Camera._drain_for(1.0), 2)

    def test_the_four_deep_default_needs_five(self):
        self.assertEqual(camera_mod.Camera._drain_for(4.0), 5)

    def test_an_unimplemented_property_falls_back(self):
        # OpenCV answers 0 or -1 for properties a backend does not implement,
        # and neither means "there is no queue".
        for answer in (0.0, -1.0, None, "unsupported"):
            with self.subTest(answer=answer):
                self.assertEqual(camera_mod.Camera._drain_for(answer),
                                 camera_mod.DEFAULT_DRAIN)

    def test_an_absurd_depth_is_capped(self):
        # A capture that blocks for 64 frames is a turn that has already lost.
        self.assertEqual(camera_mod.Camera._drain_for(64), camera_mod.MAX_DRAIN)


class TestCapture(unittest.TestCase):

    def test_a_capture_reads_as_a_data_url(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.jpg"
            path.write_bytes(b"\xff\xd8\xff not really a jpeg")
            url = camera_mod.Capture(path, 0.0, 1280, 720).as_data_url()
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
            self.assertEqual(
                base64.b64decode(url.split(",", 1)[1]), path.read_bytes())

    def test_a_capture_whose_file_vanished_is_none_not_an_exception(self):
        # /dev/shm is pruned by this module and cleared by a reboot. A missing
        # file must degrade to "no picture", because the alternative is an
        # exception on the voice path.
        gone = camera_mod.Capture(Path("/nonexistent/shot.jpg"), 0.0, 1280, 720)
        self.assertIsNone(gone.as_data_url())


class TestDegradedModes(unittest.TestCase):
    """Section 37: a camera that will not start must not stop the assistant."""

    def test_a_disabled_camera_never_opens_and_says_why(self):
        cam = camera_mod.Camera(CameraConfig(enabled=False))
        self.assertFalse(cam.open())
        self.assertFalse(cam.available())
        self.assertIn("disabled", cam.error)

    def test_no_opencv_is_a_log_line_and_not_an_import_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cam = camera_mod.Camera(CameraConfig(scratch=Path(tmp)))
            # A None in sys.modules is what an uninstalled module looks like to
            # `import`: an ImportError at the import statement, which is where
            # this is caught.
            with mock.patch.dict("sys.modules", {"cv2": None}):
                self.assertFalse(cam.open())
        self.assertIn("OpenCV", cam.error)

    def test_reading_a_camera_that_never_opened_is_none(self):
        cam = camera_mod.Camera(CameraConfig(enabled=False))
        self.assertIsNone(cam.frame())
        self.assertIsNone(cam.capture_still())


class TestLendingTheCameraToACall(unittest.TestCase):
    """Handing the Brio to Chromium and getting it back.

    The failure this exists for was measured on the device: the call ended,
    Python asked for the camera back while the browser still had it, `open()`
    failed, and nothing retried. The assistant then had no camera for the rest
    of the session — no person detection and no screensaver — from a call that
    had ended perfectly normally.
    """

    def setUp(self):
        from aipi5.core.config import CameraConfig
        from aipi5.vision.camera import Camera
        self.camera = Camera(CameraConfig(enabled=True))
        # Stand in for a device. `open()` is what contends for the node, so
        # that is what the fake controls.
        self.opens = 0
        self.free_after = 0        # how many failed opens before it works

        def fake_open():
            self.opens += 1
            if self.opens <= self.free_after:
                self.camera._error = "no camera on /dev/video0"
                return False
            self.camera._started = True
            self.camera._error = None
            return True

        self.camera.open = fake_open
        self.camera._started = True

    def test_lending_closes_the_handle(self):
        self.camera.lend("a video call")
        self.assertTrue(self.camera.lent)
        self.assertFalse(self.camera.available())

    def test_the_detector_cannot_reopen_it_while_it_is_lent(self):
        # The race that would otherwise take the picture away from the person
        # on the other end: the person detector polls twice a second and would
        # keep trying for the node all through a call.
        from aipi5.core.config import CameraConfig
        from aipi5.vision.camera import Camera
        camera = Camera(CameraConfig(enabled=True))
        camera._started = True
        camera.lend("a video call")
        self.assertFalse(camera.open(), "a lent camera must refuse to open")

    def test_lending_twice_still_needs_one_reclaim(self):
        # A call that reconnects lends again; it is not a second borrower.
        self.camera.lend("a video call")
        self.camera.lend("a video call")
        self.camera.reclaim()
        self.assertFalse(self.camera.lent)
        self.assertTrue(self.camera.available())

    def test_a_reclaim_that_wins_first_time_is_done(self):
        self.camera.lend("a video call")
        self.assertTrue(self.camera.reclaim())
        self.assertFalse(self.camera.reclaiming)

    def test_a_reclaim_the_browser_blocks_is_retried_until_it_wins(self):
        # The measured case. Two failures, then the node comes free.
        self.free_after = 2
        self.camera.lend("a video call")
        self.assertFalse(self.camera.reclaim(), "the browser still has it")
        self.assertTrue(self.camera.reclaiming, "a retry must be armed")

        self.assertFalse(self.camera.retry_reclaim(immediately=True))
        self.assertTrue(self.camera.retry_reclaim(immediately=True))
        self.assertTrue(self.camera.available())
        self.assertFalse(self.camera.reclaiming)

    def test_retrying_is_free_when_there_is_nothing_to_do(self):
        # It runs on the voice loop's idle path, every frame.
        before = self.opens
        for _ in range(50):
            self.camera.retry_reclaim()
        self.assertEqual(self.opens, before, "an idle retry must not open anything")

    def test_retries_are_rate_limited(self):
        self.free_after = 99
        self.camera.lend("a video call")
        self.camera.reclaim()
        opens = self.opens
        for _ in range(20):
            self.camera.retry_reclaim()      # not `immediately`
        self.assertEqual(self.opens, opens,
                         "retries must not run once per frame")

    def test_it_gives_up_rather_than_warning_forever(self):
        # An unplugged camera must produce one error, not a warning every two
        # seconds until somebody notices.
        from aipi5.vision import camera as camera_mod
        self.free_after = 99
        self.camera.lend("a video call")
        self.camera.reclaim()
        self.camera._reclaim_until = time.monotonic() - 1     # window elapsed
        self.assertFalse(self.camera.retry_reclaim(immediately=True))
        self.assertFalse(self.camera.reclaiming, "it must stop trying")
        self.assertGreater(camera_mod.RECLAIM_WINDOW_S, camera_mod.RECLAIM_RETRY_S)

    def test_describe_says_who_has_it(self):
        self.camera.lend("a video call")
        self.assertEqual(self.camera.describe()["lent_to"], "a video call")


class TestTheCameraBeingUnplugged(unittest.TestCase):
    """A cable knocked out, and put back.

    Measured on the device before this worked: unbinding the Brio left the
    assistant reporting `running=True` with a handle to a device that no longer
    existed — no frames, no error, and a settings page that said everything was
    fine. Replugging did not help, because the by-name search only runs inside
    `open()` and nothing called it again.

    The replug detail that makes the search matter: the Brio came back as
    **/dev/video1, not /dev/video0**. Node numbers are handed out in order of
    arrival, so a reopen that assumed the old node would have failed even with
    the camera sitting there working.
    """

    def setUp(self):
        from aipi5.core.config import CameraConfig
        from aipi5.vision.camera import Camera
        self.camera = Camera(CameraConfig(enabled=True))
        self.present = True
        self.opens = 0

        def fake_open():
            self.opens += 1
            if not self.present:
                self.camera._error = "no USB camera found — check the cable"
                return False
            self.camera._started = True
            self.camera._error = None
            return True

        self.camera.open = fake_open
        self.camera._started = True

    def unplug(self):
        self.present = False
        with self.camera._lock:
            self.camera._mark_lost("it stopped delivering frames")

    def test_losing_the_camera_stops_it_claiming_to_be_available(self):
        # The bug in its simplest form: a settings page that lies.
        self.assertTrue(self.camera.available())
        self.unplug()
        self.assertFalse(self.camera.available(),
                         "a camera that is gone must not report running")
        self.assertTrue(self.camera.describe()["lost"])

    def test_it_keeps_looking_and_recovers_when_replugged(self):
        self.unplug()
        self.assertFalse(self.camera.retry_reclaim())
        self.present = True                       # the cable goes back in
        self.camera._last_attempt = 0.0           # let the next attempt run
        self.assertTrue(self.camera.retry_reclaim())
        self.assertTrue(self.camera.available())
        self.assertFalse(self.camera.lost)

    def test_it_does_not_give_up_the_way_a_reclaim_does(self):
        # A borrowed camera comes back in seconds or something is wrong. An
        # unplugged one comes back when somebody plugs it in, which may be
        # tomorrow — so this must still be trying long after a reclaim would
        # have stopped.
        from aipi5.vision import camera as camera_mod
        self.unplug()
        self.camera._lost_since = time.monotonic() - (camera_mod.RECLAIM_WINDOW_S * 10)
        self.camera._last_attempt = 0.0
        before = self.opens
        self.camera.retry_reclaim()
        self.assertGreater(self.opens, before, "it must still be looking")
        self.assertTrue(self.camera.lost)

    def test_it_slows_down_rather_than_warning_every_two_seconds(self):
        from aipi5.vision import camera as camera_mod
        self.unplug()
        self.camera._lost_since = time.monotonic() - (camera_mod.LOST_PATIENCE_S + 5)
        self.camera._last_attempt = time.monotonic() - 3      # 3s ago
        before = self.opens
        self.camera.retry_reclaim()
        self.assertEqual(self.opens, before,
                         "past the patience window it should wait ~30s, not 2s")

    def test_an_idle_retry_costs_nothing_when_the_camera_is_fine(self):
        # It runs on the voice loop's idle path, every frame.
        before = self.opens
        for _ in range(50):
            self.camera.retry_reclaim()
        self.assertEqual(self.opens, before)

    def test_losing_it_twice_does_not_reset_the_clock(self):
        self.unplug()
        first = self.camera._lost_since
        with self.camera._lock:
            self.camera._mark_lost("again")
        self.assertEqual(self.camera._lost_since, first,
                         "already-lost must be a no-op, or the backoff never "
                         "reaches its slow cadence")


if __name__ == "__main__":
    unittest.main()
