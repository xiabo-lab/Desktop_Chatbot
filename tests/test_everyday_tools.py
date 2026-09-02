"""The tools that do ordinary things, and the policy beside each one.

These are the ones a person actually uses — the volume, the calendar — and they
are the ones a natural sentence has to reach without anybody reading a command
list. The fast router still matches "pause" and "next" in about nine
milliseconds and never gets here; what gets here is "turn it down a bit" and
"put Mum's birthday in, the fifteenth of the eighth lunar month".

Every test in this file runs with no PipeWire, no calendar file that matters,
no microphone and no network. The handlers take already-built objects, so the
fakes below are the whole of the environment.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aipi5.assistant.consent import ConsentDesk
from aipi5.calendar.store import BirthdayStore
from aipi5.llm.tools import ToolBox


def parse(result: str) -> dict:
    return json.loads(result)


class FakeVolume:
    """`VolumeControl`'s two methods, and its failure modes."""

    def __init__(self, level=50, accepts=True, available=True, error=""):
        self.level = level
        self.accepts = accepts
        self.available = available
        self.error = error
        self.asked: list[int] = []

    def set(self, percent):
        self.asked.append(percent)
        if not self.accepts:
            return False
        self.level = percent
        return True

    def describe(self):
        return {"level": self.level, "configured": self.level,
                "available": self.available, "persistent": True,
                "error": self.error, "controls": []}


class VolumeCase(unittest.TestCase):

    def setUp(self):
        self.volume = FakeVolume()
        self.box = ToolBox(volume=self.volume)

    def call(self, tool, **args):
        return parse(self.box.call(tool, json.dumps(args)))


class TestVolume(VolumeCase):

    def test_it_reads_the_level_back(self):
        self.volume.level = 37
        self.assertEqual(37, self.call("get_master_volume")["percent"])

    def test_setting_it_reaches_the_one_sink(self):
        answer = self.call("set_master_volume", percent=30)
        self.assertTrue(answer["ok"])
        self.assertEqual([30], self.volume.asked)
        self.assertEqual(30, answer["percent"])

    def test_zero_is_a_real_level_and_not_a_missing_one(self):
        """Mute. `if not percent` would refuse it, and "mute" is one of the
        commonest things anybody says to a device in a living room."""
        answer = self.call("set_master_volume", percent=0)
        self.assertTrue(answer["ok"])
        self.assertEqual(0, answer["percent"])
        self.assertTrue(answer["muted"])

    def test_a_hundred_is_accepted(self):
        self.assertEqual(100, self.call("set_master_volume", percent=100)["percent"])

    def test_out_of_range_is_clamped_rather_than_refused(self):
        """Strict mode already bounds it in the schema, so a model sending 150
        anyway means "as loud as it goes". Refusing that to be pedantic about a
        number nobody said out loud helps nobody in the room."""
        self.assertEqual(100, self.call("set_master_volume", percent=150)["percent"])
        self.assertEqual(0, self.call("set_master_volume", percent=-20)["percent"])

    def test_something_that_is_not_a_number_is_a_sentence_not_a_crash(self):
        answer = self.call("set_master_volume", percent="loud")
        self.assertFalse(answer["ok"])
        self.assertIn("0 to 100", answer["error"])

    def test_a_level_that_could_not_be_saved_is_reported_as_a_failure(self):
        """`VolumeControl.set()` rolls the audible level back when it cannot
        persist, so a success here would be the assistant confidently
        announcing a change that had already been undone."""
        self.volume.accepts = False
        self.volume.error = "The volume could not be saved: read-only file system"
        answer = self.call("set_master_volume", percent=30)
        self.assertFalse(answer["ok"])
        self.assertIn("could not be saved", answer["error"])

    def test_what_is_read_back_is_the_device_and_not_the_request(self):
        """A sink that quietly snapped to its nearest step must not be
        reported as having taken the number it was given."""
        class Snaps(FakeVolume):
            def set(self, percent):
                self.level = round(percent / 10) * 10
                return True

        box = ToolBox(volume=Snaps())
        answer = parse(box.call("set_master_volume", '{"percent": 37}'))
        self.assertEqual(40, answer["percent"])
        self.assertEqual(37, answer["requested"])

    def test_no_pipewire_at_all_still_answers(self):
        self.volume.available = False
        self.volume.error = "The system audio output is unavailable."
        self.assertFalse(self.call("get_master_volume")["available"])

    def test_the_tools_are_not_offered_without_a_control_behind_them(self):
        names = {t["name"] for t in ToolBox().schemas()}
        self.assertNotIn("set_master_volume", names)
        self.assertNotIn("get_master_volume", names)

    def test_the_tool_never_shells_out(self):
        """The guarantee at the top of `aipi5/llm/tools.py`: there is no path
        from model output to a command line. Asserted over the source, because
        what it guards is an absence."""
        source = (Path(__file__).resolve().parent.parent / "aipi5" / "llm"
                  / "tools.py").read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "Popen", "shell=True"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, source)


