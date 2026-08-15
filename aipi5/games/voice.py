"""Starting a motion game out loud, because the touchscreen cannot be reached.

**This is not a convenience.** The player has to stand far enough back that the
camera can see their whole upper body — a metre and a half of a room, well out
of arm's reach of a 1280x800 panel bolted to a wall. So the START button on the
start screen is reachable only by somebody who then has to walk backwards into
shot before the first fruit arrives, which is the wrong way round: the button
that begins a game about standing back can only be pressed by standing close.

Saying "start game" is how a player who is already in position starts playing.

Two things make this different from the button, and both come from nobody being
near the screen:

* **The game is opened if it is not already.** The button path cannot need this
  because pressing START implies the start screen is showing. A voice can
  arrive with the assistant on any page at all.
* **The player-detected check waits instead of refusing.** `GameManager.
  voice_start` blocks for up to four seconds — somebody who has just finished
  saying "start game" is by definition in front of the camera, but the pose
  service may have been running for a fraction of a second, and answering "I
  can't see you" to somebody standing in plain sight is the one failure this
  command must not have.

**Which phrases are safe here is a measurement, not a preference**, because the
router compares by sound. Three obvious candidates were dropped after measuring
them against the commands AIA already has — see `commands` below. One of them
was actively dangerous.
"""

from __future__ import annotations

import logging

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.plugins.base import CommandSpec, Plugin, Result

log = logging.getLogger(__name__)


