"""The kiosk page's script must *run*, not merely parse.

`tests/test_ui_assets.py` checks the page's structure and `node --check` checks
its syntax. Neither catches the failure that matters most here, and one shipped:

    const handControl = { retry: HAND_RETRY_MS, ... };   // line ~5400
    ...
    const HAND_RETRY_MS = 2000;                          // line ~5750

That is valid syntax and a `ReferenceError` the moment it is evaluated -- a
`const` read before its declaration is in the temporal dead zone. It does not
break one feature: it aborts the whole script, so the page stops polling, stops
drawing and stops answering touch. On a device whose only input is a
touchscreen that is indistinguishable from a frozen machine, and it was
diagnosed as one.

So this evaluates the script under Node with the smallest set of browser stubs
that lets it reach the end. It proves the top level runs: every declaration
resolves, every constant is defined before it is read, and nothing throws on
the way. It does not test behaviour, and is not trying to.

Skipped where Node is not installed, which is the Pi -- the deploy runs the
suite there and Node is a developer tool. The check is worth having on the
machine the page is edited on, which is the one that would break it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "aipi5" / "ui" / "web" / "index.html"

#: Enough of a browser for the page's top level to run to the end. Deliberately
#: dumb: a Proxy that answers every property with a no-op covers the hundreds of
#: DOM calls this file makes without naming any of them, so the stub does not
#: need maintaining every time the page grows an element.
STUB = r"""
const __noop = () => {};
const __el = new Proxy({}, {
  get: (t, k) => {
    if (k === "style" || k === "classList" || k === "dataset") {
      return new Proxy({}, { get: () => __noop, set: () => true });
    }
    if (k === "length") return 0;
    if (typeof k === "symbol") return undefined;
    return __noop;
  },
  set: () => true,
});
function __define(name, value) {
  // Some globals are getter-only on `globalThis` in Node; defineProperty gets
  // past that, and a failure means the real one is good enough.
  try {
    Object.defineProperty(globalThis, name,
                          { value, writable: true, configurable: true });
  } catch (e) { /* keep Node's own */ }
}
__define("document", {
  getElementById: () => __el, querySelector: () => __el,
  querySelectorAll: () => [], createElement: () => __el,
  addEventListener: __noop, body: __el, documentElement: __el,
  hidden: false, visibilityState: "visible",
});
__define("window", globalThis);
__define("navigator", { userAgent: "node", mediaDevices: {} });
__define("location", { href: "http://x/", search: "", protocol: "http:", host: "x" });
__define("fetch", () => Promise.resolve({ ok: true, json: () => ({}), text: () => "" }));
__define("setInterval", () => 0);
__define("setTimeout", () => 0);
__define("clearInterval", __noop);
__define("clearTimeout", __noop);
__define("requestAnimationFrame", () => 0);
__define("Image", function () { return __el; });
__define("Audio", function () { return __el; });
__define("AudioContext", function () { return __el; });
__define("localStorage", { getItem: () => null, setItem: __noop, removeItem: __noop });
__define("EventSource", function () { return __el; });
__define("WebSocket", function () { return __el; });
__define("RTCPeerConnection", function () { return __el; });
__define("addEventListener", __noop);
__define("matchMedia", () => ({ matches: false, addEventListener: __noop }));
__define("screen", { width: 1280, height: 800 });
__define("innerWidth", 1280);
__define("innerHeight", 800);
__define("devicePixelRatio", 1);
__define("speechSynthesis", { speak: __noop, cancel: __noop, getVoices: () => [] });
__define("Notification", function () { return __el; });
__define("IntersectionObserver", function () { return { observe: __noop, disconnect: __noop }; });
__define("ResizeObserver", function () { return { observe: __noop, disconnect: __noop }; });
__define("MutationObserver", function () { return { observe: __noop, disconnect: __noop }; });
"""


def page_script() -> str:
    """The page's own module, without the import map, which is JSON."""
    text = PAGE.read_text(encoding="utf-8")
    blocks = re.findall(
        r"<script(?![^>]*type=\"importmap\")[^>]*>(.*?)</script>", text, re.S)
    # The first is the page's own; the rest are tiny or empty.
    return max(blocks, key=len)


def run_under_node(script: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "page.mjs"
        path.write_text(STUB + "\n" + script, encoding="utf-8")
        return subprocess.run(["node", str(path)], capture_output=True,
                              text=True, timeout=120)


@unittest.skipUnless(shutil.which("node"), "node is not installed here")
class TestThePageEvaluates(unittest.TestCase):

    def test_the_script_runs_to_the_end(self):
        """Every declaration resolves and nothing throws at the top level."""
        done = run_under_node(page_script())
        self.assertEqual(
            done.returncode, 0,
            "the kiosk script threw while loading, which stops the whole "
            "page -- including its touch handling:\n" + done.stderr[:2000])

    def test_it_would_have_caught_the_bug_that_shipped(self):
        """Red with the fault put back, or this test proves nothing.

        Moves a `const` below its first use, which is exactly the shape of the
        break: valid syntax, fatal on evaluation.
        """
        script = page_script()
        # Any top-level `const` that is *read while the module evaluates* will
        # do. The original was in the hand-control code, which has since moved
        # to Python; the fault this recreates is identical, and the assertion
        # below fails loudly if the replacement stops being suitable rather
        # than passing for the wrong reason.
        found = re.search(r"^const FULLSCREEN_SLACK = [^\n]*\n", script, re.M)
        self.assertIsNotNone(
            found, "the constant this test re-breaks has been renamed")
        moved = script.replace(found.group(0), "", 1) + "\n" + found.group(0)
        done = run_under_node(moved)
        self.assertNotEqual(done.returncode, 0,
                            "a constant used before it is declared went "
                            "unnoticed, so this check is not checking")
        self.assertIn("ReferenceError", done.stderr)


if __name__ == "__main__":
    unittest.main()
