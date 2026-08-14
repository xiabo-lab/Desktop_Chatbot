"""Fruit Slice — the first AI Motion game, shown as "Fruit Ninja".

    collision.py   segment-versus-moving-circle. The heart of it.
    fruit.py       what gets thrown, how fast, how often.
    game.py        the session: score, lives, pause, game over.

The gameplay owes its shape to the MIT-licensed community project in
`hailo-ai/hailo-rpi5-examples` — the five fruit, ten points each, the launch
speeds as a starting point. What is not taken from it is the implementation:
that one ties physics to the frame rate and slices by proximity to a single
wrist sample, and sections 16 and 24 both ask for better. Nothing here is
derived from the commercial game of a similar name, and no artwork or audio is
shipped at all — fruit are a colour and an emoji, drawn by the page.
"""
