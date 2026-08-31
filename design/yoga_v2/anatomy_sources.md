# Yoga Coach anatomy research ledger

The rig uses ordinary healthy-adult motion, not maximum contortion. Absolute
range of motion is only an outer safety envelope; every pose also has a
narrower, researched movement contract in `check3d.py`.

## Approval batch 1: poses 1–5

### Bird Dog — left and right

- Keep the lumbar spine neutral, the pelvis level, and the reaching arm and
  opposite leg in line with the trunk. The supporting hip and knee remain
  square rather than rotating open.
- Runtime contract: reaching knee 0–10° flexion, supporting knee 80–100°,
  head within 30° of the spine, both soles oriented downward, no out-of-plane
  limb rotation.
- Evidence: Losavio et al., *Sports* (2023), describe neutral spine, level
  hips, correct knee alignment, and an in-line arm/trunk/leg endpoint:
  https://pmc.ncbi.nlm.nih.gov/articles/PMC10305076/

### Boat Preparation

- Owner-finalized on 21 August 2026 with a stronger, almost straight-leg
  shape: upright chest, symmetric legs, and both feet flexed upward. The
  production object is preserved verbatim in `approved_pose_edits.json`.

### Bridge

- Gravity is carried through the upper back/shoulders, arms and planted feet.
  Knees remain parallel; the common researched bridge positions use 60°, 90°
  and 120° knee flexion rather than a heel-to-seat fold.
- Runtime contract: both knees 90–125°, head/shoulders/elbows/hands and both
  feet at the floor, feet flat with toes pointing away from the head.
- Evidence: Takeshita et al. used motion capture and floor-reaction modelling
  at 60°, 90° and 120° knee flexion:
  https://pmc.ncbi.nlm.nih.gov/articles/PMC9168199/

### Butterfly / Bound Angle

- The motion comes from combined hip flexion, abduction and external rotation;
  the knee flexes in its hinge plane. The feet meet in front of the pelvis.
  The knees are never forced down by twisting the knee joint.
- Runtime contract: 125–165° knee flexion, feet within 0.20 spine units of
  centre and at least 0.20 units anterior to the pelvis, soles facing inward.
  The authored thighs travel outward and forward; the shins return inward and
  back, which prevents the old feet-inside-torso intersection.
- Healthy-adult context: published hip examination norms average about 128°
  flexion, 33° abduction, and 44° external rotation. Combined motion must stay
  conservative because these single-plane maxima are not additive:
  https://pmc.ncbi.nlm.nih.gov/articles/PMC8167346/

## General joint model

- Hip reference envelope: flexion 120–140°, extension 17–30°, abduction
  33–45°, external/internal rotation about 42–45° depending on protocol:
  https://pmc.ncbi.nlm.nih.gov/articles/PMC8167346/ and
  https://pmc.ncbi.nlm.nih.gov/articles/PMC3589629/
- The knee is not a frictionless ball joint. Normal flexion couples only small
  secondary motion—reported dynamic coupling up to roughly 10° abduction and
  15° internal rotation—so the coach treats it as a one-way flexion hinge and
  reserves secondary rotation for small, researched cases:
  https://pmc.ncbi.nlm.nih.gov/articles/PMC5405570/
- Joint limits vary with age, sex and measurement method. They are safety
  ceilings, not pose targets. Each future approval batch must add its own pose
  contract and source here before it is unlocked for owner review.
