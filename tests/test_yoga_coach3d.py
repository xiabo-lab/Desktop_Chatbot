"""Run the 3D coach's own test suite, which is JavaScript.

`coach3d.js` is the only part of the yoga game whose logic lives entirely in
the browser, and it is arithmetic rather than drawing -- so it is tested the
same way everything else here is, just in the language it is written in. This
wrapper exists so that `pytest` still means "run the tests" and nobody has to
remember a second command.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUITE = ROOT / "tests" / "coach3d.test.mjs"
sys.path.insert(0, str(ROOT / "design"))


@unittest.skipIf(shutil.which("node") is None,
                 "node is not installed on this machine")
def test_coach3d_suite_passes() -> None:
    result = subprocess.run(["node", "--test", str(SUITE)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_91_poses_pass_anatomical_qa() -> None:
    from yoga_v2 import check3d

    library = check3d.pose_library()
    assert len(library) == 91
    faults = []
    for name, pose in sorted(library.items()):
        faults.extend(check3d.report(name, pose, verbose=False))
    assert not faults, "\n".join(faults)


def test_thigh_collision_gate_rejects_the_flattened_rejected_shapes() -> None:
    """The six-pose correction must not regress to intersecting leg tubes."""
    import copy
    from yoga_v2 import check3d

    library = check3d.pose_library()
    for name in ("supine_twist_left", "supine_twist_right"):
        flattened = copy.deepcopy(library[name])
        flattened.pop("depth3d", None)
        faults = check3d.body_volume_faults3d(name, flattened)
        assert any("thigh cores overlap" in fault for fault in faults), name

    # The side-on hamstring legs begin from already separated hip sockets, so
    # centreline intersection alone is insufficient: the skirt/thigh meshes
    # need extra lateral room throughout the folded leg.
    for name in ("seated_hamstring_left", "seated_hamstring_right"):
        pose = library[name]
        flat = copy.deepcopy(pose)
        flat.pop("depth3d", None)
        distances = []
        for candidate in (flat, pose):
            p = check3d.joints3d(candidate)
            left = tuple((p["left_hip"][i] + p["left_knee"][i]) / 2
                         for i in range(3))
            right = tuple((p["right_hip"][i] + p["right_knee"][i]) / 2
                          for i in range(3))
            distances.append(check3d.METRE * sum(
                (left[i] - right[i]) ** 2 for i in range(3)) ** 0.5)
        assert distances[0] < 0.36 and distances[1] > 0.49, name


def test_standing_knee_contact_does_not_exempt_belly_or_arm_penetration() -> None:
    import copy
    from yoga_v2 import check3d

    library = check3d.pose_library()
    for name in ("standing_knee_left", "standing_knee_right"):
        flattened = copy.deepcopy(library[name])
        flattened["depth3d"] = {}
        faults = check3d.body_volume_faults3d(name, flattened)
        assert any("knee penetrates torso" in fault for fault in faults), name
        assert any("forearm enters raised leg" in fault for fault in faults), name


def test_owner_approved_batches_are_loaded_verbatim() -> None:
    """A rebuild must never replace the shapes approved in the Pi editor."""
    from yoga_v2.poses3d import OWNER_APPROVED, POSES3D

    assert OWNER_APPROVED["approved"] == [
        "bird_dog_left", "bird_dog_right", "boat", "bridge", "butterfly",
        "cactus_arms", "cat", "chair", "child_pose", "cobra",
        "cow", "crescent_moon_left", "crescent_moon_right", "downward_dog",
        "easy_seat", "easy_seat_breath", "figure_four_left",
        "figure_four_right", "forward_fold", "gentle_back_reach",
        "goddess", "half_lift", "half_moon_left", "half_moon_right",
        "half_split_left",
        "half_split_right", "hand_to_toe_left", "hand_to_toe_right",
        "happy_baby", "high_lunge_left",
        "high_lunge_right", "knees_to_chest", "lizard_left", "lizard_right",
        "low_lunge_left", "low_lunge_reach_left", "low_lunge_reach_right",
        "mountain", "neck_tilt_left", "neck_tilt_right",
        "pigeon_prep_left", "pigeon_prep_right", "plank", "pyramid_left",
        "pyramid_right", "reverse_warrior_left", "reverse_warrior_right",
        "savasana", "seated_forward_fold", "side_angle_left",
        "side_angle_right", "side_bend_left", "side_bend_right",
        "side_plank_left", "side_plank_right", "standing_twist_left",
        "standing_twist_right", "star", "table", "thread_needle_left",
        "thread_needle_right", "triangle_left", "triangle_right",
        "upward_salute", "warrior_one_left", "warrior_one_right",
        "warrior_three_left", "warrior_three_right", "warrior_two_left",
        "warrior_two_right", "weight_shift", "wide_leg_fold",
        "low_lunge_right", "mountain_breath", "seated_side_stretch_left",
        "seated_side_stretch_right", "seated_twist_left",
        "seated_twist_right", "shoulder_opener", "tree_heart_left",
        "tree_heart_right", "tree_overhead_left", "tree_overhead_right",
        "seated_hamstring_right", "standing_knee_left",
        "standing_knee_right", "supine_twist_left", "supine_twist_right",
        "seated_hamstring_left",
        "sphinx", "staff",
    ]
    assert len(OWNER_APPROVED["approved"]) == 91
    assert len(set(OWNER_APPROVED["approved"])) == 91
    for name, edit in OWNER_APPROVED["pose_edits"].items():
        assert POSES3D[name] == edit

    # Butterfly was approved without an edit and deliberately continues to
    # use its authored production definition. Cat gained an exact head edit
    # in the final 91-pose pass and is therefore now part of pose_edits.
    assert "butterfly" not in OWNER_APPROVED["pose_edits"]
    assert "cat" in OWNER_APPROVED["pose_edits"]


def test_every_course_edge_has_a_human_scale_transition_route() -> None:
    """No course may silently return to a direct standing-to-floor morph."""
    from yoga_v2 import catalog, courses, transitions

    edges = []
    for course in courses.COURSES.values():
        previous = "mountain"
        for step in course.steps:
            path = transitions.path_between(previous, step.pose_id)
            route = [previous, *path, step.pose_id]
            assert len(path) <= 7, (previous, step.pose_id, path)
            if previous != step.pose_id:
                assert len(route) == len(list(dict.fromkeys(route))), route
            assert all(pose_id in catalog.POSES for pose_id in route)
            # Adjacent action stations never jump from fully standing straight
            # to the floor.  A gap of two is Fold-to-Tabletop: hands are
            # already down before the knees lower.
            for source, target in zip(route, route[1:]):
                gap = abs(courses.HEIGHT[catalog.get(source).family]
                          - courses.HEIGHT[catalog.get(target).family])
                assert gap <= 2.0, route
            edges.append((previous, step.pose_id))
            previous = step.pose_id

    assert len(edges) == 605
    assert len(set(edges)) == 260


def test_side_changes_unload_through_a_neutral_pose() -> None:
    from yoga_v2 import transitions

    assert transitions.path_between("warrior_two_left", "warrior_two_right") == ["star"]
    assert transitions.path_between("high_lunge_left", "high_lunge_right") == ["mountain"]
    assert transitions.path_between("bird_dog_left", "bird_dog_right") == ["table"]
    assert transitions.path_between("supine_twist_left", "supine_twist_right") == ["savasana"]


def test_high_risk_height_and_floor_changes_use_clear_neutral_stations() -> None:
    from yoga_v2 import transitions

    assert transitions.path_between("chair", "plank") == [
        "mountain", "upward_salute", "forward_fold", "downward_dog", "table"]
    assert transitions.path_between("child_pose", "easy_seat") == [
        "table", "staff"]
    assert transitions.path_between("warrior_one_right", "bridge") == [
        "low_lunge_right", "table", "staff", "savasana"]
    assert transitions.path_between("figure_four_left", "figure_four_right") == [
        "savasana"]


def test_each_course_transition_reel_is_under_two_minutes() -> None:
    from yoga_v2 import courses

    review_seconds_per_move = 2.6 + 0.4
    totals = {course.id: len(course.steps) * review_seconds_per_move
              for course in courses.COURSES.values()}
    assert max(totals.values()) < 120, totals