class BirthdayCase(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = BirthdayStore(Path(self.directory.name) / "birthdays.json")
        self.desk = ConsentDesk()
        self.box = ToolBox(birthdays=self.store, consent=self.desk)

    def call(self, tool, **args):
        return parse(self.box.call(tool, json.dumps(args)))

    def add(self, **overrides):
        payload = {"name": "Mum", "calendar": "lunar", "month": 8, "day": 15}
        payload.update(overrides)
        return self.call("save_birthday", **payload)


class TestBirthdays(BirthdayCase):

    def test_an_empty_calendar_lists_nothing_rather_than_failing(self):
        answer = self.call("list_birthdays")
        self.assertTrue(answer["ok"])
        self.assertEqual([], answer["birthdays"])

    def test_a_lunar_birthday_is_saved_as_lunar(self):
        answer = self.add()
        self.assertTrue(answer["ok"])
        self.assertEqual("lunar", answer["saved"]["calendar"])
        self.assertEqual((8, 15), (answer["saved"]["month"],
                                   answer["saved"]["day"]))
        self.assertEqual(1, len(self.store.list()))

    def test_the_store_owns_the_validation_and_the_tool_reports_it(self):
        """A second copy of the rules in the toolbox is a second copy to get
        out of step, and the one that would drift is the copy."""
        answer = self.add(month=13)
        self.assertFalse(answer["ok"])
        self.assertIn("month", answer["error"])
        self.assertEqual([], self.store.list())

    def test_a_lunar_day_thirty_one_is_refused(self):
        self.assertFalse(self.add(day=31)["ok"])

    def test_february_the_twenty_ninth_is_allowed(self):
        # It recurs only in leap years, which the calendar page handles per
        # year. Refusing it here would lose somebody's actual birthday.
        self.assertTrue(self.add(calendar="solar", month=2, day=29)["ok"])

    def test_a_leap_month_is_only_meaningful_on_a_lunar_date(self):
        solar = self.add(calendar="solar", month=3, day=4, leap=True)
        self.assertFalse(solar["saved"]["leap"])

    def test_a_year_is_optional_and_a_null_is_not_a_zero(self):
        """Strict mode sends an explicit `null` for something the person did
        not say. Reading that as a year is a birth year of 1970."""
        answer = self.add(year=None)
        self.assertIsNone(answer["saved"]["year"])

    def test_passing_an_id_changes_the_entry_rather_than_adding_one(self):
        first = self.add()["saved"]["id"]
        self.add(id=first, name="Mother")
        entries = self.store.list()
        self.assertEqual(1, len(entries))
        self.assertEqual("Mother", entries[0]["name"])

    def test_an_id_that_does_not_exist_is_refused(self):
        answer = self.add(id="nonesuch")
        self.assertFalse(answer["ok"])
        self.assertIn("does not exist", answer["error"])

    def test_an_empty_id_adds_rather_than_looking_for_an_entry_called_nothing(self):
        """`null` becomes "" on the way in, and passing that as an id makes the
        store hunt for an entry whose id is the empty string."""
        answer = self.add(id="")
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["updated"])

    def test_the_result_says_which_calendar_this_actually_is(self):
        """A model told it can "add to the calendar" will tell somebody their
        partner will see it. This one is a JSON file on a Raspberry Pi."""
        answer = self.add()
        self.assertIn("not Google Calendar", answer["calendar"])
        schema = next(t for t in self.box.schemas()
                      if t["name"] == "save_birthday")
        self.assertIn("not Google Calendar", schema["description"])

    def test_the_schema_tells_the_model_to_ask_rather_than_guess(self):
        schema = next(t for t in self.box.schemas()
                      if t["name"] == "save_birthday")
        self.assertIn("Ask a short follow-up", schema["description"])
        self.assertIn("solar", schema["description"])
        self.assertIn("lunar", schema["description"])
        # Month and day are required, so "the fifth" alone cannot be sent at
        # all — the model has to come back and ask.
        self.assertIn("month", schema["parameters"]["required"])
        self.assertIn("day", schema["parameters"]["required"])


