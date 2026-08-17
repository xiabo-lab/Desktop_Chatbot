"""Deterministic boxing training and fight simulation.

Camera capture and pose inference deliberately do not live here.  The manager
calls :meth:`tick_pose` once for each shared pose snapshot, just as it calls
Fruit Ninja's ``tick`` on that same thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import random

from aipi5.games.boxing.ai import AIState, OpponentAI
from aipi5.games.boxing.config import COMBAT, DIFFICULTIES, CombatTuning
from aipi5.games.boxing.damage import FighterDamage
from aipi5.games.boxing.motion import BoxingAction, BoxingMotionAnalyzer, MotionResult
from aipi5.games.fruit_ninja.game import State


PUNCH_ACTIONS = {"left_punch", "right_punch", "left_hook", "right_hook"}
DODGE_ACTIONS = {"dodge_left", "dodge_right", "duck", "lean_back"}
MODES = {"training", "fight"}
DIFFICULTY_NAMES = {"easy", "normal", "hard"}


@dataclass
class TrainingPrompt:
    kind: str
    target: str
    created_at: float
    deadline: float
    stage: int

    def snapshot(self, now: float) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "stage": self.stage,
            "time_left": round(max(0.0, self.deadline - now), 2),
            "progress": round(max(0.0, min(1.0,
                (now - self.created_at) / max(0.001, self.deadline - self.created_at))), 3),
        }


@dataclass
class BoxingSession:
    """One boxing visit, including mode selection, round, and result."""

    duration: float = COMBAT.training_seconds
    time_left: float = COMBAT.training_seconds
    score: int = 0
    best: int = 0
    state: State = State.READY
    mode: str = ""
    difficulty: str = "normal"
    tuning: CombatTuning = COMBAT
    events: list[dict] = field(default_factory=list)

    started_at: float = 0.0
    ended_at: float = 0.0
    _last_tick: float = -1.0
    _paused_at: float = 0.0
    _world_time: float = 0.0
    _countdown_until: float = 0.0
    _round_started: bool = False
    _counter_until_real: float = 0.0
    _slow_motion: bool = False
    _next_prompt_at: float = 0.0
    _prompt: TrainingPrompt | None = None
    _last_hit_at: float = -1e9
    _last_result: MotionResult = field(default_factory=MotionResult)
    _rng: random.Random = field(default_factory=lambda: random.Random(11))

    analyzer: BoxingMotionAnalyzer = field(default_factory=BoxingMotionAnalyzer)
    opponent_ai: OpponentAI = field(default_factory=OpponentAI)
    opponent_damage: FighterDamage = field(default_factory=FighterDamage)
    player_damage: FighterDamage = field(default_factory=FighterDamage)

    punches_thrown: int = 0
    successful_hits: int = 0
    missed_punches: int = 0
    successful_dodges: int = 0
    successful_blocks: int = 0
    successful_parries: int = 0
    combo: int = 0
    best_combo: int = 0
    reaction_total_s: float = 0.0
    reactions: int = 0
    best_reaction_s: float | None = None
    result: str = ""

    def __post_init__(self) -> None:
        self.opponent_ai.configure(self.difficulty)

    @property
    def startable(self) -> bool:
        return self.mode in MODES

    @property
    def gesture_phase(self) -> str:
        if self.state is State.READY and not self.startable:
            return "select"
        return self.state.value

    @property
    def elapsed(self) -> float:
        return max(0.0, self.duration - self.time_left)

    def select_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("choose Training or Fight Opponent")
        if self.state not in (State.READY, State.OVER):
            raise ValueError("the mode cannot change during a round")
        self.mode = mode
        self.duration = (self.tuning.training_seconds if mode == "training"
                         else self.tuning.fight_seconds)
        self.time_left = self.duration
        self.state = State.READY
        self.result = ""
        self.analyzer.reset()

    def select_difficulty(self, difficulty: str) -> None:
        if difficulty not in DIFFICULTY_NAMES:
            raise ValueError("choose Easy, Normal or Hard")
        if self.state is State.PLAYING:
            raise ValueError("difficulty cannot change during a round")
        self.difficulty = difficulty
        self.opponent_ai.configure(difficulty)

    def start(self, now: float) -> None:
        if not self.startable:
            raise ValueError("choose Training or Fight Opponent first")
        self.duration = (self.tuning.training_seconds if self.mode == "training"
                         else self.tuning.fight_seconds)
        self.time_left = self.duration
        self.score = 0
        self.state = State.PLAYING
        self.started_at = now
        self.ended_at = 0.0
        self._last_tick = now
        self._world_time = 0.0
        self._countdown_until = now + self.tuning.countdown_seconds
        self._round_started = False
        self._counter_until_real = 0.0
        self._slow_motion = False
        self._next_prompt_at = 0.0
        self._prompt = None
        self._last_hit_at = -1e9
        self._last_result = MotionResult()
        self.events.clear()
        self.analyzer.reset()
        self.opponent_ai.reset(0.0)
        self.opponent_damage.reset(DIFFICULTIES[self.difficulty]["opponent_hp"])
        self.player_damage.reset()
        self.punches_thrown = self.successful_hits = self.missed_punches = 0
        self.successful_dodges = self.successful_blocks = self.successful_parries = 0
        self.combo = self.best_combo = 0
        self.reaction_total_s = 0.0
        self.reactions = 0
        self.best_reaction_s = None
        self.result = ""
        self.events.append({"name": "boxing-ready"})

    def pause(self, now: float) -> bool:
        if self.state is not State.PLAYING:
            return False
        self.state = State.PAUSED
        self._paused_at = now
        return True

    def resume(self, now: float) -> bool:
        if self.state is not State.PAUSED:
            return False
        paused = max(0.0, now - self._paused_at)
        if not self._round_started:
            self._countdown_until += paused
        if self._counter_until_real:
            self._counter_until_real += paused
        self.state = State.PLAYING
        self._last_tick = now
        return True

    def finish(self, now: float, result: str = "") -> None:
        if self.state is State.OVER:
            return
        self.state = State.OVER
        self.ended_at = now
        self._slow_motion = False
        if result:
            self.result = result
        elif self.mode == "training":
            self.result = "training-complete"
        else:
            opponent_ratio = self.opponent_damage.hp / self.opponent_damage.max_hp
            player_ratio = self.player_damage.hp / self.player_damage.max_hp
            if math.isclose(opponent_ratio, player_ratio, abs_tol=0.005):
                self.result = "draw"
            else:
                self.result = "win" if opponent_ratio < player_ratio else "lose"
        if self.score > self.best:
            self.best = self.score
        self.events.append({"name": "boxing-over", "result": self.result})

    def _start_round_if_due(self, now: float) -> bool:
        if self._round_started:
            return True
        if now < self._countdown_until:
            return False
        self._round_started = True
        self._last_tick = now
        self.events.append({"name": "fight-bell"})
        return True

    def tick_pose(self, now: float, person, hands: dict,
                  pose_timestamp: float) -> None:
        """Consume one shared pose frame without slowing the pose pipeline."""
        if self.state is State.READY:
            self.analyzer.calibrate(person)
            return
        if self.state is not State.PLAYING:
            return
        if not self._start_round_if_due(now):
            self.analyzer.calibrate(person)
            self._last_result = self.analyzer.update(person, pose_timestamp, hands)
            return

        real_dt = max(0.0, now - self._last_tick) if self._last_tick >= 0 else 0.0
        self._last_tick = now
        real_dt = min(real_dt, 0.12)

        if self._slow_motion and now >= self._counter_until_real:
            self._slow_motion = False
            self.events.append({"name": "slow-motion-end"})
        scale = self.tuning.slow_motion_scale if self._slow_motion else 1.0
        world_dt = real_dt * scale
        self._world_time += world_dt
        self.time_left = max(0.0, self.time_left - world_dt)

        incoming = (self._training_incoming() if self.mode == "training"
                    else self.opponent_ai.incoming(self._world_time))
        self._last_result = self.analyzer.update(
            person, pose_timestamp, hands, incoming=incoming)

        self.opponent_damage.animate(real_dt)
        self.player_damage.animate(real_dt)
        self._record_punches(self._last_result)

        if self.mode == "training":
            self._tick_training(now)
        else:
            self._tick_fight(now)

        if self.time_left <= 0 and self.state is State.PLAYING:
            self.finish(now)

    def _record_punches(self, motion: MotionResult) -> None:
        self.punches_thrown += sum(action.name in PUNCH_ACTIONS
                                   for action in motion.actions)

    def _training_stage(self) -> int:
        return 1 if self.elapsed < 30 else 2 if self.elapsed < 60 else 3

    def _new_prompt(self) -> None:
        stage = self._training_stage()
        choices = ["left_punch", "right_punch", "dodge_left", "dodge_right"]
        if stage >= 2:
            choices += ["left_punch", "right_punch", "block", "parry", "duck"]
        if stage >= 3:
            choices += ["left_hook", "right_hook", "parry", "lean_back",
                        "dodge_left", "dodge_right"]
        kind = self._rng.choice(choices)
        target = (self._rng.choice(("head", "body"))
                  if "punch" in kind or "hook" in kind else "")
        window = {1: 3.4, 2: 2.35, 3: 1.55}[stage]
        self._prompt = TrainingPrompt(kind, target, self._world_time,
                                      self._world_time + window, stage)
        self.events.append({"name": "training-prompt", "kind": kind,
                            "target": target, "stage": stage})

    def _training_incoming(self) -> dict | None:
        if self._prompt is None or self._prompt.kind not in ("parry", "block"):
            return None
        return {
            "kind": "right_cross", "side": "right", "target": "head",
            "impact_in": self._prompt.deadline - self._world_time,
            "parry_window": 0.42 if self._prompt.stage == 1 else 0.30,
            "trajectory": [0.52, 0.32, 0.56, 0.27],
        }

    @staticmethod
    def _prompt_matches(prompt: TrainingPrompt, motion: MotionResult) -> bool:
        names = motion.names
        if prompt.kind == "block":
            return motion.guard != "none"
        if prompt.kind in ("left_punch", "right_punch"):
            side = prompt.kind.split("_", 1)[0]
            return any(action.side == side and action.name in PUNCH_ACTIONS and
                       (not prompt.target or action.target == prompt.target)
                       for action in motion.actions)
        return prompt.kind in names

    def _tick_training(self, now: float) -> None:
        prompt = self._prompt
        if prompt is not None and self._prompt_matches(prompt, self._last_result):
            reaction = max(0.0, self._world_time - prompt.created_at)
            base = {1: 100, 2: 150, 3: 220}[prompt.stage]
            speed_bonus = round(max(0.0, prompt.deadline - self._world_time) * 30)
            self.combo += 1
            multiplier = 1 + min(3, self.combo // 3) * (0.25 if prompt.stage >= 2 else 0.1)
            points = round((base + speed_bonus) * multiplier)
            self.score += points
            self.best_combo = max(self.best_combo, self.combo)
            self.reaction_total_s += reaction
            self.reactions += 1
            self.best_reaction_s = (reaction if self.best_reaction_s is None
                                    else min(self.best_reaction_s, reaction))
            if prompt.kind in PUNCH_ACTIONS:
                self.successful_hits += 1
            elif prompt.kind in DODGE_ACTIONS:
                self.successful_dodges += 1
            elif prompt.kind == "block":
                self.successful_blocks += 1
            elif prompt.kind == "parry":
                self.successful_parries += 1
            self.events.append({"name": "training-success", "kind": prompt.kind,
                                "points": points, "reaction_ms": round(reaction * 1000)})
            self._prompt = None
            self._next_prompt_at = self._world_time + {1: 0.8, 2: 0.5, 3: 0.28}[prompt.stage]
        elif prompt is not None and self._world_time >= prompt.deadline:
            if prompt.kind in PUNCH_ACTIONS:
                self.missed_punches += 1
            self.combo = 0
            self.events.append({"name": "training-miss", "kind": prompt.kind})
            self._prompt = None
            self._next_prompt_at = self._world_time + 0.35

        if self._prompt is None and self._world_time >= self._next_prompt_at and self.time_left > 0:
            self._new_prompt()

        # A punch at the wrong target is a genuine miss, but count it once per
        # classified punch rather than once per render frame.
        if self._prompt is not None:
            for action in self._last_result.actions:
                if action.name in PUNCH_ACTIONS and not self._prompt_matches(
                        self._prompt, MotionResult(actions=[action])):
                    self.missed_punches += 1
                    self.combo = 0

    def _start_counter(self, now: float) -> None:
        if self._slow_motion:
            return
        self._slow_motion = True
        self._counter_until_real = now + self.tuning.counter_real_seconds
        self.successful_parries += 1
        self.score += 250
        self.opponent_ai.active = None
        self.opponent_ai.state = AIState.RECOVER
        self.opponent_ai.next_action_at = self._world_time + 0.8
        self.events.append({"name": "perfect-parry"})
        self.events.append({"name": "slow-motion-start"})

    @staticmethod
    def _zone_for(action: BoxingAction) -> str:
        if action.target == "body":
            return "left_shoulder" if action.side == "right" else "right_shoulder"
        return "right_eye" if action.side == "left" else "left_eye"

    def _player_attacks(self, now: float) -> None:
        for action in self._last_result.actions:
            if action.name not in PUNCH_ACTIONS:
                continue
            defense = ("none" if self._slow_motion else
                       self.opponent_ai.defend(action.side, action.target, self._world_time))
            if defense == "dodge":
                self.missed_punches += 1
                self.combo = 0
                self.events.append({"name": "opponent-dodge", "side": action.side})
                continue
            if defense == "block":
                self.missed_punches += 1
                self.combo = 0
                self.events.append({"name": "opponent-block", "target": action.target})
                continue

            damage = (self.tuning.head_damage if action.target == "head"
                      else self.tuning.body_damage)
            if self._slow_motion:
                damage = round(damage * 1.25)
            zone = self._zone_for(action)
            dealt = self.opponent_damage.hit(damage, zone)
            self.successful_hits += 1
            self.combo = self.combo + 1 if now - self._last_hit_at <= self.tuning.combo_window_s else 1
            self._last_hit_at = now
            self.best_combo = max(self.best_combo, self.combo)
            self.score += dealt * 10 * max(1, min(4, self.combo))
            self.events.append({
                "name": "player-hit", "side": action.side,
                "target": action.target, "zone": zone, "damage": dealt,
                "hook": "hook" in action.name, "combo": self.combo,
                "trajectory": list(action.trajectory),
            })
            if action.target == "head" or self.combo >= 3:
                self.events.append({"name": "crowd-reaction",
                                    "strength": min(4, self.combo)})
            if self.opponent_damage.hp <= 0:
                self.events.append({"name": "knockdown", "fighter": "opponent"})
                self.events.append({"name": "knockout", "fighter": "opponent"})
                self.events.append({"name": "crowd-cheer", "strength": 4})
                self.finish(now, "win")
                return

    def _opponent_impact(self, now: float, attack) -> None:
        names = self._last_result.names
        if "parry" in names:
            self._start_counter(now)
            return
        dodged = bool(names & DODGE_ACTIONS)
        if dodged:
            self.successful_dodges += 1
            self.score += 100
            self.events.append({"name": "player-dodge", "kind": next(iter(names & DODGE_ACTIONS))})
            return
        guard = self._last_result.guard
        blocked = (guard == "two_hand_guard" or
                   guard == f"{attack.side}_block" or guard.endswith("_block"))
        damage = attack.damage
        if blocked:
            damage = round(damage * self.tuning.block_damage_ratio)
            self.successful_blocks += 1
            self.score += 60
            self.events.append({"name": "player-block", "damage": damage})
        else:
            self.events.append({"name": "opponent-hit", "target": attack.target,
                                "damage": damage})
        # The player body is drawn from behind, so only its shoulder injuries
        # are visible. Route all AI impacts to the shoulder nearest the glove.
        zone = "left_shoulder" if attack.side == "right" else "right_shoulder"
        self.player_damage.hit(damage, zone)
        if self.player_damage.hp <= 0:
            self.events.append({"name": "knockdown", "fighter": "player"})
            self.events.append({"name": "knockout", "fighter": "player"})
            self.finish(now, "lose")

    def _tick_fight(self, now: float) -> None:
        if "parry" in self._last_result.names and self.opponent_ai.active is not None:
            self._start_counter(now)
        self._player_attacks(now)
        if self.state is not State.PLAYING:
            return
        for event in self.opponent_ai.tick(
                self._world_time, allow_attack=not self._slow_motion):
            attack = event["attack"]
            if event["name"] == "opponent-attack":
                self.events.append({"name": "opponent-attack",
                                    **attack.snapshot(self._world_time)})
            else:
                self._opponent_impact(now, attack)

    def _countdown(self, now: float) -> str:
        if self.state is not State.PLAYING or self._round_started:
            return ""
        remaining = self._countdown_until - now
        if remaining > 3.0:
            return "READY"
        if remaining > 0:
            return str(max(1, math.ceil(remaining)))
        return "FIGHT!"

    def snapshot(self, now: float) -> dict:
        average = self.reaction_total_s / self.reactions if self.reactions else 0.0
        accuracy = (100.0 * self.successful_hits / self.punches_thrown
                    if self.punches_thrown else 0.0)
        stats = {
            "punches_thrown": self.punches_thrown,
            "successful_hits": self.successful_hits,
            "missed_punches": self.missed_punches,
            "accuracy": round(accuracy, 1),
            "successful_dodges": self.successful_dodges,
            "successful_blocks": self.successful_blocks,
            "successful_parries": self.successful_parries,
            "best_combo": self.best_combo,
            "average_reaction_ms": round(average * 1000),
            "best_reaction_ms": round((self.best_reaction_s or 0.0) * 1000),
        }
        data = {
            "kind": "boxing",
            "state": self.state.value,
            "mode": self.mode,
            "difficulty": self.difficulty,
            "score": self.score,
            "best": self.best,
            "time_left": round(self.time_left, 2),
            "duration": round(self.duration, 1),
            "combo": self.combo,
            "countdown": self._countdown(now),
            "round_started": self._round_started,
            "slow_motion": self._slow_motion,
            "simulation_scale": self.tuning.slow_motion_scale if self._slow_motion else 1.0,
            "result": self.result,
            "stats": stats,
            "motion": self._last_result.as_dict(),
            "player": self.player_damage.snapshot(),
            "opponent": self.opponent_damage.snapshot(),
        }
        if self._prompt is not None:
            data["prompt"] = self._prompt.snapshot(self._world_time)
        if self.mode == "fight":
            data["ai"] = self.opponent_ai.snapshot(self._world_time)
        return data

    def take_events(self) -> list[dict]:
        events, self.events = self.events[-40:], []
        return events
