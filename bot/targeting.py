"""Spreading attackers over targets so their damage actually lands (see `bot.efficiency`).

A melee target can only be hit by as many attackers as fit around it. Sending more just blocks:
the extras stand behind the front line (or at the bottom of a ramp) doing nothing. `TargetSlots`
caps each target at what can use it, so the next attacker picks the next target, deeper in.

The capacity rule depends on the attacker, not on zerglings: melee attackers are limited by space
around the target; ranged and air attackers (e.g. mutalisks) aren't, so they get no cap here (an
overkill cap, based on the target's remaining health, would go in `attack_capacity`).
"""

from __future__ import annotations

import math

from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import MELEE_SPACING, MELEE_SURROUND_FRACTION

MELEE_RANGE = 1.0  # weapon range at or below this counts as melee


def is_melee_against(attacker: Unit, target: Unit) -> bool:
    weapon_range = attacker.air_range if target.is_flying else attacker.ground_range
    return weapon_range <= MELEE_RANGE


def attack_capacity(attacker: Unit, target: Unit) -> int | None:
    """How many attackers like `attacker` can usefully hit `target` at once; None = no limit."""
    if not target.is_structure:
        return None  # units move and get surrounded from every side: everyone collapses on them
    if not is_melee_against(attacker, target):
        return None  # ranged/air attackers aren't limited by space
    # Attackers stand on a ring around the target, and mostly arrive from one side
    ring = 2 * math.pi * (target.radius + attacker.radius) * MELEE_SURROUND_FRACTION
    return max(1, int(ring // (2 * attacker.radius * MELEE_SPACING)))


class TargetSlots:
    """Per squad, per step: how many attackers each target already has."""

    def __init__(self) -> None:
        self.taken: dict[int, int] = {}

    def has_room(self, attacker: Unit, target: Unit) -> bool:
        capacity = attack_capacity(attacker, target)
        return capacity is None or self.taken.get(target.tag, 0) < capacity

    def take(self, target: Unit) -> None:
        self.taken[target.tag] = self.taken.get(target.tag, 0) + 1

    def pick(self, attacker: Unit, candidates: list[Unit], depth_from: Point2 | None = None) -> Unit:
        """Which candidate this attacker should go for.

        Without `depth_from`: the closest one with room. With it (structures, melee): fill from
        the back. Attackers are handled front-first, and each takes the *deepest* candidate (the
        farthest from `depth_from`, the squad's position) that still has room, so the first ones
        in run past the front structures and later ones fill in toward the front: nobody has to
        get past anyone. An attacker keeps its current target while that still has room (the
        squad's position moves as it advances, so re-picking "deepest" every step would send
        attackers back and forth). If every candidate is full: the closest.
        """
        by_distance = sorted(candidates, key=lambda e: e.position.distance_to(attacker.position))
        current = next((e for e in candidates if e.tag == attacker.order_target), None)
        if current is not None and self.has_room(attacker, current):
            choice = current  # stick with it until it dies or fills up (re-picking on the way thrashes)
        elif depth_from is not None:
            deepest = sorted(candidates, key=lambda e: -e.position.distance_to(depth_from))
            choice = next((e for e in deepest if self.has_room(attacker, e)), by_distance[0])
        else:
            choice = next((e for e in by_distance if self.has_room(attacker, e)), by_distance[0])
        self.take(choice)
        return choice
