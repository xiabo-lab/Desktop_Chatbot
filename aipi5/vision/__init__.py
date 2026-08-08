"""The camera, and the two entirely separate things it is used for.

Worth stating at the top of the package, because conflating them is the mistake
the specification spends two sections warning against.

**Seeing**, in `camera.py` and `describe.py`, is on demand. Somebody asks what
is in front of them, one frame is captured, and that one frame is sent to
OpenAI. It happens when a person asks and at no other time.

**Presence**, in `person_detection.py`, is continuous and entirely local. It
runs twice a second for the life of the process on the AI HAT+ 2, and nothing
it sees ever leaves the device. It exists to decide whether the room is empty,
which is a question that must never cost a network request — twice a second,
forever, would be about 172,000 uploads a day of a room somebody lives in.

They share a camera and nothing else.
"""
