"""The deploy script must not ship a configuration over a deployed one.

There is no way to unit-test an ssh-and-tar script honestly, so this checks
the two properties whose absence caused a real outage rather than pretending
to exercise the transfer:

  1. the tar excludes `config/aipi5.yaml` when the device already has one;
  2. the repository's copy still reaches the device, under another name, so a
     deploy is not silently withholding new settings either.

What happened, 2026-08-13: a deploy replaced the Pi's `config/aipi5.yaml` with
the repository's. The repository ships `call.enabled: false` deliberately, and
the device's copy also had `host: 127.0.0.1` and `tls: false` because
`tailscale serve` terminates TLS in front of the call server. Result: video
calling switched off, and the call server answering TLS on an address the
tailnet proxy speaks plain HTTP to — the phone got a blank white page, and
nothing in any log connected it to a deploy.

A source check is a weak test and is the right weight here: the failure mode
is somebody simplifying the copy step back to an unconditional tar, and that
is exactly what this notices.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"


class TestDeployProtectsTheDeviceConfig(unittest.TestCase):

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_the_script_exists_and_is_bash(self):
        self.assertTrue(SCRIPT.is_file(), SCRIPT)
        self.assertTrue(self.text.startswith("#!"), "no shebang")

    def test_it_checks_whether_the_device_already_has_a_config(self):
        # The decision has to be made before the tar is built, because it
        # changes what the tar may contain.
        self.assertRegex(
            self.text,
            r"test -f ~/\$REMOTE/\$PROTECTED",
            "nothing asks whether the device already has a configuration")

    def test_the_tar_can_exclude_the_device_config(self):
        self.assertIn('EXCLUDES+=("--exclude=$PROTECTED")', self.text,
                      "the tar cannot exclude the deployed configuration, so "
                      "a deploy will overwrite it again")

    def test_the_repository_copy_still_arrives_under_another_name(self):
        # Withholding new settings entirely would be its own bug: somebody
        # adding a config block would find it never reached the device.
        self.assertRegex(
            self.text, r"scp .*\$PROTECTED.*\$PROTECTED\.new",
            "the repository's configuration never reaches the device at all")

    def test_the_protected_path_is_the_settings_file(self):
        match = re.search(r"^PROTECTED=(\S+)", self.text, re.M)
        self.assertIsNotNone(match, "PROTECTED is not defined")
        self.assertEqual(match.group(1), "config/aipi5.yaml")

    def test_a_first_deploy_still_installs_a_configuration(self):
        # A device with nothing must still get the defaults, or a fresh
        # install comes up with no settings at all.
        self.assertIn("installed $PROTECTED (the device had none)", self.text)

    def test_the_dry_run_says_which_way_it_will_go(self):
        self.assertIn("would KEEP the device's $PROTECTED", self.text)


class TestTheDefaultsStillDifferFromADeployment(unittest.TestCase):
    """The reason the protection is needed, asserted rather than assumed.

    **Not by reading `config/aipi5.yaml`.** The first version of this test did,
    and it passed on the development machine and failed on the Pi — because on
    a device that file *is* the deployment, with calling enabled. Which is
    exactly the distinction the deploy fix exists to draw, so the test proved
    the point by getting it wrong. The dataclass default is the same claim
    everywhere.
    """

    def test_calling_is_off_by_default(self):
        from aipi5.core.config import CallConfig
        self.assertFalse(
            CallConfig().enabled,
            "calling now defaults to on — a deploy would then switch it on "
            "for every device that had deliberately left it off")


if __name__ == "__main__":
    unittest.main()
