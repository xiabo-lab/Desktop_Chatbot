"""Offline calendar arithmetic assets and reboot-safe birthday storage."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aipi5.calendar import BirthdayError, BirthdayStore
ROOT = Path(__file__).resolve().parent.parent
NODE_SUITE = ROOT / "tests" / "calendar.test.mjs"


class TestBirthdayStore(unittest.TestCase):

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "birthdays.json"
        self.store = BirthdayStore(self.path)

    def birthday(self, **changes):
        row = {"name": "妈妈", "calendar": "lunar", "month": 8, "day": 15,
               "leap": False, "year": 1980, "note": "family dinner"}
        row.update(changes)
        return row

    def test_add_edit_delete_and_reload(self):
        saved = self.store.save(self.birthday())
        self.assertTrue(saved["id"])
        self.assertEqual(saved["name"], "妈妈")

        restarted = BirthdayStore(self.path)
        self.assertEqual(restarted.list(), [saved])
        edited = restarted.save(self.birthday(id=saved["id"], note="new note"))
        self.assertEqual(edited["id"], saved["id"])
        self.assertEqual(edited["note"], "new note")

        restarted.delete(saved["id"])
        self.assertEqual(BirthdayStore(self.path).list(), [])

    def test_lunar_fields_are_preserved_not_flattened_to_one_solar_date(self):
        saved = self.store.save(self.birthday(month=4, day=1, leap=True))
        disk = json.loads(self.path.read_text(encoding="utf-8"))["birthdays"][0]
        self.assertEqual(disk["calendar"], "lunar")
        self.assertEqual((disk["month"], disk["day"], disk["leap"]), (4, 1, True))
        self.assertNotIn("gregorian", disk)
        self.assertEqual(saved, disk)

    def test_validation_rejects_bad_or_missing_values(self):
        bad = (
            self.birthday(name=""), self.birthday(calendar="martian"),
            self.birthday(month=13), self.birthday(day=31),
            self.birthday(calendar="solar", month=2, day=30),
        )
        for row in bad:
            with self.subTest(row=row), self.assertRaises(BirthdayError):
                self.store.save(row)
        self.assertEqual(self.store.list(), [])

    def test_unknown_edits_and_deletes_are_refused(self):
        with self.assertRaises(BirthdayError):
            self.store.save(self.birthday(id="missing"))
        with self.assertRaises(BirthdayError):
            self.store.delete("missing")

    def test_failed_disk_write_does_not_change_the_in_memory_list(self):
        blocker = Path(self.folder.name) / "not-a-folder"
        blocker.write_text("occupied", encoding="utf-8")
        store = BirthdayStore(blocker / "birthdays.json")
        with self.assertRaises(BirthdayError):
            store.save(self.birthday())
        self.assertEqual(store.list(), [])


class TestCalendarAssets(unittest.TestCase):

    def test_home_navigation_and_timer_lifecycle_are_wired(self):
        page = (ROOT / "aipi5" / "ui" / "web" / "index.html").read_text(
            encoding="utf-8")
        script = (ROOT / "aipi5" / "ui" / "web" / "assets" / "calendar"
                  / "calendar.js").read_text(encoding="utf-8")
        weather = page.index('data-action="weather"')
        calendar = page.index('data-page="calendar"')
        music = page.index('data-action="kodama"')
        self.assertLess(weather, calendar)
        self.assertLess(calendar, music)
        self.assertIn('if (previous === "calendar") stopCalendar();', page)
        self.assertIn('if (page === "calendar") startCalendar();', page)
        self.assertIn("const AUTO_RETURN_MS = 5 * 60 * 1000;", script)
        self.assertIn("clearReturnTimer();", script)
        self.assertIn("clearMidnightTimer();", script)
        self.assertIn('page.dataset.inactivityTimers = state.returnTimer ? "1" : "0";',
                      script)

    def test_required_chinese_labels_and_color_classes_are_present(self):
        page = (ROOT / "aipi5" / "ui" / "web" / "index.html").read_text(
            encoding="utf-8")
        core = (ROOT / "aipi5" / "ui" / "web" / "assets" / "calendar"
                / "calendar-core.js").read_text(encoding="utf-8")
        css = (ROOT / "aipi5" / "ui" / "web" / "assets" / "calendar"
               / "calendar.css").read_text(encoding="utf-8")
        for label in ("美国节日", "中国节日", "生日"):
            self.assertIn(label, page)
        for holiday in ("元旦", "春节", "元宵节", "清明节", "端午节", "中秋节", "国庆节"):
            self.assertIn(holiday, core)
        self.assertIn(".cal-event.holiday", css)
        self.assertIn("color: var(--cal-red)", css)
        self.assertIn(".cal-event.birthday", css)
        self.assertIn("color: var(--cal-green)", css)

    def test_birthday_management_and_weekend_styling_are_wired(self):
        page = (ROOT / "aipi5" / "ui" / "web" / "index.html").read_text(
            encoding="utf-8")
        script = (ROOT / "aipi5" / "ui" / "web" / "assets" / "calendar"
                  / "calendar.js").read_text(encoding="utf-8")
        css = (ROOT / "aipi5" / "ui" / "web" / "assets" / "calendar"
               / "calendar.css").read_text(encoding="utf-8")

        self.assertNotIn('id="cal-add-birthday"', page)
        self.assertIn('id="cal-birthday-settings"', page)
        self.assertIn('id="birthday-settings-list"', page)
        self.assertIn('id="birthday-settings-add"', page)
        self.assertIn("function renderBirthdaySettings()", script)
        self.assertIn("function openBirthdaySettings()", script)
        self.assertNotIn('create("cal-row-actions")', script)
        self.assertIn('cell.date.getDay() === 0 || cell.date.getDay() === 6', script)
        self.assertIn('button.setAttribute("aria-hidden", "true")', script)
        self.assertIn("#cal-grid .cal-day.weekend", css)
        self.assertIn("rgba(255, 255, 255, .78)", css)
        self.assertIn("#cal-grid .cal-day.outside { visibility: hidden; }", css)
        self.assertIn(".cal-day.selected:not(.today)", css)
        self.assertNotIn("background: #153251", css)
        self.assertIn(".cal-day.today { outline: 3px solid var(--cal-red)", css)
        self.assertNotIn("background: var(--cal-blue)", css)
        # Regex rather than a contiguous string: the bilingual sweep put a
        # `data-i18n` attribute between the marker and the text. That the Back
        # button carries `data-back` and says this is what matters.
        self.assertRegex(page, r'data-back[^>]*>Back to Home Menu</button>')
        self.assertRegex(
            page,
            r'id="cal-today" class="cal-back-today"[^>]*>Back to Today</button>')
        self.assertIn("grid-template-columns: 238px 1fr 190px", css)

    def test_every_calendar_asset_is_served_locally(self):
        from aipi5.ui.server import asset_file

        for url in ("/assets/calendar/calendar.css",
                    "/assets/calendar/calendar-core.js",
                    "/assets/calendar/calendar.js",
                    "/assets/calendar/solarlunar.min.js"):
            with self.subTest(url=url):
                resolved = asset_file(url)
                self.assertIsNotNone(resolved)
                self.assertGreater(resolved.stat().st_size, 0)

    @unittest.skipUnless(shutil.which("node"), "node is not installed here")
    def test_offline_calendar_rules_under_node(self):
        result = subprocess.run(["node", "--test", str(NODE_SUITE)],
                                cwd=ROOT, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
