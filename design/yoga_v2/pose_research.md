# Yoga Coach anatomical research log

This is the evidence ledger for the owner-review queue. Each batch is checked
against normal-person alignment guidance, the current 3D render at four
azimuths, the arithmetic anatomy checker, and the complete test suite before
it is exposed on the Pi. Owner-approved poses are immutable production data.

## Poses 6–10 — pre-review complete

| # | Pose | Reference alignment | Comparison and production action |
|---:|---|---|---|
| 6 | Cactus Arms | Elbows at shoulder height, forearms at 90°, palms forward. [Tummee](https://www.tummee.com/yoga-poses/easy-pose-cactus-arms) | Bone shape matched, but the scoring-table fallback could not control wrist roll. Promoted to an authored 3D pose and added shared `palm_forward` hand orientation. |
| 7 | Cat | Hips over knees, hands shoulder-width and slightly ahead of shoulders, straight arms, rounded spine, relaxed neck. [Yoga Journal](https://www.yogajournal.com/poses/cat-pose/) | Torso and limb placement matched. Added explicit dorsum-to-floor foot contact so the feet no longer inherit arbitrary shin roll. |
| 8 | Chair | Parallel grounded feet, weight balanced, hips back, knees tracking over ankles, long spine, arms overhead without lumbar overarch. [Yoga Journal](https://www.yogajournal.com/poses/types/strength/master-chair-pose-4-steps/) | Four-angle render matched: feet remain grounded, knees track symmetrically, hips sit back, spine stays long, and arms clear the head. No pose-specific change required. |
| 9 | Child's Pose | Hips toward heels, forward fold with forehead supported, arms extended or resting, and relaxed plantar-flexed ankles. [Cleveland Clinic](https://health.clevelandclinic.org/childs-pose) | Fold, knee contact, forehead and palm support matched. Replaced generic relaxed ankle inheritance with explicit tops-of-feet-to-floor orientation. |
| 10 | Cobra | Pelvis and legs grounded, chest lifts forward/up, elbows stay near ribs, shoulders stay down, and tops of feet press into the floor. [Yoga Journal](https://www.yogajournal.com/practice/beginners/how-to/cobra-stretch/) | Pelvis, thighs, palms and elbow path matched. Added the same explicit tops-of-feet-to-floor orientation used for kneeling poses. |

### Shared constraints added in this batch

- `top_down`: points the toes along the shin while rotating the ankle so the
  dorsum contacts the mat and the sole faces upward.
- `palm_forward`: keeps the fingers vertical while rotating Cactus palms to
  face forward independently of forearm roll.
- Both constraints are avatar-relative terminal frames. They do not move the
  wrist or ankle and therefore cannot tear a hand or foot away from its limb.

## Poses 11–20 — pre-review complete

| # | Pose | Reference alignment | Comparison and production action |
|---:|---|---|---|
| 11 | Cow | Hips over knees, wrists/shoulders aligned, arms straight, belly lowers while chest and tail lift, neck remains long. [Yoga Journal](https://www.yogajournal.com/poses/cow-pose/) | Torso and support geometry matched. Added `top_down` to both feet so the ankle no longer leaves upright soles behind the knees. |
| 12–13 | Crescent Moon L/R | Both legs ground evenly, lower body stays stable, arms reach overhead, head stays between arms, and the torso lengthens rather than collapsing into the side bend. [Yoga Journal](https://www.yogajournal.com/practice/yoga-sequences/blueprint-for-change) | Both scored tables form clean mirrored 25° side bends with planted feet and long, parallel arms. No table change. |
| 14 | Downward-Facing Dog | Hands shoulder-width, feet parallel, straight active arms, long spine, hips back/up; knees may soften and heels need not touch for a normal beginner. [Yoga Journal](https://www.yogajournal.com/poses/downward-facing-dog/) | Four-view render has a long inverted V, straight elbows/knees, grounded palms and believable flat feet. No pose change. |
| 15 | Easy Seat | Cross at mid-shins, ground both sitting bones, lengthen the spine, keep chin level, and rest hands on knees. [Yoga Journal](https://www.yogajournal.com/meditation/your-best-meditation-seat/) | Crossed-leg geometry and upright spine matched. Added independent palm-down orientation. |
| 16 | Seated Breathing | Same neutral cross-legged seat, with an alert upright spine and relaxed hands. [Yoga Journal](https://www.yogajournal.com/practice/yoga-for-stability/) | Geometry matched. Added independent palm-up orientation to distinguish the breathing/rest variation without changing the arms. |
| 17–18 | Reclined Figure Four L/R | Lie with head and back supported; cross one ankle over the opposite thigh, loop hands behind the supporting thigh and draw it inward without shifting the pelvis. [Hospital for Special Surgery](https://www.hss.edu/health-library/move-better/yoga-cool-down-stretches) | Legs already formed a safe mirrored figure four. Re-authored both arm chains so hands reach behind the supporting thigh, and flexed the crossed ankle instead of leaving it relaxed. |
| 19 | Standing Forward Fold | Bend the knees as needed, hinge from the hips rather than forcing lumbar flexion, lengthen then release the torso and neck. [Yoga Journal](https://www.yogajournal.com/poses/standing-forward-bend-2/) | Scored table keeps both feet planted, straight legs with soft-knee allowance, hip hinge, hanging head and relaxed arms. No change, per the standing constraint to preserve this scored pose. |
| 20 | Gentle Back Reach | Stand with parallel hip-width feet, support the lower back with the hands, and create a small upper-body/chest lift rather than a deep lumbar bend. [Yoga Sequencing](https://www.yoga-sequencing.com/poses/standing-backbend-hands-on-back) | The old frontal table produced a side lean and floating arms. Re-authored it in the sagittal plane as a 14° backbend, with elbows flexed and palms facing the lower back. |

### Shared constraints added in this batch

- `palm_down` and `palm_up` control seated-hand roll independently of the
  forearm while preserving the finger direction.
- `palm_to_back` points the fingers down and the palms into the lumbar support
  surface for hands-assisted backbends.
- Reclined Figure Four now has a researched contract for crossed/supporting
  knee flexion and requires the crossed ankle to be flexed.
- Runtime, stage reviewer, and contact checker request `rigdata.json` with
  `cache: no-store`, preventing an older pose library from surviving a rebuild.

## Poses 21–25 — pre-review complete

| # | Pose | Reference alignment | Comparison and production action |
|---:|---|---|---|
| 21 | Goddess | Wide stance with toes turned out, knees tracking in the same direction and stacking over ankles, upright neutral spine, upper arms level and elbows near 90°. [Yoga Collective](https://www.theyogacollective.com/poses/goddess-pose-utkata-konasana/) | The scored joint table already produced the correct symmetric squat and cactus arms. Added independent 45° toe turnout to both planted feet; the old generic floor frame made the knees turn out while the toes remained forward. |
| 22 | Half Lift | Hips over ankles, weight toward the forefeet, torso lengthened forward from the hips, shoulder blades broad, neck continuing the spine, hands supported on shins or blocks. [NC State Yoga](https://yoga.dasa.ncsu.edu/asana/standing-poses/) | The old frontal definition rendered as a side bend. Re-authored it in the sagittal plane with a nearly horizontal long back, neutral neck, straight legs and both hands reaching toward the shins. |
| 23–24 | Half Moon L/R | Standing leg strong, torso and raised leg approximately parallel to the floor, hips and shoulders stacked, lower hand supported and upper arm vertical, lifted foot flexed. [Human Kinetics](https://us.humankinetics.com/blogs/excerpt/ardha-chandrasana) | Both scored tables already form clean mirrored balance shapes with straight supporting and lifted legs. Changed the lifted ankle from pointed to flexed so the heel reaches away and the sole stays vertical. |
| 25 | Half Split Left | Hips stack over the back knee, front leg lengthens with heel down and toes flexed upward, back shin and top of foot rest on the mat, hands support beneath the shoulders, and the spine stays long. [Gaia](https://www.gaia.com/article/ardha-hanumanasana-half-front-splits-pose) | Hip, knee, hand and long-spine geometry matched. Corrected the terminal rules: the front foot is flexed on its heel and the kneeling-side dorsum rests on the mat. The same shared correction was applied to the future right-side partner. |

### Shared constraints added in this batch

- Standing `floor` orientation can receive an independent toe-turnout offset,
  allowing Goddess knees and toes to track together without changing leg bones.
- Half Moon now requires a planted supporting sole and flexed lifted ankle.
- Half Split now distinguishes front-heel support from back-foot dorsum contact.
- The anatomy checker now enforces these contracts plus Half Lift's sagittal
  hip hinge; reviewer edits can still refine the rendered mesh before approval.

## Poses 26–30 — pre-review complete

| # | Pose | Reference alignment | Comparison and production action |
|---:|---|---|---|
| 26 | Half Split Right | Mirror of the approved left pose: hips over the back knee, straight active front leg, front heel grounded with toes flexed upward, and back-foot dorsum resting on the mat. [YogaUP teacher-training manual](https://yogaup.com.hk/wp-content/uploads/2020/06/200HR-Virtual-Teacher-Training-Manual.pdf) | Applied the shared Half Split terminal contract to the right-side partner: right front foot flexed, left back foot dorsum-down. Existing hip, knee, hand and long-spine geometry matched. |
| 27–28 | Standing Hand-to-Toe L/R | Stand upright on one strong leg; lift the other straight leg, keep the raised foot active, and grasp it with the same-side hand. The free hand can remain at the hip. [Yoga Education standing-balance guide](https://yogaeducation.org/wp-content/uploads/2024/09/Teaching-standing-balancing-postures-a.pdf) | The scored fallback showed both arms in a T and did not depict a toe hold. Added mirrored 3D demonstrations with a reachable raised leg, same-side hand-to-foot contact, flexed raised ankle and free hand folded toward the hip. |
| 29 | Happy Baby | Rest head, shoulders, spine and sacrum on the mat; draw knees outside the torso, stack ankles over knees with shins approximately perpendicular to the floor, flex the feet, and hold the outer feet without forcing. [Yoga & You](https://www.youtube.com/watch?v=Ppku7i3ypGM) | The former pose kept both legs in one flat plane and left the ankles relaxed. Added mirrored out-of-plane hip/arm depth so the knees open beside the ribs and changed both feet to flexed. |
| 30 | High Lunge Left | Front knee stacks above the ankle, back leg stays long and straight, rear heel remains lifted on the ball of the foot, torso rises and arms reach overhead. [YogaRenew](https://www.yogarenewteachertraining.com/yoga-poses/crescent-lunge/) | Torso, arms and knee geometry matched. Replaced the generic flat rear sole with a pointed continuation of the back shin so the toe ball supports the raised heel. The shared rule also prepares the future right-side partner. |

### Shared constraints added in this batch

- Hand-to-Toe now validates upright posture, straight supporting/raised knees,
  same-side hand-to-foot proximity, planted support and a flexed raised ankle.
- Happy Baby requires bilateral outward depth, controlled knee folding and
  flexed feet instead of a single flat leg plane.
- High Lunge distinguishes a planted front sole from a toe-ball-supported rear
  foot with a lifted heel; both legs retain their independent knee limits.

## Poses 31–91 — pre-review anatomical pass

Every remaining pose was rendered at 0°, 45°, 90° and 135°. Mirrored poses
share one alignment contract but were checked as separate meshes. The sources
below describe the ordinary, teachable form rather than an end-range variation.

| # | Pose(s) | Researched normal-person alignment | Comparison and production action |
|---:|---|---|---|
| 31 | High Lunge R | Front knee over ankle; back leg straight with heel lifted and ball of foot active. [Yoga Journal](https://www.yogajournal.com/poses/high-lunge-adaptations/) | Geometry mirrors approved #30. Kept front sole planted and rear foot pointed along the shin. |
| 32 | Knees to Chest | Supine spine and head supported; knees draw toward chest and hands hold the knees/shins without lifting the shoulders. [Yoga Journal](https://www.yogajournal.com/poses/anatomy/hips/hip-parade-2/) | Old hands reached beyond the feet. Re-solved both arm chains to the knees and separated paired limbs slightly in depth. |
| 33–34 | Lizard L/R | Front knee stacks over ankle, hands stay inside the foot, back knee may lower, and the back foot relaxes dorsum-down. [Yoga Journal](https://www.yogajournal.com/practice/yoga-sequences/20-minute-sequence-finding-fulfillment/) | Support geometry passed four views. Added side-specific back-foot dorsum contact. |
| 35–38 | Low Lunge and Reach L/R | Front knee forms a right angle over heel, back knee rests down, toes point straight back; reach variants lengthen upward without compressing the neck. [Yoga Journal](https://www.yogajournal.com/video/low-lunge-cues/) | Added shared side-specific dorsum contact to the kneeling foot; kept the front sole planted. |
| 39–42 | Mountain, Breath, Neck Tilts L/R | Feet ground evenly, pelvis/ribs neutral, spine tall; neck motion remains gentle and shoulder stays down. [Yoga Journal](https://www.yogajournal.com/practice/beginners/5-steps-master-upward-salute-urdhva-hastasana/) | Neutral standing base passed; contact gestures retain declared natural-contact exemptions, not whole-limb exemptions. |
| 43–44 | Pigeon Prep L/R | Front shin angle is comfort-dependent; back leg aligns with its hip, hips stay level, and top of back foot faces floor. [Yoga Journal](https://www.yogajournal.com/poses/anatomy/hips/ways-to-practice-pigeon-pose/) | Added back-foot dorsum contact; retained intentional front-leg/pelvis proximity while rejecting unrelated intersections. |
| 45 | Plank | Wrists under shoulders, palms load evenly, body forms one line, heels press back. [Yoga Journal](https://www.yogajournal.com/poses/plank-pose/) | Existing straight-line support and palm/foot terminals passed. |
| 46–47 | Pyramid L/R | Both legs straight, hips face front, front foot forward and rear foot turns out 30–60°, torso hinges long over front leg. [Yoga Journal](https://www.yogajournal.com/practice/pyramid-pose-alignment/) | Kept long-leg hinge and added independent front/rear ankle turnout. |
| 48–49 | Reverse Warrior L/R | Front knee remains bent, front arm reaches overhead, back hand rests lightly on rear thigh without loading the knee. [Yoga Journal](https://www.yogajournal.com/practice/find-your-balance/) | Flagged arm path for authored correction; added knee/toe tracking turnout shared with Warrior II. |
| 50 | Savasana | Back and head supported, arms away from sides with palms up, legs relaxed and toes falling outward. [Yoga Journal](https://www.yogajournal.com/practice/yoga-sequences/30-minute-sequence-ease-back-pain) | Added palms-up and relaxed independent ankles. |
| 51–58 | Seated Fold/Hamstrings/Side Stretches/Twists/Shoulder Opener | Sit bones ground, spine lengthens before folding or twisting; twists distribute through spine/pelvis instead of forcing a fixed pelvis. [Yoga Journal](https://www.yogajournal.com/teach/anatomy-yoga-practice/better-way-twist/) | Flexed long-leg feet retained. Natural hand contact is allowed only for declared gestures; shoulder opener flagged for behind-body arm correction. |
| 59–60 | Side Angle L/R | Front knee near 90° over ankle, rear leg straight, lower support stays light, top arm extends in one diagonal. [Yoga Journal](https://www.yogajournal.com/practice/beginners/how-to/extended-side-angle-pose/) | Joint geometry passed; added front/rear foot turnout so knees and toes track. |
| 61–62 | Side Bend L/R | Pelvis remains centered while the torso lengthens before bending; neck follows the spine. [Yoga Journal](https://www.yogajournal.com/practice/yoga-sequences/blueprint-for-change) | Mirrored bend and overhead clearance passed. |
| 63–64 | Side Plank L/R | Supporting wrist under shoulder, shoulders/hips stacked, body straight, feet together. [Yoga Journal](https://www.yogajournal.com/practice/yoga-sequences/free-side-body-flow-fascia/) | Existing one-palm support retained; foot stack checked from four angles. |
| 65 | Sphinx | Forearms parallel, elbows under/near shoulders, pelvis and legs grounded, tops of feet down. [Yoga Journal](https://www.yogajournal.com/poses/sphinx-pose/) | Replaced pointed-but-rolled ankles with explicit dorsum contact on both feet. |
| 66 | Staff | Legs straight and active, feet flexed, torso vertical, palms support beside hips without shoulder shrug. [Yoga Journal](https://www.yogajournal.com/practice/yoga-sequences/discerning-dandasana/) | Existing geometry and flexed ankles passed. |
| 67–70 | Standing Knee and Standing Twist L/R | Raised-knee balance keeps standing leg aligned and hands support the knee; standing twist stays tall and rotates without wrenching a knee. [Yoga Journal](https://www.yogajournal.com/poses/anatomy/knees/on-your-knees/) | Raised-knee arms and twist gestures flagged for authored contact correction; support soles remain planted. |
| 71 | Star | Wide stable stance, straight knees tracking toes, arms extend without locking. [Yoga Journal](https://www.yogajournal.com/poses/five-pointed-star-pose/) | Added mild symmetric toe turnout and kept open palms clear of shoulders. |
| 72–73 | Supine Twist L/R | Shoulders stay supported, knees move together to one side, arms open in a T and gaze may turn opposite. [Yoga Journal](https://www.yogajournal.com/practice/yoga-sequences/30-minute-sequence-ease-back-pain) | Added palms-up; leg/torso depth remains queued for collision refinement. |
| 74–76 | Table and Thread the Needle L/R | Table stacks shoulders/wrists and hips/knees; Thread passes one arm beneath chest while support remains stable. [Yoga Journal](https://www.yogajournal.com/poses/tabletop-pose/) | Added dorsum-down feet. Thread keeps only its exact intentional arm-under-torso contact exempt. |
| 77–80 | Tree Heart/Overhead L/R | Standing foot roots, raised foot contacts inner calf/thigh but not the knee, pelvis stays level, knee opens only as hip permits. [Yoga Journal](https://www.yogajournal.com/poses/tree-pose/) | Retained relaxed raised ankle and planted support; contact checked from four angles. |
| 81–82 | Triangle L/R | Knees remain straight, feet/knees align, lower hand uses shin/block/floor, shoulders and arms stack. [Yoga Journal](https://www.yogajournal.com/video/extended-triangle-pose-cues/) | Added front/rear foot turnout; lower-hand reach flagged for authored correction. |
| 83 | Upward Salute | Feet ground, pelvis/ribs neutral, straight arms beside ears, palms face each other. [Yoga Journal](https://www.yogajournal.com/practice/beginners/5-steps-master-upward-salute-urdhva-hastasana/) | Body line passed; palm orientation retained for final owner review. |
| 84–85 | Warrior I L/R | Front knee tracks over ankle, hips face forward, rear leg straight, heel roots at an angle, arms reach without rib flare. [Yoga Journal](https://www.yogajournal.com/poses/warrior-i-pose/) | Added independent front/rear ankle turnout. |
| 86–87 | Warrior III L/R | Torso and lifted leg form one horizontal line, hips stay level, standing knee tracks forward. [Yoga Journal](https://www.yogajournal.com/practice/warrior-3/) | Existing balance line passed; supporting sole planted and lifted foot remains active. |
| 88–89 | Warrior II L/R | Front knee directly over ankle/toes, rear leg straight, arms parallel and palms down. [Yoga Journal](https://www.yogajournal.com/poses/warrior-ii-pose/) | Added knee-matched foot turnout; palm roll remains independent of arm direction. |
| 90 | Weight Shift | Weight transfers over one foot while knee tracks in the foot direction and trunk remains controlled. [Yoga Journal](https://www.yogajournal.com/poses/anatomy/knees/on-your-knees/) | Conservative shift passed joint limits; no end-range joint rotation introduced. |
| 91 | Wide-Leg Fold | Wide feet remain stable with toes slightly inward, weight centered, torso hinges long, hands support on floor. [Yoga Journal](https://www.yogajournal.com/practice/prasarita-padottanasana-wide-legged-forward-bend/) | Added symmetric foot alignment; camera/framing and fold geometry flagged for authored correction. |

### Shared constraints added in this pass

- Side-specific kneeling ankle roles distinguish planted sole, toe-ball support,
  relaxed/pointed foot, and dorsum-to-mat contact.
- Scored standing poses can now carry `terminal3d` ankle turnout through the
  builder, reviewer and production game without changing scoring tables.
- Supine resting/twist poses explicitly orient palms upward.
- Collision exemptions remain exact semantic contacts. A hand-to-chest or
  threaded-arm pose does not disable checks for the rest of either limb.

### Owner rejection follow-up — poses 31–35

The first pre-review pass was rejected because semantic labels and coarse
silhouettes did not prove the rendered contact. The following are now hard
acceptance rules for every unapproved pose:

- A mirrored pose inherits the approved counterpart's actual VRM terminal
  frame. High Lunge Right now mirrors #30's toe-ball shoe roll instead of
  relying on the insufficient `point` label.
- A named body contact has a landmark-distance contract. Knees-to-Chest now
  requires each hand to reach its knee and both palms to face inward; Low
  Lunge requires both hands to reach the front-thigh region.
- Weight-bearing hands receive the owner-calibrated ±30° wrist-roll frame;
  standing soles receive the calibrated 30° sole frame unless a pose declares
  a more specific turnout.
- Paired limbs cannot remain coincident in one sagittal plane. Lizard arms are
  separated in depth before palm contact is solved.
- A new depth-aware capsule pass rejects hand, forearm, knee and opposite-leg
  centrelines entering torso, pelvis or limb cores. Exact natural contact is
  exempted only for the named limb/pair, never for the whole pose.
- All unapproved poses 31–91 were rebuilt and re-rendered after these shared
  changes. The new collision gate also caught and corrected both Standing
  Knee poses before they could enter a later owner batch.

## Owner review results — poses 31–91

The 23 August owner export approved 42 poses and rejected 17. Sphinx (#65) and
Staff (#66) had no recorded decision, so neither is included in the new
fixed-only review queue. Approved editor values for Pigeon Prep, Plank,
Reverse Warrior, Side Plank and Warrior III are immutable production overlays.

The rejected families were corrected using the researched contracts above:

- Low Lunge Right and Mountain Breath separate coincident arms in depth.
- Both Seated Hamstrings keep the folded leg visible. Both Seated Side
  Stretches and both Seated Twists inherit the approved Easy Seat leg base.
- Shoulder Opener routes straight arms behind the torso without disabling
  forearm-to-body collision checks.
- Both Standing Knee poses now raise one knee and bring two palms to that knee
  in front of the torso.
- Both Supine Twists keep the shoulders down, open the arms in a T, and move
  two folded knees together to the named side.
- The four Tree variants place the raised foot at the inner thigh, outside the
  standing-leg volume. A shared `sole_to_leg` rule turns the sole inward while
  keeping the toes downward; it is distinct from Butterfly's `sole_in` rule.

The next owner review intentionally contains only these 17 changed poses. It
uses a new local-storage namespace so rejected manual edits from the completed
31–91 pass cannot shadow the corrected base library.

## Second correction pass — six remaining poses

The Pi export approved 11 of the 17 corrections and rejected only #52–53,
#67–68 and #72–73. Eight-view sweeps at 0°, 45°, 90°, 135°, 180°, 225°, 270°
and 315° exposed collisions that the earlier landmark-only checks missed.

- Seated Hamstring now gives the folded thigh its own lateral plane; its
  mid-thigh centre is more than 49 cm from the extended thigh centre instead
  of approximately 33 cm in the rejected flat definition.
- Standing Knee places the knee forward of the abdominal volume. Both hands
  remain within 9 cm of the raised knee, while forearm midpoints must remain
  outside the raised thigh and shin cores.
- Supine Twist gives the two bent thighs a measured capsule clearance while
  keeping the shins low. Flattening the legs again is now a test failure.
- The old broad Standing Knee contact exemptions were removed. Out-of-plane
  chains bypass only the obsolete 2D projection and must pass the 3D torso,
  thigh and forearm checks.

### Six-pose approval and head-review restart

The owner approved all six remaining corrections in
`yoga-pose-review (3).json`. The exact Seated Hamstring, Standing Knee and
Supine Twist objects are production overlays; the two twists include deliberate
neck-axis face turns of −80° and +80°. The next review restarts at #1 and
contains all 91 poses solely so head direction can be checked consistently.
It uses fresh review and edit storage, while all previously approved body
geometry remains the immutable starting point.

### Final 91-pose approval

The completed head-review export contains 91 approvals, no rejected or missing
statuses, and 27 exact edited pose objects. The compact production overlay is
byte-for-byte equivalent to the export's `pose_edits` data. It includes the
owner's head bends/turns for Bird Dog, Cat, both Lizards, Low Lunge Left,
Seated/Standing Twists, Sphinx and Table, plus every body or terminal refinement
made during the same pass. All 91 catalog IDs are now protected approvals.
