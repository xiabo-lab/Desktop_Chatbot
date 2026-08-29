"""Driving the agent's browser with a hand.

Two halves, and they fail in different directions.

The **helper** half is a privilege question. `hand_click` is the first operation
in this project that takes a *place on the screen* from something outside the
helper, and the thing outside is a web page. So the tests below are mostly
refusals: a coordinate off the page, a coordinate that is not a number, a
gesture naming an operation that is not a gesture.

The **runtime** half is an ergonomics question, and the bug it guards is the one
the person reported: a browser closed while they were using it. A hand has to
count as use, and a sweep read twice from one flick of the wrist has to not.

Nothing here opens a browser or a socket. `browser.py` is imported the way the
helper imports it -- flat, with its own directory on the path -- because that is
how it runs on the device.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "aipi5" / "agent" / "helper"
if str(HELPER) not in sys.path:
    sys.path.insert(0, str(HELPER))

import browser as browser_module      # noqa: E402
import ops                            # noqa: E402

from aipi5.agent.runtime import AgentService   # noqa: E402


class Recorder:
    """A browser that is up and writes down what CDP it was asked for."""

    def __init__(self, viewport=(1000, 500)):
        self.alive = True
        self.calls = []
        self.size = viewport                   # what CDP would report
        self.viewport = (time.monotonic(), viewport)   # the module's cache
        self.scrolled_to = None
        self.offset = 0.0
        self.overlay = False
        self.overlay_error = ""
        self.handed_over = False
        self.last_url = ""
        self.last_used = time.monotonic()

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, params or {}))
        if method == "Page.getLayoutMetrics":
            width, height = self.size
            return {"cssLayoutViewport": {"clientWidth": width,
                                          "clientHeight": height},
                    "visualViewport": {"pageY": self.offset}}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 1,
                    "entries": [{"id": 1, "url": "https://a", "title": "A"},
                                {"id": 2, "url": "https://b", "title": "B"}]}
        return {}

    def touch(self):
        self.last_used = time.monotonic()

    def idle_for(self):
        return time.monotonic() - self.last_used


class HelperFixture(unittest.TestCase):
    """Puts a `Recorder` where `_get` would return a real browser."""

    def setUp(self):
        self.browser = Recorder()
        self._saved = browser_module._browser
        browser_module._browser = self.browser
        self.addCleanup(setattr, browser_module, "_browser", self._saved)
        # `_settle` sleeps for seconds it does not need to here.
        self._sleep = browser_module.time.sleep
        browser_module.time.sleep = lambda _seconds: None
        self.addCleanup(setattr, browser_module.time, "sleep", self._sleep)

    def mouse_events(self):
        return [params for method, params in self.browser.calls
                if method == "Input.dispatchMouseEvent"]

    def overlay_calls(self):
        return [params for method, params in self.browser.calls
                if method == "Overlay.highlightRect"]


class TestAPointOnThePage(HelperFixture):
    """The first operation that takes a screen position from a web page."""

    def test_a_fraction_becomes_a_pixel(self):
        """The page sends 0..1 because it cannot know the window's size.

        It is underneath that window. A page that guessed a height would put
        every click out by whatever Chromium's toolbar happens to be.
        """
        self.assertEqual(
            browser_module._point({"x": 0.5, "y": 0.25}, self.browser),
            (500.0, 125.0))

    def test_off_the_page_is_refused_not_clamped(self):
        """A hand at the edge of the camera is half out of frame.

        Clamping would turn that into a real click on the edge of the page --
        an action nobody aimed, produced by somebody reaching for a cup.
        """
        for spot in ({"x": 1.2, "y": 0.5}, {"x": -0.01, "y": 0.5},
                     {"x": 0.5, "y": 42.0}):
            with self.subTest(spot=spot):
                with self.assertRaises(ops.Refused):
                    browser_module._point(spot, self.browser)

    def test_a_point_that_is_not_a_number_is_refused(self):
        for spot in ({}, {"x": "0.5", "y": None}, {"x": None, "y": None},
                     {"x": [0.5], "y": 0.5}):
            with self.subTest(spot=spot):
                with self.assertRaises(ops.Refused):
                    browser_module._point(spot, self.browser)

    def test_the_viewport_is_measured_not_assumed(self):
        """And re-measured, eventually.

        Cached because a pointer following a hand asks eight times a second;
        expiring because a window that changed shape must be noticed.
        """
        self.browser.viewport = None
        first = browser_module._viewport(self.browser)
        self.assertEqual(first, (1000, 500))
        asked = sum(1 for method, _ in self.browser.calls
                    if method == "Page.getLayoutMetrics")
        browser_module._viewport(self.browser)
        self.assertEqual(
            sum(1 for method, _ in self.browser.calls
                if method == "Page.getLayoutMetrics"), asked,
            "the cached viewport was measured again")
        self.assertLess(browser_module.VIEWPORT_CACHE_S, 60,
                        "a stale viewport must expire within a minute")


class TestWhatAHandCanDo(HelperFixture):
    """The five effects, and the absence of a sixth."""

    def test_a_fist_clicks_where_it_was_told(self):
        browser_module.hand_click({"x": 0.25, "y": 0.5})
        events = self.mouse_events()
        self.assertEqual([e["type"] for e in events],
                         ["mousePressed", "mouseReleased"])
        self.assertEqual((events[0]["x"], events[0]["y"]), (250.0, 250.0))

    def test_a_palm_moves_the_pointer_without_pressing_anything(self):
        """The whole reason hand control is usable, and the whole reason it is
        not a click: hover feedback, no button."""
        browser_module.hand_move({"x": 0.5, "y": 0.5})
        events = self.mouse_events()
        self.assertEqual([e["type"] for e in events], ["mouseMoved"])
        self.assertNotIn("button", events[0])

    def test_the_overlay_needs_the_dom_agent_first(self):
        """Without `DOM.enable`, `Overlay.highlightRect` is *accepted* and
        draws nothing.

        There is no error to catch: it was found by screenshotting the
        compositor and counting zero changed pixels between two cursor
        positions. Ordering asserted here so it cannot be tidied away.
        """
        browser_module.hand_move({"x": 0.5, "y": 0.5})
        order = [method for method, _ in self.browser.calls
                 if method in ("DOM.enable", "Overlay.enable",
                               "Overlay.highlightRect")]
        self.assertEqual(order[:3],
                         ["DOM.enable", "Overlay.enable",
                          "Overlay.highlightRect"])

    def test_the_pointer_is_drawn_where_the_hand_is(self):
        """And drawn by the browser, not by script in the page.

        A dot on the kiosk would be a dot underneath the window somebody is
        looking at -- which is what the first version did, and why nobody could
        aim a click.
        """
        browser_module.hand_move({"x": 0.25, "y": 0.5})
        drawn = self.overlay_calls()
        self.assertEqual(len(drawn), 1)
        middle_x = drawn[0]["x"] + drawn[0]["width"] / 2
        middle_y = drawn[0]["y"] + drawn[0]["height"] / 2
        self.assertAlmostEqual(middle_x, 250.0, delta=1)
        self.assertAlmostEqual(middle_y, 250.0, delta=1)

    def test_the_pointer_never_runs_code_in_the_page(self):
        """`Runtime.evaluate` is the method this whole design keeps out of
        reach. Injecting a dot into the document would need it, and a nice
        feature is exactly how such a thing arrives."""
        browser_module.hand_move({"x": 0.5, "y": 0.5})
        browser_module.hand_click({"x": 0.5, "y": 0.5})
        browser_module.hand_scroll({"direction": "down"})
        used = {method for method, _ in self.browser.calls}
        for forbidden in ("Runtime.evaluate", "Runtime.callFunctionOn",
                          "Page.addScriptToEvaluateOnNewDocument"):
            self.assertNotIn(forbidden, used)

    def test_a_click_is_marked_before_the_page_is_told(self):
        """So the dot is on screen while the page is busy, and a click that
        does nothing visible is still visibly a click."""
        browser_module.hand_click({"x": 0.5, "y": 0.5})
        order = [method for method, _ in self.browser.calls
                 if method in ("Overlay.highlightRect",
                               "Input.dispatchMouseEvent")]
        self.assertEqual(order[0], "Overlay.highlightRect")
        drawn = self.overlay_calls()[0]
        self.assertGreater(drawn["width"], browser_module.CURSOR_SIZE,
                           "a click should be a bigger mark than a move")

    def test_an_overlay_that_refuses_is_asked_once(self):
        """A browser that will not draw costs a pointer, not a hand.

        And must not be asked eight times a second forever, which is the shape
        of the operation that has to keep up with an arm.
        """
        def refuse(method, params=None, timeout=None):
            if method.startswith("Overlay."):
                self.browser.calls.append((method, params or {}))
                raise ops.Refused("no overlay here")
            return Recorder.call(self.browser, method, params, timeout)

        self.browser.call = refuse
        for _ in range(5):
            browser_module.hand_move({"x": 0.5, "y": 0.5})
        # One *attempt*, which is two calls: the DOM agent and then the
        # overlay. Counting both would make a single try look like two.
        asked = sum(1 for method, _ in self.browser.calls
                    if method == "Overlay.enable")
        self.assertEqual(asked, 1, "it kept asking a browser that said no")
        self.assertTrue(self.browser.overlay_error,
                        "a missing pointer left no reason behind")
        self.assertEqual(len([m for m, _ in self.browser.calls
                              if m == "Input.dispatchMouseEvent"]), 5,
                         "losing the pointer stopped the pointer moving")

    def test_a_sweep_scrolls_the_way_the_hand_went(self):
        browser_module.hand_scroll({"direction": "up"})
        up = self.mouse_events()[-1]
        self.assertEqual(up["type"], "mouseWheel")
        self.assertLess(up["deltaY"], 0, "sweeping up must scroll up")

        browser_module.hand_scroll({"direction": "down"})
        self.assertGreater(self.mouse_events()[-1]["deltaY"], 0)

    def test_a_scroll_does_not_wait_for_the_page_to_stop_gliding(self):
        """It reports whether the *previous* scroll moved, not this one.

        Reading the offset straight after the wheel event catches a smooth
        scroll mid-glide and returns zero every time -- measured on the device,
        where `moved` read 0 on every call while the page was plainly moving.
        Waiting it out cost 350 ms on the operation a person repeats most, so
        the question is answered one gesture later instead.
        """
        first = browser_module.hand_scroll({"direction": "down"})
        self.assertNotIn("moved_last_time", first,
                         "it claimed to know about a scroll that never happened")

        # The page moved between the two calls, as `Recorder` now reports.
        self.browser.scrolled_to = 0.0
        self.browser.offset = 520.0
        second = browser_module.hand_scroll({"direction": "down"})
        self.assertEqual(second["moved_last_time"], 520)
        self.assertEqual(second["at"], 520)

    def test_a_scroll_never_sleeps(self):
        """The whole point of the change. Guarded because the obvious fix for
        a mid-glide reading is a sleep, and it would be added back."""
        slept = []
        original = browser_module.time.sleep
        browser_module.time.sleep = lambda s: slept.append(s)
        try:
            browser_module.hand_scroll({"direction": "down"})
            browser_module.hand_move({"x": 0.5, "y": 0.5})
        finally:
            browser_module.time.sleep = original
        self.assertEqual(slept, [], "a hand was made to wait")

    def test_a_scroll_must_name_a_direction_and_a_sane_number_of_steps(self):
        for args in ({}, {"direction": "sideways"}, {"direction": "up",
                                                     "steps": 99},
                     {"direction": "up", "steps": True},
                     {"direction": "up", "steps": 0}):
            with self.subTest(args=args):
                with self.assertRaises(ops.Refused):
                    browser_module.hand_scroll(args)

    def test_running_out_of_history_is_an_answer_not_an_error(self):
        """Sweeping forward at the end of the history is a thing people do.

        Every other refusal in this module is a boundary; this one would only
        be a hand that arrived somewhere ordinary, and saying so is friendlier
        than failing.
        """
        answer = browser_module.hand_history({"way": "forward"})
        self.assertFalse(answer["moved"])
        self.assertIn("nothing", answer["detail"])

    def test_going_back_uses_the_history_rather_than_a_url(self):
        answer = browser_module.hand_history({"way": "back"})
        self.assertTrue(answer["moved"])
        self.assertIn(("Page.navigateToHistoryEntry", {"entryId": 1}),
                      self.browser.calls)

    def test_a_hand_cannot_name_a_direction_that_is_not_one(self):
        for args in ({}, {"way": "sideways"}, {"way": None}):
            with self.subTest(args=args):
                with self.assertRaises(ops.Refused):
                    browser_module.hand_history(args)

    def test_a_hand_cannot_type(self):
        """No gesture reaches text entry, and none should.

        Reading a page, scrolling it and following a link are all things the
        touchscreen already does. Typing is where a misread gesture stops being
        a wasted movement and starts being content nobody wrote.
        """
        gestures = set(AgentService.GESTURES)
        for name, (operation, _args) in AgentService.GESTURES.items():
            with self.subTest(name=name):
                self.assertNotIn("type", operation)
        self.assertFalse(gestures & {"type", "browser_type", "text"})

    def test_the_toolbar_is_out_of_reach_by_construction(self):
        """Not by a bounds check -- by the coordinate system.

        `Input.dispatchMouseEvent` takes viewport coordinates, and the address
        bar, the tab strip and the close button are not in that space. This
        asserts the property that makes that true: the largest point the page
        can name still lands inside the content area, so no arithmetic here
        ever produces a negative y that would climb into the furniture.
        """
        width, height = browser_module._point({"x": 1.0, "y": 1.0},
                                              self.browser)
        self.assertEqual((width, height), self.browser.size)
        smallest = browser_module._point({"x": 0.0, "y": 0.0}, self.browser)
        self.assertEqual(smallest, (0.0, 0.0))


class TestNoBrowserNoGesture(unittest.TestCase):
    def setUp(self):
        self._saved = browser_module._browser
        browser_module._browser = None
        self.addCleanup(setattr, browser_module, "_browser", self._saved)

    def test_every_hand_operation_refuses_a_closed_browser(self):
        """And none of them opens one.

        A hand waved at a device with nothing on screen must not summon a
        browser -- that is the agent's decision and it belongs to the agent.
        """
        for name in ("browser_hand_scroll", "browser_hand_move",
                     "browser_hand_click", "browser_hand_history"):
            with self.subTest(name=name):
                with self.assertRaises(ops.Refused):
                    browser_module.OPS[name]({"x": 0.5, "y": 0.5,
                                              "direction": "up", "way": "back"})
        # `_get(start=False)` does construct the bookkeeping object -- what
        # must not happen is a Chromium, and `alive` is the thing that says
        # whether one was launched.
        self.assertFalse(browser_module._browser is not None
                         and browser_module._browser.alive,
                         "a gesture started a browser")

    def test_asking_the_state_does_not_start_one_either(self):
        answer = browser_module.state({})
        self.assertFalse(answer["open"])
        self.assertIsNone(browser_module._browser)


class TestTheSweepThatClosedItTooSoon(unittest.TestCase):
    """The reported bug: "the browser is closed now without user input".

    The helper's own log, from the evening it happened:

        closed the agent browser after 600s idle

    `idle_for()` was reset only by `touch()`, and `touch()` was called from
    helper operations -- so it measured the *agent* not acting. Somebody
    standing at the device reading a page was invisible to it.
    """

    def test_a_handed_over_page_outlasts_a_forgotten_one(self):
        self.assertGreater(browser_module.HANDOVER_TIMEOUT_S,
                           browser_module.IDLE_TIMEOUT_S)

    def test_ten_minutes_is_no_longer_the_answer(self):
        """The specific number that was measured to be wrong."""
        self.assertGreater(browser_module.IDLE_TIMEOUT_S, 600.0)

    def test_navigating_the_page_counts_as_using_it(self):
        """The only human-activity signal available.

        `Runtime.evaluate` is refused for the agent for good reasons, so the
        page cannot be asked when it was last scrolled. Its address changing is
        weaker but it is real, and it costs one CDP call per sweep.
        """
        browser = Recorder()
        browser.last_url = "https://a"
        browser.last_used = time.monotonic() - 10_000
        saved = browser_module._browser
        browser_module._browser = browser
        self.addCleanup(setattr, browser_module, "_browser", saved)

        # `Recorder` reports https://b as current, which is a change.
        self.assertFalse(browser_module.sweep(),
                         "a page somebody navigated was swept away")
        self.assertEqual(browser.last_url, "https://b")
        self.assertLess(browser.idle_for(), 5.0,
                        "navigating did not count as use")

    def test_a_browser_in_use_is_not_interrogated_every_second(self):
        """The sweep runs off the helper's one-second accept timeout.

        Checking the address on every one of those would be a CDP round trip a
        second for as long as a window is open -- holding the same lock a hand
        takes eight times a second to move a pointer. There is nothing to
        decide until the timeout has expired, so nothing is asked until then.
        """
        browser = Recorder()
        browser.last_used = time.monotonic()          # busy a moment ago
        saved = browser_module._browser
        browser_module._browser = browser
        self.addCleanup(setattr, browser_module, "_browser", saved)

        for _tick in range(5):
            self.assertFalse(browser_module.sweep())
        self.assertEqual(browser.calls, [],
                         "the sweep spoke to a browser it had no verdict on")

    def test_a_page_nobody_touches_is_still_closed(self):
        """The sweep must still do its job. A window left over the kiosk is a
        device that looks broken to somebody walking past."""
        browser = Recorder()
        browser.last_url = "https://b"          # unchanged since last look
        browser.last_used = time.monotonic() - 10_000
        browser.stop = lambda: setattr(browser, "alive", False)
        saved = browser_module._browser
        browser_module._browser = browser
        self.addCleanup(setattr, browser_module, "_browser", saved)

        self.assertTrue(browser_module.sweep())
        self.assertFalse(browser.alive)


class TestWhatAGestureMayAskFor(unittest.TestCase):
    """The runtime's table, which is the list a web page is bounded by."""

    def test_every_gesture_maps_to_an_operation_that_exists(self):
        for name, (operation, _args) in AgentService.GESTURES.items():
            with self.subTest(name=name):
                self.assertIn(operation, ops.BROWSER)

    def test_no_gesture_reaches_anything_but_the_browser(self):
        """The page names a gesture, never an operation.

        So a compromised kiosk page cannot ask for `apply_config`, or a
        restart, or a file -- not because those are blocked, but because there
        is no gesture whose value is one of them.
        """
        for name, (operation, _args) in AgentService.GESTURES.items():
            with self.subTest(name=name):
                self.assertTrue(operation.startswith("browser_hand_"))
        for forbidden in ("apply_config", "propose_config", "restart_service",
                          "patch_file", "read_file", "install_package",
                          "browser_open", "browser_close"):
            self.assertNotIn(
                forbidden,
                {operation for operation, _ in AgentService.GESTURES.values()})

    def test_the_placed_gestures_are_exactly_the_ones_needing_a_place(self):
        placed = set(AgentService.PLACED)
        self.assertEqual(placed, {"click", "move"})
        self.assertTrue(placed <= set(AgentService.GESTURES))

    def test_a_pointer_may_move_faster_than_a_gesture_may_fire(self):
        """One flick of the wrist is one gesture.

        A sweep crosses the threshold on several consecutive frames, so
        without a floor a single movement scrolls three times. The pointer has
        the opposite requirement -- it has to keep up with the arm.
        """
        self.assertGreater(AgentService.GESTURE_INTERVAL_S,
                           AgentService.MOVE_INTERVAL_S)
        self.assertGreaterEqual(AgentService.GESTURE_INTERVAL_S, 0.3)
        self.assertLessEqual(AgentService.MOVE_INTERVAL_S, 0.2)

    def test_a_gesture_is_not_a_tool_the_model_can_call(self):
        """Gestures are the person's input, not the agent's capability.

        If these were tool schemas the model could decide to scroll a page on
        its own, which is both useless -- it reads the accessibility tree, not
        pixels -- and a way for a web page's text to move the screen.
        """
        from aipi5.agent.tools import AgentToolBox

        toolbox = AgentToolBox.__new__(AgentToolBox)
        source = Path(__file__).resolve().parent.parent / "aipi5" / "agent" / "tools.py"
        text = source.read_text(encoding="utf-8")
        for operation, _args in AgentService.GESTURES.values():
            with self.subTest(operation=operation):
                self.assertNotIn(f'"{operation}"', text,
                                 f"{operation} is reachable from the model")
        del toolbox


if __name__ == "__main__":
    unittest.main()
