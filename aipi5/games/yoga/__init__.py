"""Yoga Coach: follow an animated coach through a twenty-minute class.

Like `aipi5.games.boxing`, this package contains no camera and no accelerator
code.  ``GameManager`` feeds it the same ``PoseSnapshot`` every other motion
game is fed, and :mod:`aipi5.motion` is unchanged by its existence.

    rig.py      fourteen bone directions, forwards to a coach and to a target
    poses.py    thirty-two frontal-plane poses, as bone tables
    scoring.py  one frame of a body against one pose, and one sentence about it
    lesson.py   three authored twenty-minute classes
    game.py     the session: two clocks, a hold, and a score
"""

from aipi5.games.yoga.game import YogaSession

__all__ = ["YogaSession"]
