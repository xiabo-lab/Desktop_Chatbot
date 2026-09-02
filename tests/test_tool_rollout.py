"""Turning the everyday tools on one at a time.

`assistant.tools` is a **rollout order**, not a security boundary, and the
difference matters enough to be worth stating in both places. What bounds a
tool is the validation in its handler and the object it was injected with; what
this decides is whether the toolbox is handed that object at all.

The reason it exists is deployment rather than safety. Eight tools arrived in
this work and six of them have a persistent or outward-facing effect — a file
on disk, a notification on a phone, an application starting in a room. Shipping
all of them at once means that when something is wrong there is no way to tell
which did it, on a device whose failures are reported as "it did something
strange yesterday".

Unknown names are **on**, which is the one decision here that could go either
way. Off would mean a tool that was written, tested and deployed and then
silently not offered because nobody added a line to a YAML file — which
presents to the person in the room as the assistant refusing to do something it
plainly can.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from aipi5.core.config import (AssistantConfig, ConfigError, TOOL_SWITCHES,
                               _tool_switches)

ROOT = Path(__file__).resolve().parent.parent


class TestTheSwitches(unittest.TestCase):

    def test_everything_is_on_by_default(self):
        config = AssistantConfig()
        for name in TOOL_SWITCHES:
            with self.subTest(tool=name):
                self.assertTrue(config.tool(name))

    def test_a_name_that_is_not_a_switch_is_on(self):
        """A tool written, tested and deployed must not be silently missing
        because nobody edited a file."""
        self.assertTrue(AssistantConfig(tools=frozenset()).tool("weather"))

    def test_naming_a_subset_turns_the_rest_off(self):
        config = AssistantConfig(tools=frozenset({"volume", "calendar"}))
        self.assertTrue(config.tool("volume"))
        self.assertFalse(config.tool("call"))
        self.assertFalse(config.tool("delegate"))

    def test_an_absent_key_means_all_of_them(self):
        """What a finished rollout looks like, and what every test wants."""
        self.assertEqual(frozenset(TOOL_SWITCHES), _tool_switches(None))

    def test_a_list_names_exactly_what_to_offer(self):
        self.assertEqual(frozenset({"volume", "photos"}),
                         _tool_switches(["volume", " Photos "]))

    def test_a_name_that_is_not_a_switch_is_complained_about_at_startup(self):
        """Rather than being a tool that silently never appears. This is a file
        somebody edits over ssh at eleven at night."""
        with self.assertRaises(ConfigError) as raised:
            _tool_switches(["volume", "vlume"])
        self.assertIn("vlume", str(raised.exception))
        self.assertIn("volume", str(raised.exception))

    def test_something_that_is_not_a_list_is_refused(self):
        with self.assertRaises(ConfigError):
            _tool_switches("volume")


class TestTheWiring(unittest.TestCase):
    """Asserted over `main.py`. Each switch has to reach the toolbox as an
    object or as None — a switch read and then not used is a switch that
    reports the rollout is working while every tool is live."""

    @classmethod
    def setUpClass(cls):
        cls.main = (ROOT / "aipi5" / "main.py").read_text(encoding="utf-8")

    def test_every_switch_gates_something(self):
        for name in TOOL_SWITCHES:
            with self.subTest(tool=name):
                self.assertIn(f'tool("{name}")', self.main)

    def test_a_tool_that_is_off_is_not_merely_hidden(self):
        """`ToolBox` leaves a tool out of `schemas()` when the object behind it
        is None *and* its handler has nothing to call. Passing None is what
        makes "off" mean the model cannot invoke it by name either — a tool
        removed from the list but still dispatchable is a tool a model will
        eventually guess at."""
        from aipi5.llm.tools import ToolBox

        box = ToolBox()
        offered = {t.get("name") for t in box.schemas()}
        for name in ("set_master_volume", "save_birthday", "create_reminder",
                     "take_photo", "get_asset_price", "call_phone",
                     "open_known_app", "delegate_agent_task"):
            with self.subTest(tool=name):
                self.assertNotIn(name, offered)
                # Still in the dispatch table, and still refuses — with a
                # sentence about what the device cannot do rather than an
                # `AttributeError` on a service that is None. A model asked for
                # a tool it was offered yesterday and is switched off today is
                # the ordinary case, not the invented-name one.
                self.assertIn(name, box._handlers)
                answer = json.loads(box.call(name, "{}"))
                self.assertFalse(answer["ok"])
                self.assertNotIn("unexpectedly", answer["error"])

    def test_the_yaml_documents_the_order_and_lists_every_switch(self):
        yaml = (ROOT / "config" / "aipi5.yaml").read_text(encoding="utf-8")
        block = yaml[yaml.index("  # Which everyday tools the model is offered."):]
        block = block[:block.index("\n\n")] if "\n\n" in block else block
        for name in TOOL_SWITCHES:
            with self.subTest(tool=name):
                self.assertIn(name, block)
        self.assertIn("not a security boundary", block)


if __name__ == "__main__":
    unittest.main()
