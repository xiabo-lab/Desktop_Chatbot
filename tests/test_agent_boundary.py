"""The line between the assistant's tools and the agent's, asserted.

`aipi5/llm/tools.py` opens with a guarantee: the model emits a name and a JSON
blob, a fixed table maps it to a Python function, and **there is no path from
model output to a shell, a filesystem path, a URL, or an argument interpolated
into a command line.** `tests/test_tool_safety.py` holds sixteen tests over it.

The agent is the thing that contradicts that sentence. It may exist — it runs as
a different user, in a different process, behind a helper that will only do what
`policy.py` lists — but it must never make that docstring false, because a
guarantee that is written down and no longer true is worse than one that was
never claimed.

So: the two dispatch tables are disjoint, the agent package does not reach into
the assistant, and the helper reaches nothing at all. These are cheap tests
guarding a property that is expensive to notice the loss of.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "aipi5" / "agent" / "helper"
if str(HELPER) not in sys.path:
    sys.path.insert(0, str(HELPER))

import ops        # noqa: E402
from aipi5.agent.tools import AgentToolBox  # noqa: E402
from aipi5.llm.tools import ToolBox  # noqa: E402

STDLIB = set(sys.stdlib_module_names)


def _imports(path: Path) -> set[str]:
    """Top-level module names imported by one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


class TestTheTablesAreDisjoint(unittest.TestCase):
    """Nothing the agent can do is reachable from a spoken sentence."""

    def setUp(self):
        # An empty ToolBox still declares its handler table; the schemas are
        # what get filtered by which subsystems are present.
        self.assistant = set(ToolBox()._handlers)
        self.agent = set(AgentToolBox()._handlers)
        self.operations = set(ops.OPS)

    def test_no_name_appears_in_both(self):
        self.assertEqual(set(), self.assistant & self.agent)

    def test_no_agent_operation_is_offered_to_the_voice_model(self):
        names = {schema["function"]["name"] for schema in ToolBox().schemas()}
        self.assertEqual(set(), names & self.agent)

    def test_every_operation_the_toolbox_names_exists_in_the_helper(self):
        """A tool that calls an operation the helper does not have is a promise
        it cannot keep, and the failure appears only when somebody asks.

        Not a name-for-name match: one tool can be two operations. `set_config`
        is `propose_config` then `apply_config`, because a change is shown
        before it is made.
        """
        import re
        source = (ROOT / "aipi5" / "agent" / "tools.py").read_text(encoding="utf-8")
        called = set(re.findall(r'(?:helper\.call|_pass)\(\s*"([a-z_]+)"', source))
        self.assertTrue(called, "the toolbox calls no helper operations at all")
        self.assertEqual(set(), called - self.operations,
                         f"the toolbox calls {sorted(called - self.operations)}, "
                         f"which the helper does not have")

    def test_every_agent_schema_refuses_extra_properties(self):
        for schema in AgentToolBox().schemas():
            with self.subTest(name=schema["function"]["name"]):
                self.assertFalse(
                    schema["function"]["parameters"]["additionalProperties"])

    #: `browser_open` is the one tool that takes an address the model composed,
    #: because "open YouTube" cannot be served from a list this repository wrote
    #: down in advance. It is the deliberate exception, and it is named here so
    #: that a second one has to be added on purpose rather than by drifting.
    FREE_TEXT_URL = {"browser_open"}

    #: The tools that take a path the model composed. Two, and both arrived
    #: with source editing, because "fix the weather page" cannot be served
    #: from a list of filenames written down in advance.
    #:
    #: Named here so a third has to be added deliberately rather than by
    #: drifting. What makes them safe is not the schema — it is
    #: `policy.resolve_source` and `policy.resolve_write`, in root's code,
    #: which resolve the path, check it against the writable trees, and check
    #: `NEVER` both before and after symlink resolution.
    FREE_TEXT_PATH = {"read_source", "patch_file"}

    def test_no_other_tool_accepts_a_composable_path(self):
        """Everywhere else a path is an enum.

        A validated string is a thing to get wrong once; an enum is a handful
        of literals this repository wrote down, so there is nothing for a model
        to compose. `set_config` does not appear because it takes a section and
        a key, not a path at all.
        """
        for schema in AgentToolBox().schemas():
            name = schema["function"]["name"]
            if name in self.FREE_TEXT_PATH:
                continue
            for field, spec in schema["function"]["parameters"]["properties"].items():
                if field in ("path", "file", "command", "unit", "service"):
                    with self.subTest(tool=name, field=field):
                        self.assertIn("enum", spec,
                                      f"{name}.{field} must be an enum")

    def test_the_paths_that_are_free_text_are_checked_by_root(self):
        """The schema says `maxLength`, which is a suggestion to a model. The
        refusal that matters is in code the agent cannot edit."""
        import policy as helper_policy
        for attempt in ("/etc/shadow", "/home/fuwenxu/.ssh/id_ed25519",
                        "/home/fuwenxu/AIPI5/scripts/deploy.sh",
                        "/home/fuwenxu/AIPI5/systemd/aipi5.service",
                        "/home/fuwenxu/AIPI5/aipi5/agent/tools.py",
                        "relative/path.py", "", None):
            with self.subTest(path=str(attempt)[:44]):
                self.assertIsNone(helper_policy.resolve_write(attempt))

    def test_only_the_browser_takes_an_address(self):
        for schema in AgentToolBox().schemas():
            name = schema["function"]["name"]
            for field, spec in schema["function"]["parameters"]["properties"].items():
                if field == "url" and name not in self.FREE_TEXT_URL:
                    with self.subTest(tool=name):
                        self.assertIn("enum", spec)

    def test_the_address_the_browser_takes_is_checked_by_root(self):
        """The validation that matters is in the helper, not in the schema.

        A `maxLength` in a JSON schema is a suggestion to a language model.
        What actually stops `file:///home/fuwenxu/.ssh/id_ed25519` is
        `_check_url` in root's code, which the agent cannot edit — so this
        calls it rather than reading it. An earlier version of this test
        grepped the source for "192.168" and passed on a comment.
        """
        import browser as browser_module
        for allowed in ("https://www.youtube.com",
                        "http://example.com/a?b=c",
                        "https://en.wikipedia.org/wiki/Raspberry_Pi"):
            with self.subTest(url=allowed):
                self.assertEqual(allowed, browser_module._check_url(allowed))

        for blocked in (
                # Other schemes are how a browser reads a disk or runs code.
                "file:///home/fuwenxu/.ssh/id_ed25519",
                "file:///etc/shadow",
                "javascript:alert(1)",
                "chrome://settings",
                "devtools://devtools/bundled/inspector.html",
                "data:text/html,<h1>x</h1>",
                # This device's own unauthenticated services.
                "http://127.0.0.1:8092/api/state",
                "http://localhost:8092/",
                "http://aipi5.local/",
                # And the rest of the house.
                "http://192.168.1.1/",
                "http://10.0.0.1/",
                "http://172.16.5.4/",
                "http://169.254.169.254/latest/meta-data/",
                # Not addresses at all.
                "", "   ", None, 7, "https://" + "a" * 3000):
            with self.subTest(url=str(blocked)[:48]):
                with self.assertRaises(Exception):
                    browser_module._check_url(blocked)

    def test_the_browser_cannot_be_asked_to_run_code(self):
        """The methods that turn a browser back into an interpreter.

        Absent from the operation table, not blocked in it — `Runtime.evaluate`
        is not a name this device will accept from anybody, because no
        operation passes a method through.
        """
        import browser as browser_module
        source = (HELPER / "browser.py").read_text(encoding="utf-8")
        for method in ("Runtime.evaluate", "Runtime.callFunctionOn",
                       "Browser.setDownloadBehavior", "Fetch.enable",
                       "Network.setCookies"):
            with self.subTest(method=method):
                self.assertNotIn(f'"{method}"', source)
        # And every operation names its CDP methods as literals, so there is no
        # parameter anywhere that becomes one.
        for name, handler in browser_module.OPS.items():
            with self.subTest(op=name):
                self.assertNotIn("method", handler.__code__.co_varnames)

    def test_an_invented_tool_name_is_refused_rather_than_raising(self):
        answer = AgentToolBox().call("run_command", '{"cmd": "rm -rf /"}')
        self.assertIn("no tool called", answer)

    def test_malformed_arguments_come_back_as_a_sentence(self):
        """A tool that throws leaves the model holding a call with no result."""
        for bad in ("{not json", "[]", "null", '"a string"'):
            with self.subTest(arguments=bad):
                answer = AgentToolBox().call("system_facts", bad)
                self.assertIn("error", answer)

    def test_the_assistant_cannot_reach_a_privileged_operation_by_name(self):
        """The voice path's dispatch refuses an agent op like any other typo."""
        box = ToolBox()
        for name in sorted(self.agent | self.operations):
            with self.subTest(name=name):
                answer = box.call(name, "{}")
                self.assertIn("error", answer,
                              f"{name} must not be callable from the assistant")