class GameVoice(Plugin):
    """The spoken half of the Games page.

    Holds no hardware and no state of its own: everything it does goes through
    `GameManager`, which is the one object allowed to own a camera and an
    accelerator on a game's behalf. This class is a phrasebook.
    """

    name = "game_voice"
    description = "The motion games"

    def __init__(self, manager):
        """`manager` is a `GameManager`, or a callable returning one.

        The callable form is what `main.py` uses, and it is late binding for a
        real reason rather than taste: the command registry is built before the
        camera is opened, and `GameManager` cannot exist until the camera, the
        ducker and the screensaver manager all do. The alternative was moving
        four blocks of a working startup sequence around to satisfy an import
        order, which is a much larger change to make for a phrasebook.
        """
        self._manager = manager

    @property
    def manager(self):
        return self._manager() if callable(self._manager) else self._manager

    def available(self) -> bool:
        """True whenever there is a manager to ask.

        Not "true when a game is running" — the command exists precisely for
        when one is *not*, and a plugin that reported itself unavailable then
        would have its own start command refused by the check meant to protect
        the others. Exactly the trap `KodamaLauncher.available` documents.
        """
        return self.manager is not None

    def commands(self) -> list[CommandSpec]:
        return [
            CommandSpec(
                name="start_game",
                description="Start the motion game",
                handler=self.start,
                # Spoken, and for the reason `open_kodama` is: opening a game
                # takes a second and a half of camera and model loading during
                # which nothing on screen changes, so silence is
                # indistinguishable from having been ignored. More importantly
                # every *failure* here — nobody visible, camera busy, no HAT —
                # is addressed to somebody standing too far back to read a
                # panel, and a diagnostic they cannot see is no diagnostic.
                speaks=True,
                speech={
                    "en": "Start the fruit game",
                    "zh": "开始体感游戏",
                },
                # Measured against every phrase AIA already routes, using the
                # router's own `similarity`, before being written down. The
                # floor a whole-utterance match needs is 0.78; anything within
                # reach of it is a phrase this command may not have.
                #
                # **Three candidates were dropped, and one was dangerous:**
                #
                #   "start the game"  0.71 vs reboot["restart the pi"]   DROPPED
                #   "play game"       0.74 vs toggle["play pause"]       DROPPED
                #   开始 (alone)       0.67 vs resume["开始播放"]          DROPPED
                #
                # The first is the one worth remembering. "Start the game" is
                # the most natural English phrasing there is, and it sounds
                # enough like "restart the pi" to be a coin toss — a command
                # that reboots the device. `confirm=True` on reboot would have
                # caught it, but a game that sometimes asks "shall I restart
                # the Raspberry Pi?" is not a game anybody trusts.
                #
                # What is left has margin in both directions. The worst of
                # these is "let's play" at 0.62 against resume["play"], and
                # tests/test_routing.py asserts every one of these numbers
                # rather than trusting this comment — an earlier comment in
                # this project quoted a score that was wrong by 0.19.
                phrases={
                    "en": ("start game", "begin game", "lets play",
                           "let's play", "start fruit ninja",
                           "start the fruit game"),
                    # 开始游戏 is 0.50 from its nearest neighbour and 玩游戏 is
                    # 0.00 from anything at all — there is simply no Kodama
                    # command about games, which is what leaves this much room.
                    "zh": ("开始游戏", "开始游戏吧", "玩游戏", "游戏开始",
                           "开始水果忍者"),
                },
            ),
            CommandSpec(
                name="play_again",
                description="Play the game again",
                handler=self.again,
                speaks=True,
                speech={
                    "en": "Play the fruit game again",
                    "zh": "再玩一次体感游戏",
                },
                # The Play Again button, which is on the game-over screen and
                # therefore just as unreachable as START was — the round has
                # ended, the player is still standing where they were playing,
                # and the only way to go again was to walk to the panel.
                #
                # Two more measured drops:
                #
                #   "replay"        0.80 vs resume["play"]            DROPPED
                #   "restart game"  0.91 vs start_game["start game"]  DROPPED
                #
                # `重来` is 0.50 from reboot[`重启`], which is close enough to
                # be worth naming and far enough to be safe: an exact 重来
                # scores 1.00 here and 0.50 there, and an exact 重启 does the
                # reverse. Asserted in tests/test_game_voice.py.
                phrases={
                    "en": ("play again", "again", "one more", "one more time",
                           "another go", "play once more"),
                    "zh": ("重来", "再来一次", "再玩一次", "再来", "再来一局",
                           "重新开始"),
                },
            ),
        ]

    # ── the handler ──────────────────────────────────────────────────

    def again(self) -> Result:
        """Play Again, from where the player is standing.

        The same lifecycle as `start`, with `fresh=True`: a round already in
        progress *is* restarted here, because "again" is a deliberate word in a
        way "start" is not. See `GameManager.voice_start`.
        """
        return self._begin(fresh=True)

    def start(self) -> Result:
        """Open a game if needed, wait for the player, start the round."""
        return self._begin(fresh=False)

    def _begin(self, fresh: bool) -> Result:
        """Every branch says something different out loud.

        The outcomes want different things from the person listening: stand in
        shot, wait, stop asking, look at the screen, or nothing at all.
        """
        if self.manager is None:
            return Result.failed("Games are turned off.", "游戏功能没有开启。")

        outcome, detail = self.manager.voice_start(fresh=fresh)

        if outcome == "started":
            # Short on purpose. This is a starting gun — it is spoken while
            # the first fruit is already on its way up, so a sentence would
            # still be playing when the player needs to swing at something.
            return Result.done(f"Here we go. {detail}.", f"开始！{detail}。")

        if outcome == "resumed":
            return Result.done("Carrying on.", "继续。")

        if outcome == "already":
            return Result.done("You are already playing.", "游戏已经在进行了。")

        if outcome == "no-player":
            # The one failure this command was built to avoid, so it says what
            # to do rather than what went wrong.
            return Result.failed(
                "I cannot see you. Stand in front of the camera so your "
                "shoulders and both hands are visible.",
                "我看不到你。请站到摄像头前面，让肩膀和双手都能被看到。")

        if outcome == "no-games":
            return Result.failed("There is no game to start.",
                                 "没有可以开始的游戏。")

        # unavailable — the camera is busy, or the AI HAT is missing. `detail`
        # is written for a screen but is the only true thing there is to say,
        # and section 42 is explicit that a missing accelerator must be
        # reported rather than worked around.
        log.warning("Game: voice start refused — %s", detail)
        return Result.failed(
            f"The game could not start. {detail}",
            f"游戏无法开始。{detail}")
