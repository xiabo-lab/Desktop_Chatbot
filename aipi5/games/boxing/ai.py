"""Predictable, configurable state-machine opponent AI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import random

from aipi5.games.boxing.config import DIFFICULTIES


class AIState(str, Enum):
    OBSERVE = "observe"
    GUARD = "guard"
    APPROACH = "approach"
    ATTACK = "attack"
    RECOVER = "recover"
    DEFEND = "defend"
    COUNTER = "counter"


@dataclass
class Attack:
    kind: str
    side: str
    target: str
    started_at: float
    impact_at: float
    recover_at: float
    damage: int
    parry_window: float
    impacted: bool = False

    def snapshot(self, now: float) -> dict:
        windup = max(0.001, self.impact_at - self.started_at)
        windup_progress = max(0.0, min(1.0,
            (now - self.started_at) / windup))
        recovery = max(0.001, self.recover_at - self.impact_at)
        if now <= self.impact_at:
            phase = "windup"
            extension = windup_progress
        else:
            phase = "recover"
            extension = max(0.0, min(1.0,
                1.0 - (now - self.impact_at) / recovery))
        # Canvas-space path used by the debug overlay and opponent animation.
        start = (0.48 if self.side == "left" else 0.52, 0.32)
        end = ((0.43 if self.side == "left" else 0.57),
               0.27 if self.target == "head" else 0.55)
        return {
            "kind": self.kind, "side": self.side, "target": self.target,
            "damage": self.damage,
            "impact_in": round(self.impact_at - now, 3),
            "parry_window": self.parry_window,
            "phase": phase,
            "progress": round(windup_progress, 3),
            "extension": round(extension, 3),
            "trajectory": [*start, *end],
        }


class OpponentAI:
    """A small state machine that learns broad player tendencies slowly."""

    def __init__(self, difficulty: str = "normal", seed: int = 7):
        self.random = random.Random(seed)
        self.difficulty = "normal"
        self.params = DIFFICULTIES["normal"]
        self.configure(difficulty)
        self.reset(0.0)

    def configure(self, difficulty: str) -> None:
        if difficulty not in DIFFICULTIES:
            raise ValueError(f"unknown difficulty {difficulty!r}")
        self.difficulty = difficulty
        self.params = DIFFICULTIES[difficulty]

    def reset(self, now: float) -> None:
        self.state = AIState.OBSERVE
        self.active: Attack | None = None
        self.next_action_at = now + self.random.uniform(*self.params["attack_interval"])
        self.guard = "none"
        self.guard_until = 0.0
        self.sequence_left = 0
        self.lane = 0
        self.history = {"head": 0, "body": 0, "left": 0, "right": 0}

    def note_player_attack(self, side: str, target: str) -> None:
        if side in ("left", "right"):
            self.history[side] += 1
        if target in ("head", "body"):
            self.history[target] += 1

    def _adapted_guard(self) -> str:
        total_target = self.history["head"] + self.history["body"]
        if total_target < 3:
            return self.random.choice(("head", "body"))
        adaptation = self.params["adaptation"]
        head_share = self.history["head"] / total_target
        # Blend learned tendency with an even split; never become perfect.
        learned = 0.5 + (head_share - 0.5) * adaptation
        return "head" if self.random.random() < learned else "body"

    def defend(self, side: str, target: str, now: float) -> str:
        """Choose the response to a player punch: block, dodge, or none."""
        self.note_player_attack(side, target)
        if now < self.guard_until and self.guard == target:
            return "block"
        chance = self.params["guard_chance"]
        side_total = self.history["left"] + self.history["right"]
        if side_total >= 4:
            repeated = self.history.get(side, 0) / side_total
            chance += max(0.0, repeated - 0.55) * self.params["adaptation"] * 0.35
        if self.random.random() >= min(0.72, chance):
            return "none"
        if self.random.random() < 0.22:
            self.state = AIState.DEFEND
            self.guard_until = now + 0.42
            self.guard = "dodge"
            # Move out of the incoming hand's line. The renderer eases this
            # discrete lane change, so it reads as a step rather than a jump.
            self.lane = 1 if side == "left" else -1
            return "dodge"
        self.state = AIState.GUARD
        self.guard = self._adapted_guard()
        self.guard_until = now + 0.62
        return "block" if self.guard == target else "none"

    def _begin_attack(self, now: float) -> Attack:
        side = self.random.choice(("left", "right"))
        # Approach from an outside lane, then return through centre on the
        # next attack. This keeps movement predictable while using all three
        # positions instead of pinning the opponent to screen centre.
        self.lane = (-1 if side == "left" else 1) if self.lane == 0 else 0
        target = "body" if self.random.random() < 0.34 else "head"
        hook = self.random.random() < 0.30
        kind = f"{side}_{'hook' if hook else ('jab' if side == 'left' else 'cross')}"
        windup = self.random.uniform(*self.params["windup"])
        damage = 5 if target == "body" else 10
        if hook:
            damage += 2
            windup *= 1.12
        attack = Attack(kind, side, target, now, now + windup,
                        now + windup + 0.44, damage,
                        float(self.params["parry_window"]))
        self.active = attack
        self.state = AIState.ATTACK
        if not self.sequence_left and self.random.random() < self.params["combo_chance"]:
            self.sequence_left = 2 if self.random.random() < 0.28 else 1
        return attack

    def tick(self, now: float, *, allow_attack: bool = True) -> list[dict]:
        """Advance the state machine and return one-shot AI events."""
        events: list[dict] = []
        if self.active is not None:
            if not self.active.impacted and now >= self.active.impact_at:
                self.active.impacted = True
                events.append({"name": "opponent-impact", "attack": self.active})
                self.state = AIState.RECOVER
            if now >= self.active.recover_at:
                self.active = None
                if self.sequence_left and allow_attack:
                    self.sequence_left -= 1
                    self.next_action_at = now + 0.20
                    self.state = AIState.COUNTER
                else:
                    self.next_action_at = now + self.random.uniform(
                        *self.params["attack_interval"])
                    self.state = AIState.OBSERVE

        if now >= self.guard_until and self.active is None:
            self.guard = "none"
            if self.state in (AIState.GUARD, AIState.DEFEND):
                self.state = AIState.OBSERVE

        if allow_attack and self.active is None and now >= self.next_action_at:
            attack = self._begin_attack(now)
            events.append({"name": "opponent-attack", "attack": attack})
        return events

    def incoming(self, now: float) -> dict | None:
        if self.active is None or self.active.impacted:
            return None
        return self.active.snapshot(now)

    def snapshot(self, now: float) -> dict:
        data = {
            "state": self.state.value,
            "guard": self.guard if now < self.guard_until else "none",
            "lane": self.lane,
            "difficulty": self.difficulty,
            "adaptation": dict(self.history),
        }
        if self.active is not None:
            data["attack"] = self.active.snapshot(now)
        return data
