---
name: fruit-ninja-experience
description: Plan, implement, tune, or review the AIPI5 camera-controlled Fruit Ninja experience. Use for slicing feel, onboarding, difficulty, HUD, accessibility, feedback, playtesting, telemetry, rendering performance, or new Fruit Ninja modes; preserve the project's authoritative motion-game architecture and fairness rules.
---

# Fruit Ninja Experience

Improve the room-scale slicing experience without weakening input fidelity, visual clarity, or restart safety.

## Read first

- For planned work and acceptance metrics, read [`design/fruit_ninja_experience_plan.md`](../../../design/fruit_ninja_experience_plan.md).
- For current implementation decisions and measured device behavior, search `REPORT.md` for `Fruit Ninja`, then inspect the relevant current code and tests. Do not assume an older report section still describes the present rules.

## Product frame

- Core fantasy: become a room-scale ninja whose real arm movement becomes a bright, trustworthy blade.
- Primary loop: read an arc, choose a target, swing, receive immediate feedback, build a combo, and decide whether to chase the next fruit or avoid a bomb.
- Target qualities: responsive, fair, readable across the room, physically comfortable, and rewarding in the first 30 seconds.

## Architecture invariants

1. Keep simulation, collision, score, lives, phase transitions, and RNG authoritative in Python under `aipi5/games/fruit_ninja/`.
2. Keep the browser responsible for 60 Hz presentation, client-only effects, and DOM HUD. It can extrapolate snapshots but must not decide hits or score.
3. Keep motion-space positions in the camera's normalized coordinates for Fruit Ninja. Do not apply `PersonPose.shaped()` aspect correction to slicing positions.
4. Match drawn reach to tested reach for rewards. Bombs keep their exact radius and do not inherit the blade-width assist.
5. Do not freeze the authoritative simulation or stop accepting pose input for hit-stop. If impact pause is useful, hold only client-side debris or presentation for a few frames.
6. Preserve pause, resume, replay, teardown, page reload, and late-pose safety. Every new state must reset cleanly.
7. Keep balance values named and testable. Use seeded sessions for comparisons and change one tuning variable at a time.

## Experience workflow

1. State the player problem as an observable hypothesis, not a feature request.
2. Capture the baseline: completion reason, bomb hits, fruit misses, combo distribution, Ultimate result, pose/render rate, and hand-to-feedback latency.
3. Choose the smallest layer that can solve it:
   - mechanics or fairness in Python;
   - feedback and rendering in Canvas/Web Audio;
   - text-heavy HUD, menus, and settings in DOM/CSS;
   - motion-pipeline changes only when profiling shows the input path is the cause.
4. Attach feedback to existing discrete events. Use small, medium, and large feedback tiers so routine slices do not compete with bombs or the Ultimate.
5. Keep the center and lower-middle of the playfield clear. Use corner HUD clusters and short contextual prompts.
6. Add unit and lifecycle coverage, then validate synthetic browser states and inspect screenshots.
7. For changes to tracking, effects, balance, or readability, finish with on-device play using a person. Record measured behavior rather than relying on a desktop render.

## Feel rules

- A normal slice should combine a crisp sound, visible blade contact, fruit separation, juice/debris, and a short score pop within one perceptual beat.
- Scale feedback by importance. Normal fruit is small, combos and power-ups are medium, and bombs/Ultimate completion are large.
- Prefer eased, self-ending visual motion. Effects must return to rest and must not obscure the next target.
- Avoid global camera shake by default because screen displacement breaks the visual agreement between the player's hand and the target. Use edge pulses, local recoil, trail intensity, and audio weight first.
- Provide reduced-flash, reduced-motion, and sound controls before adding stronger effects.

## Fairness rules

- A visible blade overlap with fruit should normally score; a near miss around a bomb should remain a miss.
- Dropped ordinary fruit must not cost a life. Bombs are the judgment failure and the only life loss.
- Do not silently change difficulty from current score during a comparable high-score round. Prefer explicit modes or pre-round recommendations.
- Teach hazards after the player has demonstrated the basic slice, and never place a player in an unwinnable or unreadable pattern.

## Optional technology gates

- Keep Canvas 2D unless measured performance and a specific visual requirement justify WebGL or Three.js. Do not rewrite a working motion game merely to make it 3D.
- Keep the core game local and single-player. Consider asynchronous ghosts, household challenges, or leaderboards before real-time multiplayer; never stream camera frames or raw pose data by default.
- Generated art can be used for exploration only after style, license, provenance, asset budget, and on-device performance are defined. The runtime must not depend on an external generation service.

## Verification minimum

- Run the affected Fruit Ninja, collision, Ultimate, UI, and lifecycle tests.
- Compare seeded simulations before and after balance changes.
- Exercise ready -> start -> play -> pause -> resume -> game over -> replay -> exit.
- Inspect at least the opening, busy normal play, bomb feedback, power-up feedback, Ultimate, pause, and results states.
- Check no new console errors, stale effects, duplicate sounds, score drift, or late events after teardown.
- For device-sensitive changes, report pose FPS, render FPS, capture-to-pose latency, and at least one real-player round.
