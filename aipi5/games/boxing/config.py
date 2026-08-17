"""Tunable boxing rules.

Distances and speeds are measured in shoulder widths, so one set of values
works for players standing at different distances from the camera.  Keeping
the values here makes on-device tuning possible without touching combat code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MotionTuning:
    confidence: float = 0.35
    punch_speed: float = 1.45
    punch_travel: float = 0.065
    punch_extension: float = 0.62
    punch_extension_gain: float = 0.075
    punch_cooldown_s: float = 0.32
    hook_horizontal_ratio: float = 1.45
    guard_radius: float = 0.72
    dodge_distance: float = 0.24
    duck_distance: float = 0.24
    lean_scale_drop: float = 0.13
    parry_speed: float = 1.05
    parry_window_s: float = 0.24
    parry_radius: float = 0.82


@dataclass(frozen=True)
class CombatTuning:
    training_seconds: float = 90.0
    fight_seconds: float = 180.0
    countdown_seconds: float = 4.0
    # Player damage is deliberately lower than opponent damage.  Pose input can
    # recognise two or three clean punches per second, so arcade-sized 10-point
    # head shots against 100 HP made a fight end before the AI's first combo.
    body_damage: int = 1
    head_damage: int = 2
    block_damage_ratio: float = 0.2
    counter_real_seconds: float = 3.0
    slow_motion_scale: float = 0.1
    combo_window_s: float = 0.85


DIFFICULTIES = {
    "easy": {
        "opponent_hp": 140,
        "attack_interval": (2.2, 3.2), "windup": (0.9, 1.25),
        "parry_window": 0.34, "guard_chance": 0.16,
        "counter_chance": 0.05, "combo_chance": 0.08,
        "adaptation": 0.25,
    },
    "normal": {
        "opponent_hp": 220,
        "attack_interval": (1.45, 2.35), "windup": (0.62, 0.96),
        "parry_window": 0.24, "guard_chance": 0.28,
        "counter_chance": 0.14, "combo_chance": 0.22,
        "adaptation": 0.55,
    },
    "hard": {
        "opponent_hp": 300,
        "attack_interval": (0.9, 1.7), "windup": (0.42, 0.70),
        "parry_window": 0.16, "guard_chance": 0.40,
        "counter_chance": 0.26, "combo_chance": 0.38,
        "adaptation": 0.85,
    },
}


MOTION = MotionTuning()
COMBAT = CombatTuning()
