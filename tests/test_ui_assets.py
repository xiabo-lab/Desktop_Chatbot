"""The UI's deliberately narrow local-asset route.

The retained hand gesture model and active ninja head made this the first
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
            "/assets/fruit-ninja/ninja-head.png",
            "/assets/boxing/boxing.js",
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

    def test_the_retained_gesture_model_is_not_a_placeholder(self):
        model = asset_file("/assets/mediapipe/gesture_recognizer.task")
        self.assertIsNotNone(model)
        self.assertGreater(model.stat().st_size, 1_000_000)

    def test_literal_traversal_is_refused(self):
        self.assertIsNone(asset_file("/assets/../index.html"))

    def test_percent_encoded_traversal_is_refused(self):
        self.assertIsNone(asset_file("/assets/%2e%2e/index.html"))

    def test_a_missing_asset_is_refused(self):
        self.assertIsNone(asset_file("/assets/fruit-ninja/not-there.png"))

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

    def test_ninja_uses_fixed_anime_proportions_not_player_body_size(self):
        page = (ASSET_ROOT.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn("function retargetNinjaPose(source)", page)
        self.assertIn("const thick = NINJA_RIG.cloth", page)
        self.assertIn("const headR = NINJA_RIG.headRadius", page)
        self.assertNotIn("span * 0.30", page)

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
        self.assertIn('/assets/boxing/boxing.js?v=20260816-7', page)

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


if __name__ == "__main__":
    unittest.main()
