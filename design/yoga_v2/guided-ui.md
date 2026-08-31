# Guided mode — what the screen does when nothing is being scored

252 of the curriculum's 575 steps are guided. On those the accuracy dial has
nothing to say, and **an empty rectangle where a number used to be reads as a
broken screen, not as a deliberate mode.** This specifies what goes there
instead, and how a player finds out before they start.

Guided is not a degraded experience. It is the mode a relaxation pose belongs
in, and the screen should look like it was designed for it.

---

## 1. On the course-selection screen

Four courses are guided-heavy, and a player choosing one deserves to know what
kind of class it is *before* they commit sixteen minutes to it.

| Course | Scored steps | Badge |
|---|---:|---|
| Back and Shoulder Release | 2 of 25 | **Guided Focus** |
| Relax and Recover | 3 of 23 | **Guided Focus** |
| Deep Mobility | 6 of 28 | **Guided Focus** |
| Hip Mobility Flow | 8 of 26 | **Guided Focus** |

**Rule.** A course whose scored steps are **under 40%** carries the badge
`Guided Focus`, with the subtitle `Mostly guided · relaxation & mobility`. The
threshold is computed from the catalog, never hand-maintained — `course_catalog
.json` already carries `scored_steps` and `pose_count` for exactly this.

Courses above the threshold show nothing extra. The badge marks a genuine
difference in what the class is, not a warning about a deficiency.

---

## 2. During a guided pose

The accuracy dial at `(1150, 548)` and the correction line along the bottom are
both scoring furniture. In guided mode they are **replaced**, not hidden.

What takes their place depends on what the pose is for — the pose's own band
already says which:

| Band | Replaces the dial with | Example |
|---|---|---|
| `relax` | `Relax & Breathe` above the hold clock | Savasana, Seated Breathing |
| `stretch` | `N slow breaths`, counting down as they pass | Butterfly, Pigeon, Child's Pose |
| `dynamic` | `Round N of M` | Cat–Cow, the salutation steps |
| `balance`, `hatha` | `Follow the Coach` | Plank, Downward Dog, Bridge |

The breath count is already in the catalog — `breaths_for(hold)` at the 5.5 s
cadence — so `4 slow breaths` is a real number derived from the hold, not a
decoration. §6 asks for exactly that.

**The hold clock, the lesson progress bar and the pose card stay put.** They are
not scoring UI; they are the class. Only the accuracy dial and the correction
line change.

### The transition between modes should feel intentional

Scored and guided poses alternate constantly — a standing sequence into Child's
Pose and back out. The dial appearing and vanishing at each boundary would
flicker.

**Cross-fade the panel over the pose's `preview` phase**, which exists precisely
because the coach is standing still and the player is looking at the next shape.
By the time the movement starts, the panel already says what this pose is about.
No layout reflow: the replacement occupies the same box as the dial, so nothing
below it moves.

---

## 3. The layout problem guided poses create

`yoga.js` places the accuracy dial at `ACCURACY_X = 1150` with the comment that
it sits "right of the widest pose in the library — Half Moon stops around
x=970". **A coach lying down is wider than Half Moon**, and 54 of the catalog's
poses are floor poses.

Since the dial is replaced rather than merely blanked, the replacement panel
inherits the same collision. Two things follow for the implementation:

1. Floor-pose artwork must be composed **centred on the stage** (see
   `mirroring.md`, which needs the same thing for the mirror to work), so the
   figure's extent is predictable.
2. The guided panel should sit **top-right or bottom-centre** rather than at
   `ACCURACY_X`, clear of a horizontal figure. Worth deciding with a real floor
   still on screen rather than from the geometry alone.

---

## 4. What must not happen

- **Do not fake a score.** A guided pose has no accuracy; showing a plausible
  number would be worse than showing none.
- **Do not count guided poses toward the class score.** The end-of-class summary
  should say what it measured — "scored on 13 of 30 poses" — rather than
  averaging over poses it could not see.
- **Do not drop a pose from a class to raise its scored percentage.** The
  curriculum is for wellness, mobility and relaxation first; scoring is
  secondary, and a class of standing poses chosen to be measurable is a worse
  class.
- **Do not let the hold clock stop.** A guided pose runs on the timer, because
  there is no "found the pose" event to gate it on. That is already how
  `hold_left` behaves for an untracked frame, so the mechanism exists.

---

## 5. Engine changes this implies

Recorded here, not implemented — production code is untouched until the
curriculum is frozen.

| Change | Where |
|---|---|
| `score_enabled` per step, read from the catalog | `lesson.py`, `game.py` |
| Skip `assess()` and hold the accuracy at zero-untracked for guided steps | `game.py:_evaluate` |
| Hold clock counts on wall time when `score_enabled` is false | `game.py:496-508` |
| Replacement panel, per band, cross-faded on `preview` | `yoga.js` |
| `Guided Focus` badge from `scored_steps / pose_count` | course select |
| Summary reports scored-step count, not an average over everything | `game.py:_score_pose`, summary |
