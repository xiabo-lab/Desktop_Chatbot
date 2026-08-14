"""Games that are played by moving, and the one object that owns their hardware.

    aipi5/games/manager.py          starts, stops, and gives everything back
    aipi5/games/fruit_ninja/        the first game

The split from `aipi5/motion/` is the important one and it is section 11's:
`motion` produces poses and knows nothing about games; `games` consumes them
and knows nothing about accelerators. Yoga, Boxing and Workout are new
directories beside `fruit_ninja` and no change at all to `motion`.
"""
