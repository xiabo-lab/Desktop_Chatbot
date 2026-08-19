"""The UI's deliberately narrow local-asset route.

The retained hand gesture model and the Boxing artwork made this the first
version of the page that loads files beside ``index.html``.  The route is
useful only if those files ship, and safe only if ``..`` can never turn it into
a filesystem reader.
"""

from __future__ import annotations

from itertools import product
import json
import unittest

from aipi5.ui.server import ASSET_ROOT, ASSET_TYPES, asset_file


class TestBundledGameAssets(unittest.TestCase):

    def test_every_game_asset_resolves_inside_the_asset_folder(self):
        paths = (
            "/assets/boxing/boxing.js",
            "/assets/boxing/fp/glove.webp",
            "/assets/boxing/fp/forearm.webp",
            "/assets/boxing/fp/manifest.json",
            "/assets/yoga/yoga.js",
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

    def test_the_yoga_page_has_the_coach_and_the_live_feed_side_by_side(self):
        """Both must be on screen at once, and neither may cover the other.

        The coach is drawn on the shared canvas; the player is an MJPEG
        element positioned beside it. The camera is the one element in this
        game that nothing is allowed on top of, because it is what the player
        is correcting themselves in.
        """
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('<img id="yoga-camera" alt="">', page)
        self.assertIn("#game-stage.yoga-playing #yoga-camera { display: block; }",
                      page)
        for element in ("yoga-name", "yoga-sanskrit", "yoga-instruction",
                        "yoga-cue", "yoga-hold", "yoga-feedback", "yoga-clock",
                        "yoga-count", "yoga-segment", "yoga-score",
                        "yoga-result", "yoga-results"):
            self.assertIn(f'id="{element}"', page)
        self.assertIn('/assets/yoga/yoga.js?v=20260817-1', page)

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

    def test_the_yoga_level_sheet_offers_all_three_lessons(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="sheet-yoga"', page)
        for level in ("beginner", "intermediate", "advanced"):
            self.assertIn(f'data-yoga-difficulty="{level}"', page)
        # The level sheet is one of the sheets `showSheet` knows how to hide,
        # or choosing a level leaves it on screen over the class.
        self.assertIn('["mode", "yoga", "start", "paused", "over", "error"]',
                      page)
        # And an X held over it must not start a lesson nobody has chosen.
        self.assertIn('const yogaNeedsLevel = gameId === "yoga" '
                      '&& !game.difficulty;', page)

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

    def test_the_yoga_live_feed_asks_for_a_rate_the_server_will_agree_to(self):
        """Five frames a second reads as a broken camera in a mirror.

        The page asks for twelve and a wider frame; the server clamps both, so
        a stale tab cannot take a core away from pose inference.
        """
        script = (ASSET_ROOT / "yoga" / "yoga.js").read_text(encoding="utf-8")
        self.assertIn('"/api/game/preview?fps=12&w=640&t="', script)
        from aipi5.ui.server import GAME_PREVIEW_FPS_MAX, GAME_PREVIEW_WIDTH_MAX
        self.assertGreaterEqual(GAME_PREVIEW_FPS_MAX, 12)
        self.assertGreaterEqual(GAME_PREVIEW_WIDTH_MAX, 640)
        self.assertLessEqual(GAME_PREVIEW_FPS_MAX, 15)

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
        self.assertIn("javascript", ASSET_TYPES[".mjs"])
        self.assertEqual(ASSET_TYPES[".webp"], "image/webp")
        self.assertIn("application/json", ASSET_TYPES[".json"])

    def test_settings_page_offers_every_supported_round_duration(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        for seconds in (60, 120, 180, 240, 300):
            self.assertIn(f'data-game-seconds="{seconds}"', page)
        self.assertIn('fetch("/api/game/settings"', page)

    def test_the_clock_reacts_to_a_bomb(self):
        """A five-second drop with no reaction reads as a broken countdown.

        The popup over the bomb is in the middle of the screen; the seconds go
        from the top-right corner. Reported from real play as "the timer went
        from 24 to 20", which is exactly what one bomb does.
        """
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('<div id="game-clock-penalty"></div>', page)
        self.assertIn("#game-clock-col.penalty", page)
        self.assertIn("@keyframes clock-penalty", page)
        self.assertIn("flashClockPenalty(bombSeconds(e))", page)
        # From the event, so the page and `BOMB_PENALTY_S` cannot disagree.
        self.assertIn('Number(e && e.seconds)', page)

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
        self.assertIn("createRadialGradient(shoulderX, shoulderY", script)
        self.assertIn("OPPONENT_SCALE + motion.drive * .14", script)
        self.assertNotIn('replaceAll("_", " ").toUpperCase()', script)

    def test_boxing_is_played_from_behind_the_players_own_eyes(self):
        """First person, stated as the absence of a third-person player.

        The rear-view fighter was a rig, a torso image and a twenty-one
        pose matrix, and the failure a partial removal produces is very
        specific: a body still drawn in the middle of a view that is
        supposed to be looking out of its head.
        """
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("/assets/boxing/arena-anime-v2.png", script)
        self.assertIn("/assets/boxing/opponent-blue-torso.png", script)
        self.assertIn("function drawFirstPersonHands", script)
        self.assertIn("function drawFirstPersonArm", script)
        self.assertIn("function drawImpactEffect", script)
        for gone in ("function smoothPlayerRig", "function constrainJoint",
                     "function drawPlayerLimb", "function playerPoseFor",
                     "player-red-torso.png", "globalAlpha = .68"):
            self.assertNotIn(gone, script)

    def test_the_players_head_is_the_camera(self):
        """Leaning has to move the room, not a figure standing in it.

        Two depths and no more: the arena is across the room and the
        opponent is within reach, so one movement of the head shifts them
        by different amounts. That parallax is the whole illusion, and one
        flat camera applied to both would throw it away.
        """
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("function headCamera", script)
        self.assertIn("function applyCamera", script)
        self.assertIn("applyCamera(ctx, camera, .34)", script)
        self.assertIn("applyCamera(ctx, camera, 1)", script)
        # The gloves hang off the same head, so the camera must be put
        # away before they are drawn or they move twice.
        hands = script.index("drawFirstPersonHands(ctx, payload, now)")
        restore = script.index("ctx.restore();", script.index(
            "applyCamera(ctx, camera, 1)"))
        self.assertLess(restore, hands)

    def test_the_players_gloves_come_from_the_approved_artwork(self):
        """One right arm, cut in two at the wrist, mirrored for the left.

        Two pieces because they move differently — the glove sits at the
        tracked hand and the forearm stretches to reach it from off-frame
        — and one arm because two would be two things to keep matching.
        """
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("/assets/boxing/fp/${part}.webp", script)
        self.assertIn("/assets/boxing/fp/manifest.json", script)
        self.assertIn('if (side === "left") { ctx.translate(W, 0); '
                      "ctx.scale(-1, 1); }", script)
        self.assertIn('training ? "#49cfe8" : "#164f9f"', script)
        self.assertNotIn('drawDamageBody(ctx, "player"', script)

        # **The negative y scale is the arm's roll.** The sprite is a right arm
        # reaching to the right, so pointing it up and inward takes it past
        # vertical and lands its top edge underneath — which shows up as both
        # arms upside down *and* as the pair swapped, because an arm reflected
        # along its own length is the other arm. Both were reported from the
        # screen; neither is visible in any other test.
        self.assertIn("ctx.scale(armLength / axisLength, "
                      "-scale * FP_ARM_WIDTH);", script)
        self.assertIn("ctx.scale(scale, -scale);", script)

        manifest = json.loads(
            (ASSET_ROOT / "boxing" / "fp" / "manifest.json").read_text(
                encoding="utf-8"))
        # The renderer rotates the glove about its wrist and stretches the
        # forearm between two named points. A missing anchor would not
        # fail — it would hang a glove off the side of its own arm.
        self.assertEqual(manifest["glove"]["wrist"][0], 0)
        self.assertEqual(manifest["forearm"]["elbow"][0], 0)
        self.assertEqual(manifest["forearm"]["wrist"][0],
                         manifest["forearm"]["width"])

    def test_a_punch_that_lands_on_the_player_is_taken_by_the_view(self):
        """There is no body on screen to flinch, so the picture has to."""
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("function hitFlash", script)
        self.assertIn("now < hitFlashUntil", script)

    def test_boxing_uses_jointed_reference_arms_and_three_lanes(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("function muscleSegment", script)
        self.assertIn("function drawAnimeArm", script)
        self.assertIn("drawAnimeArm(ctx, shoulder, elbow, wrist", script)
        # Only the opponent has lanes now; the player's lane is the camera.
        self.assertIn('smoothLane("opponent", laneIndex(ai.lane)', script)
        self.assertNotIn('smoothLane("player"', script)
        self.assertNotIn("function arm(ctx", script)

    def test_boxing_uses_complete_baked_damage_images_not_canvas_overlays(self):
        script = (ASSET_ROOT / "boxing" / "boxing.js").read_text(
            encoding="utf-8")
        self.assertIn("const DAMAGE_CACHE_LIMIT = 12", script)
        self.assertIn("/assets/boxing/damage/${kind}/${key}.webp", script)
        self.assertIn("function drawDamageBody", script)
        self.assertNotIn("function drawInjury", script)
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('/assets/boxing/boxing.js?v=20260818-2', page)

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
        self.assertIn("function opponentPoseFor", script)
        self.assertIn("function drawPoseBody", script)
        self.assertIn(
            "/assets/boxing/poses/matrix/${kind}/${pose}/${key}.webp", script)
        # The opponent's twenty poses are still driven by live AI state.
        # The player's twenty-one are not: nothing on screen is the player.
        self.assertIn('drawPoseBody(ctx, "opponent"', script)
        self.assertNotIn('drawPoseBody(ctx, "player"', script)
        for pose in ("high_guard", "body_guard", "dodge_left", "dodge_right",
                     "head_hit_right", "body_hit", "parried", "knockout"):
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


if __name__ == "__main__":
    unittest.main()
