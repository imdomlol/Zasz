"""Damage efficiency: how much of the army's damage potential actually lands.

Every step, each army unit that is **committed** to a target counts its potential DPS against it
(`Unit.calculate_dps_vs_target`: armor, upgrades and bonus damage included, air or ground). That
potential is split by what the unit is actually doing:

- **delivered**: it's swinging/shooting (`weapon_cooldown > 0`, i.e. it fired recently)
- **blocked**: it's out of range and not getting closer (stuck behind other units, on a ramp, or
  unable to reach a spot around the target)
- **idle**: it's in range but not attacking

Efficiency = delivered / potential. Nothing here is melee-specific: ranges come from the unit's
own ground/air weapon, so it works the same for zerglings and for ranged or air units (mutalisks).
Mutalisk bounce damage isn't part of `calculate_dps_vs_target`, so it isn't counted as potential.

A unit walking to a spot near its target (a detour, a safe cell, around others: a move order, not an
attack) stays engaged while the army still has it assigned to that target (`Army.assignments`, picked
within ASSIGNED_SECONDS): those steps count for nothing, and its commitment clock keeps running.

"Committed" means: the same attack target for at least COMMIT_SECONDS, the unit isn't chasing a
moving target it can't reach yet (away or sideways), and it isn't freely walking up to it (getting
closer at a decent fraction of its speed). So chasing a scouting worker, switching targets or
travelling don't count as waste; standing around without hitting anything does.

Damage dealt and taken are tracked alongside, from health + shield changes of visible enemies and
our own army units. In real games those are noisy (repair, regeneration, damage from queens and
static defense, and only what we can see), so the per-unit split above is the main measure.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sc2.position import Point2
from sc2.unit import Unit

if TYPE_CHECKING:
    from ares import AresBot

COMMIT_SECONDS = 1.0  # same target at least this long before a unit counts
APPROACH_FRACTION = 0.3  # closing in at >= this fraction of its speed = travelling, not stuck
RANGE_SLACK = 0.25  # extra range allowed when deciding "in range" (positions are approximate)
MOVING_TARGET_SPEED = 0.5  # a target moving faster than this (per second) is being chased
WINDOW_SECONDS = 10.0  # rolling window for `recent_efficiency`
ASSIGNED_SECONDS = 1.0  # a unit the army picked a target for this recently is still on it while it walks


@dataclass
class EfficiencyTotals:
    potential: float = 0.0  # damage committed units could have dealt
    delivered: float = 0.0  # ...by units that were actually attacking
    blocked: float = 0.0  # ...by units out of range and not getting closer
    idle: float = 0.0  # ...by units in range but not attacking
    damage_dealt: float = 0.0  # enemy health + shields lost (visible enemies)
    damage_taken: float = 0.0  # health lost by our army units
    started: float = 0.0  # game time measuring started
    elapsed: float = 0.0  # game seconds measured so far

    @property
    def efficiency(self) -> float | None:
        return self.delivered / self.potential if self.potential > 0 else None

    def share(self, part: float) -> float:
        return part / self.potential if self.potential > 0 else 0.0

    @property
    def dealt_per_second(self) -> float:
        """Throughput: unlike efficiency this includes travel and stalls, so it can't be gamed by
        units that look efficient but take longer to get anywhere."""
        return self.damage_dealt / self.elapsed if self.elapsed > 0 else 0.0

    def summary(self) -> str:
        eff = self.efficiency
        ratio = self.damage_dealt / self.damage_taken if self.damage_taken > 0 else float("inf")
        return (
            f"{self.dealt_per_second:.0f} dmg/s, "
            + (
                f"efficiency {eff:.0%} (blocked {self.share(self.blocked):.0%}, idle {self.share(self.idle):.0%}), "
                if eff is not None
                else "efficiency n/a, "
            )
            + f"dealt {self.damage_dealt:.0f}, taken {self.damage_taken:.0f} (ratio {ratio:.1f})"
        )


@dataclass
class _Engagement:
    target: int  # target tag
    since: float  # game time the unit started on this target
    distance: float  # last distance to the target
    target_position: Point2  # where the target was last step


class DamageTracker:
    def __init__(self, army_types: set) -> None:
        self.army_types = army_types
        self.total = EfficiencyTotals()
        self.engagements: dict[int, _Engagement] = {}  # army unit tag -> what it's attacking
        self.enemy_health: dict[int, float] = {}  # enemy tag -> last seen health + shields
        self.window: deque[tuple[float, float, float]] = deque()  # (time, potential, delivered)
        self.last_time: float | None = None

    def reset(self, now: float = 0.0) -> None:
        """Start measuring from now (e.g. when a scenario's units appear): totals, the window and every
        engagement start afresh (enemy health is kept, to see damage dealt from here on)."""
        self.total = EfficiencyTotals(started=now)
        self.window.clear()
        self.engagements.clear()
        self.last_time = None

    @property
    def recent_efficiency(self) -> float:
        """Delivered / potential over the last WINDOW_SECONDS; 1.0 when nothing was committed."""
        potential = sum(p for _, p, _ in self.window)
        return sum(d for _, _, d in self.window) / potential if potential > 0 else 1.0

    def on_damage_taken(self, unit: Unit, amount: float) -> None:
        if unit.type_id in self.army_types:
            self.total.damage_taken += amount

    def on_unit_destroyed(self, tag: int) -> None:
        """An enemy we saw die: its remaining health was dealt as damage."""
        if (health := self.enemy_health.pop(tag, None)) is not None:
            self.total.damage_dealt += health

    def update(self, ai: AresBot) -> None:
        now = ai.time
        dt = 0.0 if self.last_time is None else now - self.last_time
        self.last_time = now
        self.total.elapsed = now - self.total.started

        visible = {e.tag: e for e in ai.all_enemy_units if not e.is_memory}
        self._track_enemy_health(visible)

        potential = delivered = 0.0
        alive = set()
        army = getattr(ai, "army", None)
        assignments = army.assignments if army is not None else None
        for unit in ai.units:
            if unit.type_id not in self.army_types:
                continue
            alive.add(unit.tag)
            target = visible.get(unit.order_target) if isinstance(unit.order_target, int) else None
            if target is None:
                if (walking_to := self._assigned(assignments, unit, visible, now)) is not None:
                    self._walking(unit, walking_to)
                else:
                    self.engagements.pop(unit.tag, None)
                continue
            p, d = self._measure(unit, target, now, dt)
            potential += p
            delivered += d

        self.engagements = {tag: e for tag, e in self.engagements.items() if tag in alive}
        if dt > 0:
            self.window.append((now, potential, delivered))
            while self.window and self.window[0][0] < now - WINDOW_SECONDS:
                self.window.popleft()

    @staticmethod
    def _assigned(assignments, unit: Unit, visible: dict[int, Unit], now: float) -> Unit | None:
        """The visible enemy the army has this unit on (picked within ASSIGNED_SECONDS), if any."""
        if assignments is None or (tag := assignments.target.get(unit.tag)) is None:
            return None
        if now - assignments.picked_at.get(unit.tag, -99) > ASSIGNED_SECONDS:
            return None
        return visible.get(tag)

    def _walking(self, unit: Unit, target: Unit) -> None:
        """A move order on the way to its assigned target: keep its engagement (and clock), count nothing."""
        engagement = self.engagements.get(unit.tag)
        if engagement is None or engagement.target != target.tag:
            self.engagements.pop(unit.tag, None)  # walking isn't committing: no clock starts here
            return
        engagement.distance = unit.position.distance_to(target.position)
        engagement.target_position = target.position

    def _measure(self, unit: Unit, target: Unit, now: float, dt: float) -> tuple[float, float]:
        distance = unit.position.distance_to(target.position)
        previous = self.engagements.get(unit.tag)
        if previous is None or previous.target != target.tag:
            self.engagements[unit.tag] = _Engagement(target.tag, now, distance, target.position)
            return 0.0, 0.0
        closing_speed = (previous.distance - distance) / dt if dt > 0 else 0.0
        target_speed = target.position.distance_to(previous.target_position) / dt if dt > 0 else 0.0
        previous.distance = distance
        previous.target_position = target.position
        if dt <= 0 or now - previous.since < COMMIT_SECONDS:
            return 0.0, 0.0

        weapon_range = unit.air_range if target.is_flying else unit.ground_range
        gap = distance - unit.radius - target.radius
        in_range = gap <= weapon_range + RANGE_SLACK
        if not in_range:
            if closing_speed >= APPROACH_FRACTION * max(unit.real_speed, 0.1):
                return 0.0, 0.0  # travelling to the target, fine
            if target_speed > MOVING_TARGET_SPEED:
                return 0.0, 0.0  # chasing a moving target (away or sideways), not stuck

        dps = unit.calculate_dps_vs_target(target)
        if dps <= 0:
            return 0.0, 0.0  # it can't hurt this target at all (wrong weapon type)
        amount = dps * dt
        self.total.potential += amount
        if in_range and unit.weapon_cooldown > 0:
            self.total.delivered += amount
            return amount, amount
        if in_range:
            self.total.idle += amount
        else:
            self.total.blocked += amount
        return amount, 0.0

    def _track_enemy_health(self, visible: dict[int, Unit]) -> None:
        for tag, enemy in visible.items():
            health = enemy.health + enemy.shield
            previous = self.enemy_health.get(tag)
            if previous is not None and health < previous:
                self.total.damage_dealt += previous - health
            self.enemy_health[tag] = health
