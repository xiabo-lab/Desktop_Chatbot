"""Health and stylised, body-attached injury state."""

from __future__ import annotations

from dataclasses import dataclass, field


DAMAGE_ZONES = ("left_eye", "right_eye", "left_shoulder", "right_shoulder")
FACE_ZONES = {"left_eye", "right_eye"}


@dataclass
class FighterDamage:
    max_hp: int = 100
    hp: int = 100
    display_hp: float = 100.0
    delayed_hp: float = 100.0
    injuries: dict[str, int] = field(default_factory=dict)

    def reset(self, max_hp: int | None = None) -> None:
        if max_hp is not None:
            self.max_hp = max(1, int(max_hp))
        self.hp = self.max_hp
        self.display_hp = float(self.max_hp)
        self.delayed_hp = float(self.max_hp)
        self.injuries.clear()

    def hit(self, damage: int, zone: str) -> int:
        before = self.hp
        self.hp = max(0, self.hp - max(0, int(damage)))
        dealt = before - self.hp
        # Injury progression follows clean impacts, not the HP tuning value.
        # Player punches are intentionally only 1–2 HP so a fight lasts long
        # enough for the AI to answer; requiring four HP here meant the
        # opponent could never bruise at all.
        if dealt > 0 and zone in DAMAGE_ZONES:
            self.injuries[zone] = min(3, self.injuries.get(zone, 0) + 1)
        return dealt

    def animate(self, dt: float) -> None:
        # Two rates create the familiar bright bar plus delayed damage trail.
        self.display_hp += min(1.0, dt * 10.0) * (self.hp - self.display_hp)
        self.delayed_hp += min(1.0, dt * 2.2) * (self.hp - self.delayed_hp)

    def snapshot(self) -> dict:
        matrix = {zone: self.injuries.get(zone, 0) for zone in DAMAGE_ZONES}
        return {
            "hp": self.hp,
            "max_hp": self.max_hp,
            "display_hp": round(self.display_hp, 1),
            "delayed_hp": round(self.delayed_hp, 1),
            "damage_matrix": matrix,
            "damage_key": "".join(str(matrix[zone]) for zone in DAMAGE_ZONES),
            "injuries": [
                {"zone": zone, "level": level,
                 "kind": "face" if zone in FACE_ZONES else "body",
                 "stage": ("bruise" if level == 1 else
                           "bruise-swelling" if level == 2 else
                           "bruise-strong-swelling-bleeding"),
                 "swelling": 0 if level == 1 else 1 if level == 2 else 2,
                 "bleeding": level >= 3}
                for zone, level in sorted(self.injuries.items())
                if zone in DAMAGE_ZONES
            ],
        }
