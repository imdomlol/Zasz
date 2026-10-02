"""Spreading attackers over targets so their damage actually lands (see `bot.efficiency`).

A melee target can only be hit by as many attackers as fit around it. Sending more just blocks:
the extras stand behind the front line (or at the bottom of a ramp) doing nothing. `TargetSlots`
caps each target at what can use it, so the next attacker picks the next target, deeper in.

The capacity rule depends on the attacker, not on zerglings: melee attackers are limited by space
around the target (a structure: part of the ring, they arrive from one side; a unit: about a full
ring, it gets surrounded); ranged and air attackers (e.g. mutalisks) aren't, so they get no cap
here (an overkill cap, based on the target's remaining health, would go in `attack_capacity`).

`BlockTracker` catches what capacity can't predict: a zergling that stops getting closer to its
target (stuck behind other units, in a cramped base, at a choke) is blocked *for that target*, so it
picks another one it can reach, or goes around.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import (
    BLOCKED_MEMORY_SECONDS,
    BLOCKED_SECONDS,
    DETOUR_CROWD_COST,
    DETOUR_JAM_COST,
    DETOUR_MAX_EXTRA,
    DETOUR_MAX_FACTOR,
    DETOUR_SECONDS,
    MELEE_SPACING,
    MELEE_SURROUND_FRACTION,
    MELEE_UNIT_SURROUND_FRACTION,
    PROGRESS_STEP,
)

MELEE_RANGE = 1.0  # weapon range at or below this counts as melee


def is_melee_against(attacker: Unit, target: Unit) -> bool:
    weapon_range = attacker.air_range if target.is_flying else attacker.ground_range
    return weapon_range <= MELEE_RANGE


def attack_capacity(attacker: Unit, target: Unit) -> int | None:
    """How many attackers like `attacker` can usefully hit `target` at once; None = no limit."""
    if not is_melee_against(attacker, target):
        return None  # ranged/air attackers aren't limited by space
    # Attackers stand on a ring around the target: structures mostly from one side, units (which
    # move and get surrounded) from all sides
    fraction = MELEE_SURROUND_FRACTION if target.is_structure else MELEE_UNIT_SURROUND_FRACTION
    ring = 2 * math.pi * (target.radius + attacker.radius) * fraction
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
        if not candidates:
            raise ValueError("TargetSlots.pick needs at least one candidate")
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


@dataclass
class _Progress:
    target: int  # target tag
    best: float  # closest the attacker has been to it (edge to edge)
    since: float  # game time `best` last improved


class BlockTracker:
    """Which attackers are stuck: not getting closer to their target while out of range."""

    def __init__(self) -> None:
        self.progress: dict[int, _Progress] = {}  # attacker tag -> progress on its current target
        self.blocked: dict[int, dict[int, float]] = {}  # attacker tag -> {target tag: blocked until}

    def is_blocked(self, attacker: Unit, target: Unit, now: float) -> bool:
        return self.blocked.get(attacker.tag, {}).get(target.tag, 0) > now

    def reachable(self, attacker: Unit, candidates: list[Unit], now: float) -> list[Unit]:
        """Candidates this attacker hasn't recently been blocked from."""
        return [c for c in candidates if not self.is_blocked(attacker, c, now)]

    def update(self, attacker: Unit, target: Unit, now: float) -> bool:
        """Record progress toward `target`; True if the attacker just became blocked on it."""
        gap = attacker.position.distance_to(target.position) - attacker.radius - target.radius
        weapon_range = attacker.air_range if target.is_flying else attacker.ground_range
        progress = self.progress.get(attacker.tag)
        if gap <= weapon_range + 0.25 or progress is None or progress.target != target.tag:
            self.progress[attacker.tag] = _Progress(target.tag, gap, now)  # in range, or a new target
            return False
        if gap < progress.best - PROGRESS_STEP:
            progress.best, progress.since = gap, now
            return False
        if now - progress.since < BLOCKED_SECONDS:
            return False
        self.blocked.setdefault(attacker.tag, {})[target.tag] = now + BLOCKED_MEMORY_SECONDS
        del self.progress[attacker.tag]
        return True

    def forget_dead(self, alive: set[int]) -> None:
        self.progress = {t: p for t, p in self.progress.items() if t in alive}
        self.blocked = {t: b for t, b in self.blocked.items() if t in alive}


