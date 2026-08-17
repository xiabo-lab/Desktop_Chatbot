"""Motion-controlled boxing built on AIPI5's shared pose stream.

The package contains no camera or accelerator code.  ``GameManager`` feeds it
the same ``PoseSnapshot`` used by Fruit Ninja, keeping camera ownership and
pose inference in :mod:`aipi5.motion.service`.
"""

from aipi5.games.boxing.game import BoxingSession

__all__ = ["BoxingSession"]