class TestDeletingABirthday(BirthdayCase):
    """The one thing here that cannot be undone by saying the opposite."""

    def test_nothing_is_deleted_by_the_call_itself(self):
        entry = self.add()["saved"]
        answer = self.call("delete_birthday", id=entry["id"])
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["asked"])
        self.assertFalse(answer["removed"])
        self.assertEqual(1, len(self.store.list()))

    def test_the_question_names_the_person_and_not_the_id(self):
        """The check that actually works is somebody hearing the wrong name. An
        id read out loud is not a check at all."""
        entry = self.add()["saved"]
        self.call("delete_birthday", id=entry["id"])
        self.assertIn("Mum", self.desk.waiting().question)
        self.assertNotIn(entry["id"], self.desk.waiting().question)

    def test_a_yes_removes_it(self):
        entry = self.add()["saved"]
        self.call("delete_birthday", id=entry["id"])
        outcome = self.desk.answer(self.desk.waiting().token, True)
        self.assertTrue(outcome["allowed"])
        self.assertEqual([], self.store.list())

    def test_a_no_leaves_it_alone(self):
        entry = self.add()["saved"]
        self.call("delete_birthday", id=entry["id"])
        self.desk.answer(self.desk.waiting().token, False)
        self.assertEqual(1, len(self.store.list()))

    def test_an_id_that_does_not_exist_asks_nothing(self):
        answer = self.call("delete_birthday", id="nonesuch")
        self.assertFalse(answer["ok"])
        self.assertIsNone(self.desk.waiting())

    def test_the_model_is_told_not_to_claim_it_happened(self):
        entry = self.add()["saved"]
        answer = self.call("delete_birthday", id=entry["id"])
        self.assertIn("must not say it has", answer["instruction"])

    def test_a_second_delete_while_one_is_waiting_is_refused(self):
        first = self.add()["saved"]
        second = self.add(name="Dad")["saved"]
        self.call("delete_birthday", id=first["id"])
        answer = self.call("delete_birthday", id=second["id"])
        self.assertFalse(answer["ok"])
        self.assertIn("already a question waiting", answer["error"])
        self.assertIn("Mum", self.desk.waiting().question)

    def test_with_no_desk_at_all_nothing_is_removed(self):
        """A build that cannot ask must not fall back to doing it anyway."""
        box = ToolBox(birthdays=self.store)
        entry = self.add()["saved"]
        answer = parse(box.call("delete_birthday",
                                json.dumps({"id": entry["id"]})))
        self.assertFalse(answer["ok"])
        self.assertEqual(1, len(self.store.list()))


if __name__ == "__main__":
    unittest.main()
