"""Reading the device off the device.

Two properties matter here and they pull against each other. It has to answer
with real numbers on the Pi, and it has to answer *something* everywhere else —
this module is imported by `/api/system`, which is polled every four seconds by
a settings page, and it runs on a Windows development machine that has no
`/proc`, no thermal zone and no `vcgencmd`.

So: every field optional, nothing raises, and a reader that fails costs its own
row rather than the twelve beside it.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from aipi5.core import hardware

ROOT = Path(__file__).resolve().parent.parent

MEMINFO = """\
MemTotal:        8140728 kB
MemFree:          412332 kB
MemAvailable:    5220612 kB
Buffers:          198472 kB
SwapTotal:       2097148 kB
SwapFree:        2097148 kB
"""

CPUINFO = """\
processor\t: 0
model name\t: Cortex-A76
Hardware\t: BCM2835
Serial\t\t: 7634bbcbf77f06ab
Model\t\t: Raspberry Pi 5 Model B Rev 1.0
"""


class TestItNeverThrows(unittest.TestCase):
    """The reason every reader is wrapped. A settings page that raises is a
    blank panel with a traceback nobody on a kiosk can see."""

    def test_a_snapshot_on_this_machine_is_a_dict(self):
        self.assertIsInstance(hardware.snapshot(), dict)

    def test_a_machine_with_none_of_the_files_still_answers(self):
        missing = Path("/nonexistent/aipi5")
        with (mock.patch.object(hardware, "MEMINFO", missing),
              mock.patch.object(hardware, "CPUINFO", missing),
              mock.patch.object(hardware, "THERMAL", missing),
              mock.patch.object(hardware, "UPTIME", missing),
              mock.patch.object(hardware, "MODEL", missing)):
            facts = hardware.snapshot()
        self.assertIsInstance(facts, dict)
        for absent in ("model", "serial", "cpu_temp_c", "uptime_s"):
            with self.subTest(field=absent):
                self.assertNotIn(absent, facts)

    def test_one_reader_blowing_up_costs_only_its_own_row(self):
        def boom():
            raise RuntimeError("the thermal zone caught fire, ironically")

        with mock.patch.object(hardware, "_temperature", boom):
            facts = hardware.snapshot()
        self.assertNotIn("cpu_temp_c", facts)
        self.assertIn("hostname", facts)      # everything else survived

    def test_a_field_it_could_not_read_is_absent_rather_than_none(self):
        """So the page asks "is this key here" instead of every row repeating
        a null check."""
        with mock.patch.object(hardware, "MODEL", Path("/nonexistent")):
            self.assertIsNone(hardware.snapshot().get("model", None))
        for value in hardware.snapshot().values():
            self.assertIsNotNone(value)


class TestWhatItReads(unittest.TestCase):

    def setUp(self):
        self.tmp = self.enterContext(
            __import__("tempfile").TemporaryDirectory())
        self.folder = Path(self.tmp)

    def write(self, name: str, text: str) -> Path:
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_board_name_loses_its_trailing_nul(self):
        """It comes straight out of the device tree blob, NUL-terminated, and
        the raw string renders as a stray box on the page."""
        path = self.write("model", "Raspberry Pi 5 Model B Rev 1.0\x00")
        with mock.patch.object(hardware, "MODEL", path):
            self.assertEqual("Raspberry Pi 5 Model B Rev 1.0",
                             hardware.snapshot()["model"])

    def test_memory_is_bytes_and_prefers_available_over_free(self):
        """Free memory on Linux is mostly cache and reads as alarmingly small
        — the number that makes somebody reboot a device that was fine."""
        path = self.write("meminfo", MEMINFO)
        with mock.patch.object(hardware, "MEMINFO", path):
            memory = hardware.snapshot()["memory"]
        self.assertEqual(8140728 * 1024, memory["total"])
        self.assertEqual(5220612 * 1024, memory["available"])
        self.assertEqual(2097148 * 1024, memory["swap_free"])
        self.assertNotIn("free", memory)

    def test_the_serial_comes_out_of_cpuinfo(self):
        path = self.write("cpuinfo", CPUINFO)
        with mock.patch.object(hardware, "CPUINFO", path):
            self.assertEqual("7634bbcbf77f06ab", hardware.snapshot()["serial"])

    def test_the_temperature_is_millidegrees_turned_into_degrees(self):
        path = self.write("temp", "59503\n")
        with mock.patch.object(hardware, "THERMAL", path):
            self.assertEqual(59.5, hardware.snapshot()["cpu_temp_c"])

    def test_uptime_is_the_machines_not_the_assistants(self):
        """`/api/system` already carries the assistant's own. Both are shown,
        labelled apart, because "the Pi has been up nine days and the
        assistant four minutes" is what somebody needs after a restart nobody
        asked for, and one number cannot say it."""
        path = self.write("uptime", "14147.79 55301.22\n")
        with mock.patch.object(hardware, "UPTIME", path):
            self.assertEqual(14147.8, hardware.snapshot()["uptime_s"])


class TestThrottling(unittest.TestCase):
    """Under-voltage is the classic cause of "the games got slow" on a Pi with
    a HAT and an underpowered supply, and it is invisible from every other
    reading on the page.

    Read through `vcgencmd` and not from sysfs. The sysfs file was written
    first — no fork — and then measured on the device: it does not exist on
    this Pi 5's kernel, while `vcgencmd` answers. A preferred path that can
    never run is not a fallback, it is dead code.
    """

    def setUp(self):
        hardware._throttle_cache = (0.0, "")
        self.addCleanup(setattr, hardware, "_throttle_cache", (0.0, ""))

    def answer(self, stdout: str):
        return mock.patch.object(
            hardware.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout, ""))

    def test_a_healthy_device_says_nothing_at_all(self):
        """Empty, which `snapshot` drops — so the row appears only when there
        is something to say."""
        with self.answer("throttled=0x0\n"):
            self.assertNotIn("throttled", hardware.snapshot())

    def test_under_voltage_is_named(self):
        with self.answer("throttled=0x1\n"):
            self.assertEqual("under-voltage", hardware.snapshot()["throttled"])

    def test_several_at_once_are_listed_worst_first(self):
        with self.answer("throttled=0x5\n"):
            self.assertEqual("under-voltage, CPU throttled",
                             hardware.snapshot()["throttled"])

    def test_only_what_is_happening_now_not_what_happened_since_boot(self):
        """The high bits mean "has happened since boot" and are still set on a
        device that was briefly unplugged three weeks ago — not what somebody
        reading a settings page is asking about."""
        with self.answer("throttled=0x50000\n"):
            self.assertNotIn("throttled", hardware.snapshot())

    def test_no_vcgencmd_is_not_an_error(self):
        with mock.patch.object(hardware.subprocess, "run",
                               side_effect=FileNotFoundError):
            self.assertNotIn("throttled", hardware.snapshot())

    def test_a_hung_firmware_call_does_not_hang_the_page(self):
        with mock.patch.object(
                hardware.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("vcgencmd", 2.0)):
            self.assertNotIn("throttled", hardware.snapshot())

    def test_it_is_not_run_again_on_every_poll(self):
        """The settings page polls every four seconds; this forks. The cache is
        what pays for choosing the tool that works over the one that is free."""
        with self.answer("throttled=0x0\n") as run:
            for _ in range(10):
                hardware.snapshot()
        self.assertEqual(1, run.call_count)

    def test_a_missing_tool_is_remembered_too(self):
        """Otherwise the miss is repaid on every poll of a machine that will
        never have it."""
        with mock.patch.object(hardware.subprocess, "run",
                               side_effect=FileNotFoundError) as run:
            for _ in range(10):
                hardware.snapshot()
        self.assertEqual(1, run.call_count)


class TestItStaysOnItsOwnSideOfTheBoundary(unittest.TestCase):
    """`aipi5/agent/helper/ops.py::system_facts` reads the same files, in the
    process that runs as root. This is a deliberate second reader, not a
    shortcut across: none of these reads needs any privilege."""

    def setUp(self):
        self.source = (ROOT / "aipi5" / "core"
                       / "hardware.py").read_text(encoding="utf-8")

    def test_it_does_not_reach_into_the_helper(self):
        self.assertNotIn("from aipi5.agent", self.source)
        self.assertNotIn("import ops", self.source)

    def test_it_avoids_the_word_the_boundary_test_greps_for(self):
        """`tests/test_agent_boundary.py` searches whole voice-side files for
        that class name, comments included — so even explaining the
        relationship in prose has to name the module and not the class."""
        self.assertNotIn("Helper" + "Client", self.source)

    def test_it_needs_no_privilege(self):
        """Everything read here is world-readable. That is what makes the
        duplication honest rather than the helper's job smuggled across."""
        for path in (hardware.MEMINFO, hardware.CPUINFO, hardware.UPTIME,
                     hardware.THERMAL, hardware.MODEL):
            with self.subTest(path=path.as_posix()):
                # `as_posix`, because this suite also runs on Windows where a
                # `Path` renders with backslashes and every one of these would
                # fail for a reason that has nothing to do with privilege.
                self.assertRegex(path.as_posix(), r"^/(proc|sys)/")


if __name__ == "__main__":
    unittest.main()
