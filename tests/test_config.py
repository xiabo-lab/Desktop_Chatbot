"""Loading the configuration, and refusing to be silently wrong about it.

Two rules pulled apart deliberately: a *missing* file is defaults, because
every default here is the value the specification asks for and a deployment
that lost its YAML should come back as the assistant it was; a *malformed* file
raises, because that is somebody halfway through an edit and starting with
their change silently ignored is how a setting appears not to work.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aipi5.core import config as config_mod
from aipi5.core.config import ConfigError, Settings, load


class TestDefaults(unittest.TestCase):

    def test_a_missing_file_is_the_specified_assistant(self):
        settings = load(Path(tempfile.gettempdir()) / "aipi5-does-not-exist.yaml")
        self.assertEqual((settings.display.width, settings.display.height),
                         (1280, 800))
        self.assertEqual(settings.location.zip, "95127")
        self.assertEqual(settings.screensaver.timeout_seconds, 60.0)
        self.assertEqual(settings.games.round_seconds, 120)
        self.assertEqual(
            (settings.motion.capture_width, settings.motion.capture_height,
             settings.motion.capture_fps),
            (1280, 720, 90),
        )
        self.assertTrue(settings.news.feeds, "news needs somewhere to read from")

    def test_the_shipped_file_loads(self):
        settings = load()
        self.assertEqual((settings.display.width, settings.display.height),
                         (1280, 800))
        self.assertEqual(settings.location.label, "San Jose, CA 95127")
        self.assertEqual(settings.openai.model, "gpt-5.6-luna")


class TestParsing(unittest.TestCase):

    def write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                             encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_malformed_yaml_raises(self):
        with self.assertRaises(ConfigError):
            load(self.write("display: [1, 2\n  broken"))

    def test_a_section_that_is_not_a_block_raises(self):
        with self.assertRaises(ConfigError):
            load(self.write("display: just a string\n"))

    def test_an_empty_file_is_defaults(self):
        self.assertEqual(load(self.write("")).display.width, 1280)

    def test_zero_is_rejected_where_it_would_be_a_busy_loop(self):
        # `cache_seconds: 0` means every screensaver tick hits the weather API
        # and `interval_ms: 0` is a spin on a device whose whole design is
        # about leaving cores free. Both look like a plausible thing to type.
        settings = load(self.write(
            "weather:\n  cache_seconds: 0\n"
            "person_detection:\n  interval_ms: 0\n"))
        self.assertEqual(settings.weather.cache_seconds, 600.0)
        self.assertEqual(settings.person.interval_ms, 500)

    def test_a_debounce_of_zero_frames_is_raised_to_one(self):
        settings = load(self.write(
            "person_detection:\n  frames_to_appear: 0\n  frames_to_disappear: 0\n"))
        self.assertEqual(settings.person.frames_to_appear, 1)
        self.assertEqual(settings.person.frames_to_disappear, 1)

    def test_relative_model_paths_resolve_against_the_project(self):
        settings = load(self.write(
            "person_detection:\n  model: models/other.hef\n"))
        self.assertTrue(settings.person.model.is_absolute())
        self.assertEqual(settings.person.model.name, "other.hef")

    def test_a_single_feed_may_be_written_as_a_string(self):
        settings = load(self.write("news:\n  feeds: https://example.com/rss\n"))
        self.assertEqual(settings.news.feeds, ("https://example.com/rss",))

    def test_celsius(self):
        settings = load(self.write("location:\n  units: celsius\n"))
        self.assertEqual(settings.location.temperature_unit, "celsius")
        self.assertEqual(settings.location.degree_symbol, "C")

    def test_the_vision_model_follows_the_main_one_by_default(self):
        settings = load(self.write("openai:\n  model: some-model\n"))
        self.assertEqual(settings.openai.vision, "some-model")

    def test_the_vision_model_can_be_named_separately(self):
        settings = load(self.write(
            "openai:\n  model: some-model\n  vision_model: other-model\n"))
        self.assertEqual(settings.openai.vision, "other-model")

    def test_game_round_duration_accepts_only_the_five_menu_choices(self):
        self.assertEqual(load(self.write(
            "games:\n  round_seconds: 300\n")).games.round_seconds, 300)
        self.assertEqual(load(self.write(
            "games:\n  round_seconds: 90\n")).games.round_seconds, 120)
        self.assertEqual(load(self.write(
            "games:\n  round_seconds: forever\n")).games.round_seconds, 120)

    def test_motion_camera_mode_is_configurable_and_positive(self):
        settings = load(self.write(
            "motion:\n"
            "  capture_width: 1920\n"
            "  capture_height: 1080\n"
            "  capture_fps: 60\n"))
        self.assertEqual(
            (settings.motion.capture_width, settings.motion.capture_height,
             settings.motion.capture_fps),
            (1920, 1080, 60),
        )

        fallback = load(self.write(
            "motion:\n"
            "  capture_width: 0\n"
            "  capture_height: -1\n"
            "  capture_fps: 0\n"))
        self.assertEqual(
            (fallback.motion.capture_width, fallback.motion.capture_height,
             fallback.motion.capture_fps),
            (1280, 720, 90),
        )


class TestTheShippedConfigMatchesTheCamera(unittest.TestCase):
    """Every mode `config/aipi5.yaml` asks for is one the BRIO 4K can do.

    Read off the installed camera on 2026-08-17 with `v4l2-ctl -d /dev/video0
    --list-formats-ext`, and kept here because a resolution the camera does not
    advertise does not fail: V4L2 quietly gives you the nearest one it has, and
    the first sign is a game that feels slow or a still that is the wrong shape.

    Two facts changed with the camera itself and are the reason this table is
    written down rather than remembered. The Brio 101 this replaced offered only
    YUYV and MJPG and capped **YUYV 720p at 5 fps** — so the old rule was "ask
    for MJPEG at 720p or silently get 5 fps". This camera adds NV12 and does
    YUYV 720p at a full 30. MJPEG is still what the high rates need.
    """

    #: (format, width, height) -> the highest frame rate the device advertises.
    MODES = {
        ("MJPG", 640, 480): 120,
        ("MJPG", 1280, 720): 90,
        ("MJPG", 1920, 1080): 60,
        ("MJPG", 3840, 2160): 30,
        ("MJPG", 640, 360): 30,
        ("YUYV", 1280, 720): 30,
        ("YUYV", 640, 480): 30,
        ("NV12", 1280, 720): 30,
        ("NV12", 640, 360): 30,
    }

    def setUp(self):
        if not (config_mod.ROOT / ".git").exists():
            self.skipTest("a deployment, not a checkout — the YAML is a local file")
        self.settings = load()

    def assert_reachable(self, what, width, height, fps):
        best = self.MODES.get(("MJPG", width, height))
        self.assertIsNotNone(
            best, f"{what}: the camera advertises no {width}x{height} MJPEG mode")
        self.assertLessEqual(
            fps, best,
            f"{what}: {width}x{height} tops out at {best} fps on this camera")

    def test_the_assistants_still_capture_is_a_real_mode(self):
        camera = self.settings.camera
        self.assert_reachable("camera", camera.capture_width,
                              camera.capture_height, camera.fps)

    def test_the_gameplay_capture_is_a_real_mode(self):
        # The one that motivated the new camera: 720p90 is what lets the pose
        # loop have a fresh frame every time it asks. See `motion/camera_lease`.
        motion = self.settings.motion
        self.assert_reachable("motion", motion.capture_width,
                              motion.capture_height, motion.capture_fps)
        self.assertEqual((motion.capture_width, motion.capture_height,
                          motion.capture_fps), (1280, 720, 90))

    def test_the_call_capture_is_a_real_mode(self):
        call = self.settings.call
        self.assert_reachable("call", call.width, call.height, call.fps)

    def test_the_pose_loop_is_not_asked_for_more_than_the_accelerator_gives(self):
        # 90 is the camera's number, 30 is the Hailo's. Asking the pose loop for
        # the camera's rate would be asking for frames nothing can infer.
        self.assertLessEqual(self.settings.motion.target_fps,
                             self.settings.motion.capture_fps)

    def test_the_name_hint_still_matches_the_installed_camera(self):
        # Card name "Logitech BRIO"; the search lowercases both sides.
        self.assertIn(self.settings.camera.name_hint.lower(),
                      "logitech brio")


class TestAiaConfig(unittest.TestCase):

    def test_aias_own_web_ui_is_turned_off(self):
        # AIPI5 serves its own page from the same conversation database. AIA's
        # would work and would be a second thing to find, at a second address,
        # in a layout designed for a 1920x440 strip.
        self.assertFalse(load().aia_config().ui.enabled)

    def test_retention_follows_this_projects_setting(self):
        settings = Settings()
        object.__setattr__(settings.assistant, "retention_hours", 12.0)
        self.assertEqual(settings.aia_config().retention.hours, 12.0)

    def test_the_measured_audio_settings_are_untouched(self):
        # Everything about sound stays AIA's, exactly as measured on this
        # hardware. If this test ever fails, something has started overriding
        # numbers that were derived from real captures.
        from aia.core.config import CONFIG as AIA_CONFIG
        aia = load().aia_config()
        self.assertEqual(aia.audio, AIA_CONFIG.audio)
        self.assertEqual(aia.vad, AIA_CONFIG.vad)
        self.assertEqual(aia.wake, AIA_CONFIG.wake)
        self.assertEqual(aia.tts, AIA_CONFIG.tts)


class TestCredentials(unittest.TestCase):

    def test_the_environment_wins(self):
        os.environ["OPENAI_API_KEY"] = "sk-from-the-environment"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        self.assertEqual(config_mod.credentials(), "sk-from-the-environment")
        self.assertEqual(config_mod.describe_credentials()["source"],
                         "OPENAI_API_KEY")

    def test_a_key_file_is_read_when_the_environment_is_empty(self):
        os.environ.pop("OPENAI_API_KEY", None)
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                             encoding="utf-8")
        handle.write("sk-proj-abcdefghijkl\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        os.environ["OPENAI_API_KEY_FILE"] = handle.name
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY_FILE", None)
        self.assertEqual(config_mod.credentials(), "sk-proj-abcdefghijkl")

    def test_a_file_that_is_not_a_key_is_ignored(self):
        # Somebody's notes, or a shell export line. Rejected here, where the
        # message can name the file, rather than at the API where it comes back
        # as a 401 that reads like the key was revoked.
        os.environ.pop("OPENAI_API_KEY", None)
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                             encoding="utf-8")
        handle.write("export OPENAI_API_KEY=sk-nope\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        os.environ["OPENAI_API_KEY_FILE"] = handle.name
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY_FILE", None)
        self.assertEqual(config_mod.credentials(), "")

    def test_the_key_itself_is_never_described(self):
        os.environ["OPENAI_API_KEY"] = "sk-proj-secret-value-1234"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        described = config_mod.describe_credentials()
        self.assertEqual(described["suffix"], "1234")
        self.assertNotIn("secret", repr(described))

    def test_every_key_filename_is_gitignored(self):
        # The whole of "never commit API credentials", as a test rather than a
        # comment. KEY_FILES growing a name that is not in .gitignore is the
        # exact mistake this catches.
        ignored = (config_mod.ROOT / ".gitignore").read_text(encoding="utf-8")
        patterns = {line.strip() for line in ignored.splitlines()}
        for name in config_mod.KEY_FILES:
            self.assertIn(name, patterns, f"{name} can be read but is not ignored")


if __name__ == "__main__":
    unittest.main()