def pick_tiered(
    slots: TargetSlots,
    blocks: BlockTracker,
    attacker: Unit,
    tiers: list[tuple[list[Unit], bool]],
    depth_from: Point2 | None,
    now: float,
) -> tuple[Unit | None, bool]:
    """The best target for `attacker`: the first tier (highest priority) with a reachable target
    that still has room. Each tier is (targets, fill_from_back): fill from the back takes the deepest
    one from `depth_from`, otherwise the closest. An attacker keeps its current target while that
    still has room and isn't blocked.

    Returns (target, flank): flank=True means every target is blocked for this attacker (go around
    the returned one). If every reachable target is full, the closest one in the best tier.
    """
    candidates = [t for tier, _ in tiers for t in tier]
    if not candidates:
        return None, False
    current = next((t for t in candidates if t.tag == attacker.order_target), None)
    best_tier = next(
        (tier for tier, _ in tiers if any(slots.has_room(attacker, e) for e in blocks.reachable(attacker, tier, now))),
        None,
    )
    if (
        current is not None
        and best_tier is not None
        and any(e.tag == current.tag for e in best_tier)
        and slots.has_room(attacker, current)
        and not blocks.is_blocked(attacker, current, now)
    ):
        # Stick with it, but only while it's in the best tier we can reach: a zergling breaking
        # through a gateway switches back to the units as soon as one is reachable
        slots.take(current)
        return current, False
    for tier, back in tiers:
        reachable = blocks.reachable(attacker, tier, now)
        if back and depth_from is not None:
            ordered = sorted(reachable, key=lambda e: -e.position.distance_to(depth_from))
        else:
            ordered = sorted(reachable, key=lambda e: e.position.distance_to(attacker.position))
        if choice := next((e for e in ordered if slots.has_room(attacker, e)), None):
            slots.take(choice)
            return choice, False
    # Everything reachable is full: the closest reachable one in the best tier that has any
    for tier, _ in tiers:
        if reachable := blocks.reachable(attacker, tier, now):
            choice = min(reachable, key=lambda e: e.position.distance_to(attacker.position))
            slots.take(choice)
            return choice, False
    # Blocked from everything: go around the closest
    return min(candidates, key=lambda e: e.position.distance_to(attacker.position)), True


@dataclass
class _Route:
    target: int  # target tag
    waypoints: list[Point2]
    until: float  # give up on the detour after this


class Detours:
    """Routes around our own crowds for blocked attackers.

    Pathing ignores our own units, so a zergling stuck behind others keeps choosing the same jammed
    lane (e.g. a 1-tile gap between gateways). For a blocked attacker we plan on a copy of the
    ground grid where friendly units near it are expensive and the jammed spot even more, so the
    cheapest path to the same target goes around (e.g. around the outside of a gateway grid).
    """

    def __init__(self) -> None:
        self.routes: dict[int, _Route] = {}

    def route_target(self, attacker: Unit, now: float) -> int | None:
        route = self.routes.get(attacker.tag)
        if route is None or route.until < now or not route.waypoints:
            self.routes.pop(attacker.tag, None)
            return None
        return route.target

    def plan(self, ai, attacker: Unit, target: Unit, crowd: list[Unit], now: float) -> bool:
        """Plan a detour to `target` around `crowd`; False if there's no reasonable one."""
        map_data = ai.mediator.get_map_data_object
        grid = ai.mediator.get_ground_grid.copy()
        for unit in crowd:
            grid = map_data.add_cost(unit.position, unit.radius + 0.5, grid, weight=DETOUR_CROWD_COST)
        grid = map_data.add_cost(attacker.position, 2.0, grid, weight=DETOUR_JAM_COST)
        path = ai.mediator.find_raw_path(start=attacker.position, target=target.position, grid=grid, sensitivity=2)
        if not path:
            return False
        length = sum(a.distance_to(b) for a, b in zip([attacker.position, *path], path, strict=False))
        if length > DETOUR_MAX_FACTOR * attacker.position.distance_to(target.position) + DETOUR_MAX_EXTRA:
            return False  # no reasonable way around
        self.routes[attacker.tag] = _Route(target.tag, list(path), now + DETOUR_SECONDS)
        return True

    def next_waypoint(self, attacker: Unit) -> Point2 | None:
        route = self.routes.get(attacker.tag)
        if route is None:
            return None
        while route.waypoints and route.waypoints[0].distance_to(attacker.position) < 1.5:
            route.waypoints.pop(0)
        return route.waypoints[0] if route.waypoints else None

    def drop(self, attacker: Unit) -> None:
        self.routes.pop(attacker.tag, None)

    def forget_dead(self, alive: set[int]) -> None:
        self.routes = {t: r for t, r in self.routes.items() if t in alive}
