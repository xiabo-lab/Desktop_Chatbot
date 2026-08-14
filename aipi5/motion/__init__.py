"""AI Motion: the camera-and-accelerator half of every body-motion game.

Nothing in this package knows what a fruit is. That is section 11's
requirement and section 51's reason: Fruit Ninja is the first consumer of a
pose stream, not its owner, and Yoga, Boxing and Workout arrive later wanting
the same seventeen joints with different opinions about what they mean.

    Brio -> Camera (aipi5/vision) -> PoseService -> PoseFrame -> a game

The split inside is by what changes together:

    pose_types    the vocabulary. No numpy, no hardware, importable anywhere.
    geometry      letterbox, mirror, screen mapping. Pure arithmetic.
    yolov8_pose   raw tensors to joints. numpy only, no accelerator.
    hailo_pose    the AI HAT+ 2. The only file that imports hailo_platform.
    pose_filter   jitter, confidence decay, which person is the player.
    service       the thread, the camera lease, and the newest-frame rule.

The first three are testable on the development machine with no hardware at
all, which is where the arithmetic that actually gets things wrong lives.
"""

from aipi5.motion.pose_types import (KEYPOINT_INDEX, KEYPOINT_NAMES, SKELETON,
                                     WRISTS, KeyPoint, PersonPose, PoseFrame,
                                     PoseStats)

__all__ = [
    "KEYPOINT_INDEX", "KEYPOINT_NAMES", "SKELETON", "WRISTS",
    "KeyPoint", "PersonPose", "PoseFrame", "PoseStats",
]
