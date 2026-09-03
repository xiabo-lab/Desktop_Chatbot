"""The UI's deliberately narrow local-asset route.

The retained hand gesture model and the Boxing artwork made this the first
version of the page that loads files beside ``index.html``.  The route is
useful only if those files ship, and safe only if ``..`` can never turn it into
a filesystem reader.
"""

from __future__ import annotations

from itertools import product
import json
import re
import unittest

from aipi5.ui.server import ASSET_ROOT, ASSET_TYPES, asset_file


class TestBundledGameAssets(unittest.TestCase):

    def test_every_game_asset_resolves_inside_the_asset_folder(self):
        paths = (
            "/assets/boxing/boxing.js",
            "/assets/yoga/yoga.js",
            "/assets/yoga/yoga3d.js",
            "/assets/yoga/v3/rigdata.json",
            "/assets/yoga/v3/models/coach.vrm",
            "/assets/yoga/v3/stage.html",
            "/assets/yoga/clips/manifest.json",
            "/assets/yoga/clips/warrior_two_left/00.webp",
            "/assets/yoga/clips/triangle_left/06.webp",
            "/assets/boxing/arena-anime-v2.png",
            "/assets/boxing/player-red-torso.png",
            "/assets/boxing/opponent-blue-torso.png",
            "/assets/boxing/damage/manifest.json",
            "/assets/boxing/damage/opponent/1232.webp",
            "/assets/boxing/damage/player/13.webp",
            "/assets/boxing/poses/manifest.json",
            "/assets/boxing/poses/base/player/high_guard.png",
            "/assets/boxing/poses/base/opponent/right_cross_head.png",
            "/assets/boxing/poses/matrix/player/left_hook_body/23.webp",
            "/assets/boxing/poses/matrix/opponent/dodge_right/1232.webp",
            "/assets/mediapipe/vision_bundle.mjs",
            "/assets/mediapipe/gesture_recognizer.task",
            "/assets/mediapipe/wasm/vision_wasm_internal.js",
            "/assets/mediapipe/wasm/vision_wasm_internal.wasm",
            "/assets/mediapipe/wasm/vision_wasm_nosimd_internal.js",
            "/assets/mediapipe/wasm/vision_wasm_nosimd_internal.wasm",
        )
        root = ASSET_ROOT.resolve()
        for url in paths:
            with self.subTest(url=url):
                path = asset_file(url)
                self.assertIsNotNone(path)
                path.relative_to(root)
                self.assertGreater(path.stat().st_size, 0)

    def test_the_yoga_page_shows_the_coach_and_not_the_player(self):
        """The live feed is gone, and its absence is a rule rather than a tidy-up.

        A video of yourself in the corner is the thing you watch instead of
        the coach, so nothing of it may survive: not the element, not the CSS
        that showed it, and not the request that fed it. The camera keeps
        running -- every number on this screen is computed from it -- but on
        the Pi, not on the screen.
        """
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        for gone in ('id="yoga-camera"', "#yoga-camera", "yoga-camera-label"):
            self.assertNotIn(gone, page)
        for gone in ("yoga-camera", "function cameraOn",
                     '"/api/game/preview?fps=12'):
            self.assertNotIn(gone, script)
        for element in ("yoga-name", "yoga-sanskrit", "yoga-instruction",
                        "yoga-cue", "yoga-hold", "yoga-feedback", "yoga-clock",
                        "yoga-count", "yoga-segment", "yoga-score",
                        "yoga-result", "yoga-results", "yoga-hold-clock"):
            self.assertIn(f'id="{element}"', page)
        self.assertIn('/assets/yoga/yoga.js?v=20260821-courses', page)
        self.assertIn('/assets/yoga/yoga3d.js?v=20260824-ankles5', page)
        self.assertIn('id="yoga-3d-canvas"', page)

    def test_the_yoga_coach_is_photographed_from_the_rig_that_scores_her(self):
        """The pictures and the scoring targets come from one bone table.

        `build_yoga_coach.py` draws each guide with the scorer's own
        `forward_kinematics`, so this checks the two ends of that: the script
        imports the rig rather than restating it, and the page draws the
        manifest those guides produced. The scale is checked too -- the page
        and the packer disagreeing about pixels-per-spine is a coach who
        changes size the moment her pictures finish loading.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        builder = (ASSET_ROOT.parents[3] / "scripts"
                   / "build_yoga_coach.py").read_text(encoding="utf-8")
        self.assertIn("from aipi5.games.yoga import rig", builder)
        self.assertIn("rig.forward_kinematics(pose.table, pose.scales)", builder)
        self.assertIn('"/assets/yoga/coach/manifest.json"', script)
        self.assertIn("/assets/yoga/coach/${file}.webp", script)
        self.assertIn("/assets/yoga/field-forest.webp", script)
        # One number, in both files.
        self.assertIn("SERVED_UNIT = 160.0", builder)
        self.assertIn("UNIT = 160,", script)
        # A missing or slow picture must cost the picture and nothing else.
        self.assertIn("drawCoach(ctx, bones, scales);", script)

    def test_the_yoga_coach_pictures_are_complete_and_mirrored_in_pairs(self):
        """Every pose in the library has a picture, and the sides are one drawing."""
        import json as _json
        from aipi5.games.yoga.poses import AUTHORED as POSES
        # The 2D pipeline only ever drew the thirty-two poses authored in
        # `poses.py`, and now that the coach is a 3D model it never will draw
        # the rest. These assets are the fallback, so the invariant is about
        # the set they were actually built for.
        manifest = _json.loads(
            (ASSET_ROOT / "yoga" / "coach" / "manifest.json").read_text(
                encoding="utf-8"))
        # Every pose, plus the transition frames that are checked separately.
        self.assertLessEqual(set(POSES), set(manifest["poses"]))
        for pose_id, entry in manifest["poses"].items():
            with self.subTest(pose=pose_id):
                source = entry.get("mirror_of", pose_id)
                self.assertIsNotNone(
                    asset_file(f"/assets/yoga/coach/{source}.webp"))
                # The anchors are what stop her hopping between poses.
                for key in ("w", "h", "hip_x", "floor_y"):
                    self.assertIn(key, entry)
            if pose_id.endswith("_right"):
                self.assertEqual(entry.get("mirror_of"),
                                 pose_id[: -len("_right")] + "_left")

    def test_a_transition_moves_through_three_shapes_and_never_cuts(self):
        """Out of the pose she is in, through standing, into the next.

        The pictures are per pose, not per pair — every pose has one frame of
        its own halfway between standing and itself — because the three
        lessons contain 74 consecutive pairs. Three intermediate shapes fall
        out of that for any two poses, without a drawing for either pair.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn("function chain(rig)", script)
        self.assertIn("const STEP = 50;", script)
        self.assertIn('function stepId(pose) { return pose + "__" + STEP; }',
                      script)
        self.assertIn("if (coachPicture(stepId(from))) keys.push(stepId(from));",
                      script)
        self.assertIn('if (from !== "mountain" && to !== "mountain" '
                      '&& coachPicture("mountain")) {', script)
        # The in-between shapes are derived here, not sent: the payload
        # carries the two endpoint tables and nothing else.
        self.assertIn("function keyBones(id, rig)", script)
        self.assertIn("return blendBones(standing, table, Number(step) / 100);",
                      script)

    def test_every_transition_frame_that_exists_is_whole(self):
        """The manifest, the files and the mirroring have to agree.

        Separate from the coverage check below because they fail for different
        reasons: this one failing means the packer is wrong, that one failing
        means the artwork batch has not finished.
        """
        import json as _json
        manifest = _json.loads(
            (ASSET_ROOT / "yoga" / "coach" / "manifest.json").read_text(
                encoding="utf-8"))["poses"]
        for key, entry in manifest.items():
            with self.subTest(frame=key):
                source = entry.get("mirror_of", key)
                self.assertIsNotNone(
                    asset_file(f"/assets/yoga/coach/{source}.webp"))
                for anchor in ("w", "h", "hip_x", "floor_y"):
                    self.assertIn(anchor, entry)
            if key.endswith("_right") or "_right__" in key:
                base, _, step = key.partition("__")
                twin = base[: -len("_right")] + "_left" + (f"__{step}" if step else "")
                self.assertEqual(entry.get("mirror_of"), twin)
                # The page flips about the coach's x and then draws at
                # `-hip_x`, so the anchor must stay the *source* picture's.
                self.assertEqual(entry["hip_x"], manifest[twin]["hip_x"])

    def test_every_pose_has_its_halfway_frame_drawn(self):
        """One frame per pose is what makes a transition a movement.

        Skipped rather than failed while the artwork is unfinished: the
        renderer builds its chain out of the frames that exist and falls back
        to fewer, so a half-drawn library is a shorter transition and not a
        broken screen. The skip message says what is left to draw.
        """
        import json as _json
        from aipi5.games.yoga.poses import AUTHORED as POSES
        # The 2D pipeline only ever drew the thirty-two poses authored in
        # `poses.py`, and now that the coach is a 3D model it never will draw
        # the rest. These assets are the fallback, so the invariant is about
        # the set they were actually built for.
        manifest = _json.loads(
            (ASSET_ROOT / "yoga" / "coach" / "manifest.json").read_text(
                encoding="utf-8"))["poses"]
        missing = [f"{pose_id}__50" for pose_id in POSES
                   if pose_id != "mountain" and f"{pose_id}__50" not in manifest]
        if missing:
            self.skipTest(
                f"{len(missing)} halfway frames are not drawn yet "
                f"(first: {missing[0]}). Finish with: python "
                "scripts/build_yoga_coach.py art --reference "
                "artwork/yoga/coach-reference-anime.png --out "
                "artwork/yoga/poses && python scripts/build_yoga_coach.py pack")
        for pose_id in POSES:
            if pose_id == "mountain":
                continue
            with self.subTest(frame=f"{pose_id}__50"):
                entry = manifest[f"{pose_id}__50"]
                source = entry.get("mirror_of", f"{pose_id}__50")
                self.assertIsNotNone(
                    asset_file(f"/assets/yoga/coach/{source}.webp"))

    def test_a_transition_plays_a_film_of_her_moving_not_a_dissolve(self):
        """The clips are what a transition draws; the drawings are the fallback.

        Two still shapes cross-faded through each other is two bodies
        overlapping, and neither of them is moving. A clip is one continuous
        shot of the coach really going from standing into a pose, so a
        transition plays the one she is in backwards to stand her up and the
        next one forwards to put her down. That is why twenty clips cover the
        seventy-four transitions the three lessons contain, and why she is
        recognisably one person across each of them.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn('"/assets/yoga/clips/manifest.json"', script)
        self.assertIn("function clipAt(rig, blend)", script)
        self.assertIn("drawClipFrame(ctx, moving.frame, 1);", script)
        # Backwards out of the shape she is in, forwards into the next, with
        # standing the hinge between the halves.
        self.assertIn("return t < 0.5 ? play(leaving, 1 - t / 0.5)", script)
        self.assertIn("return leaving ? play(leaving, 1 - t) : play(entering, t);",
                      script)
        # The clip is asked for first and the chain of drawings still follows
        # it: a pair with no clip, or a clip that has not downloaded yet, has
        # to cost the film and nothing else.
        self.assertLess(script.index("const moving = rig ? clipAt(rig, blend)"),
                        script.index("const step = rig ? transitionKeys(rig, blend)"))
        self.assertIn("function chain(rig)", script)
        self.assertIn("drawCoach(ctx, bones, scales);", script)

    def test_every_pose_has_a_clip_and_the_two_sides_share_one(self):
        """Twenty clips, and every pose in the library reaches one of them.

        Standing is the exception rather than a gap: it is the first frame of
        every clip, so a clip of her standing still would be twenty copies of a
        frame that already ships. The right-hand poses are the left-hand clip
        mirrored, the same economy the drawings use.
        """
        import json as _json
        from aipi5.games.yoga.poses import AUTHORED as POSES
        # The 2D pipeline only ever drew the thirty-two poses authored in
        # `poses.py`, and now that the coach is a 3D model it never will draw
        # the rest. These assets are the fallback, so the invariant is about
        # the set they were actually built for.
        manifest = _json.loads(
            (ASSET_ROOT / "yoga" / "clips" / "manifest.json").read_text(
                encoding="utf-8"))
        poses = manifest["poses"]
        for pose_id in POSES:
            if pose_id == "mountain":
                continue
            source = pose_id
            if source not in poses and source.endswith("_right"):
                source = source[: -len("_right")] + "_left"
            with self.subTest(pose=pose_id):
                self.assertIn(source, poses)
                entry = poses[source]
                # One file per frame, and a placement for each of them.
                self.assertEqual(entry["frames"], manifest["frames"])
                self.assertEqual(len(entry["at"]), entry["frames"])
                for index in range(entry["frames"]):
                    self.assertIsNotNone(asset_file(
                        f"/assets/yoga/clips/{source}/{index:02d}.webp"))
                # She is placed in the stage's own pixels, so a box that leaves
                # it is a coach with a limb cut off at the edge of the canvas.
                for box in entry["at"]:
                    self.assertGreaterEqual(box["x"], 0)
                    self.assertGreaterEqual(box["y"], 0)
                    self.assertLessEqual(box["x"] + box["w"], manifest["width"])
                    self.assertLessEqual(box["y"] + box["h"], manifest["height"])

    def test_a_clip_ends_on_the_drawing_it_was_made_from(self):
        """The handover when she arrives must have nothing in it to see.

        Each clip was generated with two of the drawings as its first and last
        frame, so when it finishes and the still picture takes over they are
        the same shape in the same place. This checks the placement, which is
        where a disagreement would show: the page draws a clip frame at the
        packer's own offset and a drawing at `COACH_X - hip_x, FLOOR_Y -
        floor_y`, and if those two do not land on each other the coach jumps
        the instant she stops moving.

        The *first* frame of every clip is checked against standing for the
        same reason from the other end. It is what makes the hinge in the
        middle of a transition one more frame of movement rather than a cut
        from one clip to another.
        """
        import json as _json
        # Must match `COACH_X` and `FLOOR_Y` in yoga.js.
        coach_x, floor_y = 640, 726
        # Four pixels on a 1280-wide stage. The two pipelines round differently
        # -- one trims a drawing, the other crops a video frame -- so they are
        # never bit-identical, and four is under half the width of her wrist.
        tolerance = 4
        clips = _json.loads(
            (ASSET_ROOT / "yoga" / "clips" / "manifest.json").read_text(
                encoding="utf-8"))["poses"]
        drawings = _json.loads(
            (ASSET_ROOT / "yoga" / "coach" / "manifest.json").read_text(
                encoding="utf-8"))["poses"]

        def placed(pose_id):
            entry = drawings[pose_id]
            return (coach_x - entry["hip_x"], floor_y - entry["floor_y"],
                    entry["w"], entry["h"])

        standing = placed("mountain")
        for pose_id, entry in sorted(clips.items()):
            first, last = entry["at"][0], entry["at"][-1]
            for name, box, want in (("arrives at", last, placed(pose_id)),
                                    ("starts standing", first, standing)):
                with self.subTest(pose=pose_id, edge=name):
                    for axis, got, expected in zip(
                            ("x", "y", "w", "h"),
                            (box["x"], box["y"], box["w"], box["h"]), want):
                        self.assertLessEqual(
                            abs(got - expected), tolerance,
                            f"{pose_id} {name} is {abs(got - expected):.1f}px "
                            f"out on {axis}: clip {got}, drawing {expected}")

    def test_the_arrows_point_where_the_rig_says_the_limb_goes(self):
        """Drawn from the bone table, not painted into the artwork.

        There are 74 transitions and one rig; an arrow baked into a picture
        would be right for one of them. Hands and feet only - an arrow on
        every joint is a diagram of a skeleton, not an instruction.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn("function drawMoveArrows(ctx, rig, step, now)", script)
        self.assertIn("function screenJoints(bones, scales)", script)
        self.assertIn('const ARROW_JOINTS = ["left_wrist", "right_wrist", '
                      '"left_ankle", "right_ankle"];', script)
        # Only while she is moving, and only for joints that actually travel.
        self.assertIn('game.phase === "transition"', script)
        self.assertIn("< ARROW_MIN) continue;", script)

    def test_the_preview_holds_the_instruction_back(self):
        """Target pose first, instruction second - that is the whole phase."""
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn('id="yoga-preview-tag"', page)
        self.assertIn("#game-stage.yoga-previewing #yoga-preview-tag "
                      "{ display: block; }", page)
        # The instruction is shown for the movement, not for the preview.
        self.assertIn('#game-stage.yoga-active:not(.yoga-reading) '
                      '#yoga-instruction', page)
        self.assertIn('game.phase === "preview"', script)

    def test_the_class_has_music_that_never_restarts(self):
        """Synthesised, endless, and ducked under the coach rather than paused.

        A twenty-minute recording would be a twenty-minute download to a device
        fed by scp, would loop audibly, and would be the first sound here that
        somebody else owns.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn("function startMusic()", script)
        self.assertIn("function stopMusic()", script)
        self.assertIn("function duckMusic(under)", script)
        # Started once for the class, not once per pose.
        self.assertIn("if (active && playing) startMusic(); else stopMusic();",
                      script)
        self.assertIn("duckMusic(!!game.speaking);", script)
        # Chords glide and the end fades; nothing cuts.
        self.assertIn("linearRampToValueAtTime(" + chr(10)
                      + "          chord[i], at + CHORD_FADE);", script)
        self.assertIn("dying.out.gain.linearRampToValueAtTime(0.0001, at + 2);",
                      script)
        # And it stays under a speaking coach rather than stopping for her.
        self.assertIn("under ? MUSIC_GAIN * DUCK_GAIN : MUSIC_GAIN", script)
        # A browser that has not been touched keeps its audio clock at zero, so
        # the opening fade has to be scheduled again from whenever it starts.
        self.assertIn('if (ctx.state !== "running") '
                      "ctx.resume().then(fadeIn).catch(() => {});", script)

    def test_the_hold_bar_and_the_hold_number_are_one_timer(self):
        """A bar and a number that can disagree are two clocks, not one.

        Both are written from `holdClock`, which interpolates the Pi's
        `hold_left` rather than reading a clock of its own, so the bar moves
        every frame instead of stepping once a second and the figure in the
        corner is the same value rounded up.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn("function holdClock(game, now)", script)
        self.assertIn("function drawHoldBar(ctx, hold, holding)", script)
        self.assertIn("const hold = holdClock(game, now);", script)
        self.assertIn("drawHoldBar(ctx, hold, holding);", script)
        self.assertIn('String(Math.ceil(hold.left))', script)
        # Interpolated, not stepped: the frame time is what moves it.
        self.assertIn("if (game.holding && game.state === \"playing\") "
                      "holdShown -= dt;", script)
        # And the bar is the full width of the screen at the very bottom.
        self.assertIn("const height = 18, top = H - height;", script)
        self.assertIn("ctx.fillRect(0, top, W * fraction, height);", script)

    def test_the_end_of_a_class_is_not_called_game_over(self):
        """Nobody loses a yoga class.

        The sheet is deliberately the shared one — same buttons, same
        crossed-arms Play Again — so only the three words change, and they
        change back when the game does.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn('overHeading("YOGA COMPLETE");', script)
        self.assertIn('const SHARED_OVER = "GAME OVER";', script)
        self.assertIn("overHeading(SHARED_OVER)", script)

    def test_a_level_chosen_while_the_game_is_opening_is_not_dropped(self):
        """The choosing sheets are live 1.8 seconds before the Pi can answer.

        `/api/game/open` starts the camera and the pose pipeline inside the
        call, and Yoga's level sheet and Boxing's mode sheet are on screen a
        millisecond after the library tile is tapped. Sending the choice
        straight out meant a refusal nobody handled: the page advanced anyway,
        the next state said no level had been chosen, the sheet flipped back,
        and every player learned to tap twice.

        So both sheets go through the queue, and neither advances until the Pi
        has taken the choice.
        """
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        yoga = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        boxing = (ASSET_ROOT / "boxing" / "boxing.js").read_text(encoding="utf-8")

        self.assertIn("async function gameChoice(action)", page)
        self.assertIn("async function flushChoices()", page)
        # Kept on a refusal, dropped only on acceptance.
        self.assertIn("if (!answer.ok) break;", page)
        self.assertIn("pendingChoices.delete(knob);", page)
        # Retried the moment there is a session: on the open, and on every
        # state message after it.
        self.assertIn("if (pendingChoices.size) flushChoices();", page)
        self.assertIn("flushChoices();" + chr(10) + "  openGameStream();", page)
        # And not carried into the next game.
        self.assertIn("pendingChoices.clear();", page)

        # One slot per knob, so a mode and a course do not evict each other.
        self.assertIn('const knob = action.split("-")[0];', page)

        self.assertIn("gameChoice(`course-${button.dataset.yogaCourse}`)",
                      yoga)
        self.assertIn("if (answer.ok) showSheet(\"start\");", yoga)
        self.assertNotIn("gameCommand(`course-", yoga)
        for used in ('gameChoice("mode-training")', 'gameChoice("mode-fight")',
                     "gameChoice(`difficulty-${button.dataset.boxingDifficulty}`)"):
            self.assertIn(used, boxing)

    def test_the_yoga_sheet_offers_all_twenty_one_courses(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="sheet-yoga"', page)
        for level in ("beginner", "intermediate", "advanced"):
            self.assertIn(f'data-yoga-level="{level}"', page)
        self.assertIn('id="yoga-course-grid"', page)
        self.assertIn('21 courses', page)
        # The level sheet is one of the sheets `showSheet` knows how to hide,
        # or choosing a level leaves it on screen over the class.
        self.assertIn('["mode", "yoga", "start", "paused", "over", "error"]',
                      page)
        # And an X held over it must not start a lesson nobody has chosen.
        self.assertIn('const yogaNeedsCourse = gameId === "yoga" '
                      '&& !game.course;', page)

    def test_four_yoga_courses_fit_each_selector_row(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr));", page)
        self.assertIn("width: 100%; min-width: 0; height: 128px", page)

    def test_yoga_has_pause_menu_and_no_legacy_duration_copy(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn("#game-pause {", page)
        self.assertIn("font-size: 21px; z-index: 7;", page)
        # Regex rather than a contiguous string: the bilingual sweep added a
        # `data-i18n` attribute between the id and the text. The pairing of
        # that id with those words is what the test is about.
        self.assertRegex(page, r'id="paused-exit"[^>]*>Return to Game Menu')
        self.assertIn('gameId === "yoga"' + chr(10) + '      ? ""', page)
        self.assertIn('answer.data.game && id !== "yoga"', page)

    def test_the_manual_head_review_covers_all_91_poses(self):
        page = (ASSET_ROOT / "yoga" / "v3" / "stage.html").read_text(
            encoding="utf-8")
        self.assertIn('P.get("review") === "1"', page)
        self.assertIn('91-POSE HEAD REVIEW', page)
        self.assertIn('if (reviewing) list = allPoseIds;', page)
        self.assertIn('original #${poseNumber(id)}', page)
        self.assertIn('aipi5-yoga-pose-review-v4', page)
        self.assertIn('saveReview("approved")', page)
        self.assertIn('saveReview("needs-fix")', page)
        self.assertIn('yoga-pose-review.json', page)
        self.assertIn('aipi5-yoga-pose-edits-v5', page)
        self.assertIn('headTitle.textContent = "Head direction"', page)
        self.assertIn('"Look up / down", ["bones3d", "neck", 0]', page)
        self.assertIn('"Look left / right", ["bones3d", "neck", 1]', page)
        self.assertIn('"Ear tilt left / right", ["bones3d", "neck", 2]', page)
        self.assertIn('path[0] === "terminal3d" || path[0] === "bones3d"', page)
        self.assertIn('id="review-edit"', page)
        self.assertIn('Flip both feet ${"XYZ"[axis]}', page)
        self.assertIn('pose_edits: clonePose(poseEdits)', page)
        self.assertIn('edited_pose = edit', page)
        self.assertIn('id="review-toggle"', page)
        self.assertIn('panel.classList.toggle("minimized")', page)
        self.assertIn('renderer.domElement.addEventListener("pointermove"', page)
        self.assertIn('renderer.domElement.addEventListener("wheel"', page)
        self.assertIn('orbit.elevation = 82 * Math.PI / 180', page)
        self.assertIn('if (reviewing) placeReviewCamera();', page)

    def test_yoga_reuses_the_shared_gesture_start_and_play_again(self):
        """No second gesture implementation, and no second pose pipeline."""
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn('if (window.YogaUI) window.YogaUI.onOpen(id);', page)
        self.assertIn('if (window.YogaUI) window.YogaUI.stop();', page)
        self.assertIn("window.YogaUI.applyState(state);", page)
        self.assertIn("window.YogaUI.render(ctx, now, gameState, gameFps);", page)
        # The gesture, the readiness and the preview all come from the shared
        # code; yoga adds none of its own.
        self.assertNotIn("MediaPipe", script)
        self.assertNotIn("getUserMedia", script)
        self.assertNotIn("arms_crossed", script)

    def test_the_retained_gesture_model_is_not_a_placeholder(self):
        model = asset_file("/assets/mediapipe/gesture_recognizer.task")
        self.assertIsNotNone(model)
        self.assertGreater(model.stat().st_size, 1_000_000)

    def test_literal_traversal_is_refused(self):
        self.assertIsNone(asset_file("/assets/../index.html"))

    def test_percent_encoded_traversal_is_refused(self):
        self.assertIsNone(asset_file("/assets/%2e%2e/index.html"))

    def test_a_missing_asset_is_refused(self):
        self.assertIsNone(asset_file("/assets/boxing/not-there.png"))

    def test_wasm_and_modules_have_explicit_mime_types(self):
        self.assertEqual(ASSET_TYPES[".wasm"], "application/wasm")
        self.assertEqual(ASSET_TYPES[".vrm"], "model/gltf-binary")
        self.assertIn("text/html", ASSET_TYPES[".html"])
        self.assertIn("javascript", ASSET_TYPES[".mjs"])
        self.assertEqual(ASSET_TYPES[".webp"], "image/webp")
        self.assertIn("application/json", ASSET_TYPES[".json"])

    def test_settings_page_offers_every_supported_round_duration(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        for seconds in (60, 120, 180, 240, 300):
            self.assertIn(f'data-game-seconds="{seconds}"', page)
        self.assertIn('fetch("/api/game/settings"', page)

    def test_settings_has_one_master_volume_for_every_application(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="set-volume" type="range" min="0" max="100"', page)
        self.assertIn('fetch("/api/volume"', page)
        for application in ("Game", "Call", "Kodama-Lite", "Browser",
                            "Agent Talk"):
            self.assertIn(application, page)

    def test_the_bomb_no_longer_touches_the_clock(self):
        """The countdown is monotone again, and nothing on the page fakes it.

        A bomb used to take five seconds, and the page flinched the clock and
        threw the lost seconds off it — because a five-second drop with no
        reaction reads as a broken countdown, which is how the first person to
        play it reported the bug. The bomb costs a life and a score now, so all
        of that machinery is gone rather than left inert: a clock animation
        that can never fire is a clock animation somebody will one day wire
        back up to the wrong event.
        """
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        for dead in ("game-clock-penalty", "flashClockPenalty",
                     "clearClockPenalty", "@keyframes clock-penalty"):
            self.assertNotIn(dead, page)

    def test_the_hud_draws_the_lives_it_is_told_about(self):
        """Three hearts in the top-right corner, and none of it hard-coded.

        `max_lives` comes off the snapshot, so a Pi restarted with a different
        number gets a row of that length rather than a row of three with the
        rest of the rule invisible.
        """
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('<div id="game-lives" aria-live="polite"></div>', page)
        self.assertIn("function renderLives(game)", page)
        self.assertIn("row.innerHTML = heartSvg().repeat(max)", page)
        self.assertIn('hearts[i].classList.toggle("spent", i >= lives)', page)
        # The heart the corner draws and the heart the canvas draws are the
        # same object, which is how a player learns what the fruit is for.
        self.assertIn("const HEART_PATH =", page)
        self.assertIn("function heartPath(ctx, size)", page)
        self.assertIn("heart(ctx, r, item) {", page)

    def test_the_bomb_says_what_it_cost_from_the_event(self):
        """So the page and `BOMB_PENALTY_POINTS` cannot disagree after a
        restart that changed one of them."""
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('popup(e.x, e.y - 20, "-" + bombPoints(e)', page)
        self.assertIn("Number(e && e.points)", page)

    def test_main_header_uses_the_requested_chinese_name(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>小爱同学</title>", page)
        self.assertIn("<h1>小爱同学</h1>", page)
        self.assertNotIn("<h1>AI ASSISTANT</h1>", page)

    def test_exit_suppresses_stale_game_adoption_until_close_is_seen(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('let dismissedGameId = "";', page)
        self.assertIn("if (closingId) dismissedGameId = closingId;", page)
        self.assertIn("game.active === dismissedGameId", page)
        self.assertIn('if (!game || !game.active)', page)
        self.assertIn("if (gameCloseRequest) await gameCloseRequest", page)

    def test_the_ninja_figure_is_gone_from_the_play_field(self):
        """Removed on request: the play field is wood, fruit and blades.

        Asserted as an absence because that is what the change was. The figure
        was five hundred lines that ran inside the render loop, and a partial
        removal — the draw call gone but the pose still smoothed every frame,
        or the reverse — is the failure this catches.
        """
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        for gone in ("drawPlayerShadow", "retargetNinjaPose", "NINJA_RIG",
                     "updateShadow", "resetShadow", "shadowCanvas",
                     "ninja-head.png"):
            self.assertNotIn(gone, page)

    def test_boxing_does_not_mirror_anatomical_sides_twice(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn(
            "centreX + ((point[0] - centre) / span) * 380", script)
        self.assertNotIn(
            "centreX - ((point[0] - centre) / span) * 380", script)

    def test_boxing_opponent_telegraphs_and_drives_punches_toward_player(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("function drawAttackCue", script)
        self.assertIn("attack.phase === \"recover\"", script)
        self.assertIn("const targetY = attack.target === \"body\" ? 310 : 240", script)
        self.assertIn("1.06 + motion.drive * .09", script)
        self.assertIn("createRadialGradient(shoulderX, shoulderY", script)
        self.assertNotIn('replaceAll("_", " ").toUpperCase()', script)

    def test_boxing_uses_layered_anime_arena_and_filtered_player_rig(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("/assets/boxing/arena-anime-v2.png", script)
        self.assertIn("/assets/boxing/player-red-torso.png", script)
        self.assertIn("/assets/boxing/opponent-blue-torso.png", script)
        self.assertIn("function smoothPlayerRig", script)
        self.assertIn("function constrainJoint", script)
        self.assertIn("ctx.save(); ctx.globalAlpha = .68;", script)
        self.assertIn("function drawImpactEffect", script)

    def test_boxing_reference_fighters_keep_red_player_and_blue_opponent(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn('side === "left" ? "#ef3b48" : "#bc243b"', script)
        self.assertIn('training ? "#49cfe8" : "#164f9f"', script)
        self.assertIn('drawDamageBody(ctx, "player"', script)
        self.assertIn("510, 540], .62", script)

    def test_the_player_is_on_screen_and_the_view_is_not_their_eyes(self):
        """The third-person view, stated as the absence of first person.

        The failure a partial revert produces is specific: gloves still
        hanging off the bottom of the frame in front of a player who is
        also drawn in the middle of it, moving with the same hands.
        """
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("function drawPlayer", script)
        for gone in ("function drawFirstPersonHands", "function drawFirstPersonArm",
                     "function headCamera", "function applyCamera",
                     "/assets/boxing/fp/"):
            self.assertNotIn(gone, script)

    def test_boxing_uses_jointed_reference_arms_and_three_lanes(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("function muscleSegment", script)
        self.assertIn("function drawAnimeArm", script)
        self.assertIn("drawAnimeArm(ctx, shoulder, elbow, wrist", script)
        self.assertIn('smoothLane("player", playerLane', script)
        self.assertIn('smoothLane("opponent", laneIndex(ai.lane)', script)
        self.assertNotIn("function arm(ctx", script)

    def test_boxing_uses_complete_baked_damage_images_not_canvas_overlays(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("const DAMAGE_CACHE_LIMIT = 12", script)
        self.assertIn("/assets/boxing/damage/${kind}/${key}.webp", script)
        self.assertIn("function drawDamageBody", script)
        self.assertNotIn("function drawInjury", script)
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('/assets/boxing/boxing.js?v=20260819-3p', page)

    def test_boxing_action_feedback_is_small_and_bottom_centred(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("#boxing-message.action", page)
        self.assertIn("bottom: 42px; font-size: 20.4px", page)
        self.assertIn('showMessage("BLOCK", 520, "action"', script)
        self.assertIn('showMessage("DODGE!", 480, "action"', script)
        self.assertIn('showMessage("PERFECT PARRY", 1500, "parry action"', script)

    def test_boxing_combo_is_small_and_top_right(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn('<div id="boxing-combo"></div>', page)
        self.assertIn("#boxing-combo", page)
        self.assertIn("right: 28px; top: 126px; font-size: 8.4px", page)
        self.assertIn('gameEl("boxing-combo").textContent', script)
        self.assertNotIn('showMessage(`${event.combo}× COMBO`', script)

    def test_boxing_maps_motion_to_complete_pose_matrix_fighters(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("function playerPoseFor", script)
        self.assertIn("function opponentPoseFor", script)
        self.assertIn("function drawPoseBody", script)
        self.assertIn(
            "/assets/boxing/poses/matrix/${kind}/${pose}/${key}.webp", script)
        self.assertIn("left_straight_${target}", script)
        self.assertIn("right_hook_${target}", script)
        for pose in ("high_guard", "dodge_left", "duck", "lean_back",
                     "knockout"):
            self.assertIn(f'"{pose}"', script)

    def test_boxing_pose_damage_matrix_is_complete(self):
        poses = ASSET_ROOT / "boxing" / "poses"
        manifest = json.loads((poses / "manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(manifest["clean_pose_bases"], 41)
        self.assertEqual(manifest["player"]["pose_count"], 21)
        self.assertEqual(manifest["opponent"]["pose_count"], 20)
        self.assertEqual(manifest["player"]["count"], 336)
        self.assertEqual(manifest["opponent"]["count"], 5120)
        self.assertEqual(manifest["total"], 5456)

        player_keys = {
            "".join(map(str, levels))
            for levels in product(range(4), repeat=2)}
        opponent_keys = {
            "".join(map(str, levels))
            for levels in product(range(4), repeat=4)}
        for pose in manifest["player"]["poses"]:
            files = {path.stem for path in
                     (poses / "matrix" / "player" / pose).glob("*.webp")}
            self.assertEqual(files, player_keys, pose)
        for pose in manifest["opponent"]["poses"]:
            files = {path.stem for path in
                     (poses / "matrix" / "opponent" / pose).glob("*.webp")}
            self.assertEqual(files, opponent_keys, pose)

    def test_boxing_damage_manifest_and_all_matrix_images_are_complete(self):
        damage = ASSET_ROOT / "boxing" / "damage"
        manifest = json.loads((damage / "manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(manifest["opponent"]["key_order"], [
            "left_eye", "right_eye", "left_shoulder", "right_shoulder"])
        self.assertEqual(manifest["player"]["key_order"], [
            "left_shoulder", "right_shoulder"])
        self.assertEqual(manifest["total"], 272)
        expected_opponent = {
            "".join(map(str, levels)) for levels in product(range(4), repeat=4)}
        expected_player = {
            "".join(map(str, levels)) for levels in product(range(4), repeat=2)}
        opponent_files = {path.stem for path in (damage / "opponent").glob("*.webp")}
        player_files = {path.stem for path in (damage / "player").glob("*.webp")}
        self.assertEqual(opponent_files, expected_opponent)
        self.assertEqual(player_files, expected_player)


class TheBladeIsOneWidth(unittest.TestCase):
    """The drawn blade and the tested blade are the same object.

    `collision.BLADE_HALF_WIDTH` decides what cuts and `BLADE_HALF_WIDTH_PX` in
    the page decides what the player sees, and nothing in the running system
    connects them — the snapshot does not carry the number, because nothing the
    page draws depends on reading it back. So they are kept equal by hand, and
    a hand is exactly the thing that forgets.

    The failure is not a crash. It is a blade that visibly passes through a
    fruit and does not cut it, or cuts one it did not touch, and both read to a
    player as broken tracking rather than as a mistuned constant. That is worth
    a test that can only ever fail for one reason.
    """

    def test_the_page_and_the_simulation_agree(self):
        from aipi5.games.fruit_ninja.collision import BLADE_HALF_WIDTH

        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        found = re.search(r"const BLADE_HALF_WIDTH_PX = ([0-9.]+);", page)
        self.assertIsNotNone(found, "the page no longer declares the blade width")
        self.assertAlmostEqual(float(found.group(1)), BLADE_HALF_WIDTH, places=3)


if __name__ == "__main__":
    unittest.main()