class TestTheAgentDoesNotReachIntoTheAssistant(unittest.TestCase):
    """Import direction, checked statically rather than by convention."""

    FORBIDDEN = {"aia", "sounddevice", "cv2", "numpy"}

    def test_nothing_in_the_agent_package_imports_the_voice_loop(self):
        for path in sorted((ROOT / "aipi5" / "agent").rglob("*.py")):
            with self.subTest(path=path.name):
                names = _imports(path)
                self.assertNotIn("aia", names)
                # `aipi5.main` builds the whole assistant on import; an agent
                # module that pulled it in would start a second one.
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("from aipi5.main", text)
                self.assertNotIn("import aipi5.main", text)


class TestTheHelperIsStandalone(unittest.TestCase):
    """It runs as root, on the system interpreter, out of /usr/local/lib."""

    def test_it_imports_nothing_outside_the_standard_library(self):
        """So `pip install` can never change what root runs.

        The only non-stdlib names allowed are the helper's own two modules,
        which sit beside it in the same installed directory.
        """
        siblings = {"policy", "ops", "main", "changes", "browser"}
        for path in sorted(HELPER.glob("*.py")):
            with self.subTest(path=path.name):
                outside = _imports(path) - STDLIB - siblings
                self.assertEqual(set(), outside,
                                 f"{path.name} imports {sorted(outside)}")

    def test_it_never_imports_from_aipi5(self):
        """Checked as imports, not as text.

        `ops._validate` legitimately contains the string "from aipi5.core
        import config" — it is a one-line script handed to the *assistant's*
        interpreter, run as the owner, to prove the edited file still loads.
        A substring check calls that an import; the AST knows better.
        """
        for path in sorted(HELPER.glob("*.py")):
            with self.subTest(path=path.name):
                self.assertNotIn("aipi5", _imports(path))

    def test_it_is_not_a_package(self):
        """No __init__.py, so it cannot be imported into the assistant.

        It is a directory of files that gets copied somewhere else and run by a
        different interpreter as a different user. Making it importable would
        invite exactly the shortcut this whole design exists to prevent.
        """
        self.assertFalse((HELPER / "__init__.py").exists())

    def test_the_installer_is_not_run_by_the_deploy_script(self):
        """A deploy must not be able to change what root executes."""
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        self.assertNotIn("install-agent.sh", deploy)


if __name__ == "__main__":
    unittest.main()
